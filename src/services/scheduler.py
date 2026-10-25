"""Safe Farming Schedule & Anti-Correlation Pairing Engine for FoxProfile.

Implements the 5-layer anti-detect scheduling algorithm:
1. Probation Guard: Newly created accounts (< 72h old) are placed in the later days of the cycle
   so they complete a full 72–96h cool-off after signup before their second session.
2. Cross-Cluster Pairing: Every simultaneous 2-account pair consists of 2 strictly different
   server/subnet clusters (e.g. `209.141.40.174` + `usa109`, `198.98.62.18` + `usa104`, or `usa108` + `usa103`).
3. Sequential Cluster Cooldown: Within each shift (Morning / Evening), pairs are interleaved
   so consecutive pairs avoid reusing the same proxy cluster back-to-back.
4. Anti-Correlation Dynamic Shuffling: Remembers previous cycle pairings and shifts in
   `monitor_settings.json` so two accounts do not repeatedly co-occur as a fixed pair across cycles.
5. Shared Endpoint Separation: Accounts sharing an identical `host:port` (e.g. `usa106:10291`)
   are separated by at least 2–3 days (>= 48–72h) and placed in late shifts if a sibling ran recently.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import itertools
import re
from dataclasses import dataclass, field

from ..models.profile import Profile
from .monitor import MonitorSnapshot, extract_proxy_endpoint, extract_proxy_port

_RE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_RE_USA_CLUSTER = re.compile(r"\b(usa\d+)\b", re.IGNORECASE)
_RE_PORT_SPLIT = re.compile(r":(\d{2,5})(?=:|$|\s|/)")

WEEKDAY_TAGS = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")
WEEKDAY_NAMES_VI = (
    "Thứ Hai",
    "Thứ Ba",
    "Thứ Tư",
    "Thứ Năm",
    "Thứ Sáu",
    "Thứ Bảy",
    "Chủ Nhật",
)
WEEKDAY_NAMES_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def extract_proxy_cluster(proxy_str: str | None) -> str:
    """Return the physical server/subnet cluster identifier for a proxy string."""
    if not proxy_str:
        return ""
    s = _RE_SCHEME.sub("", proxy_str.strip())
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    s = s.split("/", 1)[0].strip()
    m_port = _RE_PORT_SPLIT.search(s)
    host = s[: m_port.start()].strip().lower() if m_port else s.strip().lower()
    if not host:
        return ""
    m_usa = _RE_USA_CLUSTER.search(host)
    if m_usa:
        return m_usa.group(1).lower()
    return host


def is_gateway_ipv6_cluster(cluster: str) -> bool:
    """Return True if the cluster is a dedicated IPv6 gateway host (e.g. 209.141.40.174 or 198.98.62.18)."""
    return bool(cluster and cluster[0].isdigit())


@dataclass
class ScheduledAccount:
    name: str
    email: str
    cluster: str
    port: str
    endpoint: str
    timezone: str
    rest_hours: float
    age_hours: float
    is_probation: bool = False
    shared_endpoint: bool = False
    completed_today: bool = False


@dataclass
class ScheduledPair:
    pair_index: int
    shift: str  # "morning" | "evening"
    first: ScheduledAccount
    second: ScheduledAccount | None = None
    has_cluster_collision: bool = False
    has_adjacent_overlap: bool = False

    @property
    def clusters(self) -> set[str]:
        out = {self.first.cluster} if self.first.cluster else set()
        if self.second and self.second.cluster:
            out.add(self.second.cluster)
        return out

    @property
    def has_west_coast(self) -> bool:
        if "los_angeles" in (self.first.timezone or "").lower():
            return True
        return bool(self.second and "los_angeles" in (self.second.timezone or "").lower())


@dataclass
class ScheduledDay:
    day_index: int
    date_str: str
    weekday_tag: str
    weekday_name_vi: str
    weekday_name_en: str
    morning_pairs: list[ScheduledPair] = field(default_factory=list)
    evening_pairs: list[ScheduledPair] = field(default_factory=list)

    @property
    def all_pairs(self) -> list[ScheduledPair]:
        return [*self.morning_pairs, *self.evening_pairs]

    @property
    def account_names(self) -> list[str]:
        names: list[str] = []
        for pr in self.all_pairs:
            names.append(pr.first.name)
            if pr.second is not None:
                names.append(pr.second.name)
        return names

    @property
    def total_accounts(self) -> int:
        return len(self.account_names)


@dataclass
class SchedulePlan:
    generated_at: str
    cycle_days: int
    total_live: int
    days: list[ScheduledDay] = field(default_factory=list)
    completed_today: list[ScheduledAccount] = field(default_factory=list)
    skipped_drafts: list[str] = field(default_factory=list)
    collision_count: int = 0
    adjacent_overlap_count: int = 0
    repeated_pair_count: int = 0

    def status_tag_map(self) -> dict[str, str]:
        """Return `{profile_name: '<weekday>-C<pair>'}` for all scheduled accounts (clearing tags for accounts already opened today)."""
        done_names = {a.name for a in self.completed_today}
        mapping: dict[str, str] = {}
        for d in self.days:
            for pr in d.all_pairs:
                tag = f"{d.weekday_tag}-C{pr.pair_index}"
                mapping[pr.first.name] = "" if pr.first.name in done_names else tag
                if pr.second is not None:
                    mapping[pr.second.name] = "" if pr.second.name in done_names else tag
        return mapping

    def pairing_records(self) -> list[list[str]]:
        """Return list of `[first_name, second_name]` pairs for anti-correlation persistence."""
        records: list[list[str]] = []
        for d in self.days:
            for pr in d.all_pairs:
                if pr.second is not None:
                    records.append(sorted([pr.first.name, pr.second.name]))
        return records

    def shift_records(self) -> dict[str, str]:
        """Return `{profile_name: shift}` for shift-alternation persistence."""
        out: dict[str, str] = {}
        for d in self.days:
            for pr in d.all_pairs:
                out[pr.first.name] = pr.shift
                if pr.second is not None:
                    out[pr.second.name] = pr.shift
        return out


def _jitter_hash(name: str, seed_int: int) -> float:
    h = hashlib.md5(f"{seed_int}:{name}".encode()).digest()
    return (int.from_bytes(h[:4], "big") % 1000) / 1000.0


def _compute_even_day_quotas(total: int, cycle_days: int) -> list[int]:
    """Divide `total` accounts across `cycle_days` using even quotas so pairs stay complete."""
    if cycle_days <= 0:
        return []
    if total <= 0:
        return [0] * cycle_days
    total_pairs = total // 2
    odd_remainder = total % 2
    base_pairs = total_pairs // cycle_days
    extra_pairs = total_pairs % cycle_days
    quotas = [
        2 * (base_pairs + (1 if i < extra_pairs else 0))
        for i in range(cycle_days)
    ]
    if odd_remainder:
        quotas[-1] += 1
    return quotas


def _pair_day_accounts(
    accounts: list[ScheduledAccount],
    prev_pair_set: set[tuple[str, str]],
    seed_int: int,
) -> list[tuple[ScheduledAccount, ScheduledAccount | None]]:
    """Find the minimum-penalty pairing of `accounts` with zero same-cluster collisions."""
    if not accounts:
        return []
    pool = list(accounts)
    solo: ScheduledAccount | None = None
    if len(pool) % 2 == 1:
        # Leave the newest / least-rested non-gateway account as the trailing solo if odd
        pool.sort(key=lambda a: (a.rest_hours, _jitter_hash(a.name, seed_int)))
        solo = pool.pop(0)

    n = len(pool)
    if n == 0:
        return [(solo, None)] if solo else []

    # Order pool so accounts from the largest cluster in this day are matched first (MRV heuristic)
    cluster_counts: dict[str, int] = {}
    for a in pool:
        cluster_counts[a.cluster] = cluster_counts.get(a.cluster, 0) + 1
    pool.sort(
        key=lambda a: (
            -cluster_counts.get(a.cluster, 0),
            -int(is_gateway_ipv6_cluster(a.cluster)),
            _jitter_hash(a.name, seed_int),
        )
    )

    best_cost = float("inf")
    best_pairs: list[tuple[ScheduledAccount, ScheduledAccount]] = []
    used = [False] * n
    current: list[tuple[ScheduledAccount, ScheduledAccount]] = []
    nodes_visited = 0

    def _pair_cost(a: ScheduledAccount, b: ScheduledAccount) -> float:
        cost = 0.0
        if a.cluster and a.cluster == b.cluster:
            cost += 100_000.0
        key = (a.name, b.name) if a.name < b.name else (b.name, a.name)
        if key in prev_pair_set:
            cost += 500.0
        # Prefer pairing 1 IPv6 gateway (Las Vegas / New York) with 1 Detroit usa10X
        gw_a = is_gateway_ipv6_cluster(a.cluster)
        gw_b = is_gateway_ipv6_cluster(b.cluster)
        if gw_a == gw_b:
            cost += 15.0
        # Slight tie-breaker jitter so pairings vary naturally across cycles
        cost += _jitter_hash(f"{a.name}+{b.name}", seed_int) * 2.0
        return cost

    def _dfs(cost_so_far: float, seen_cluster_pairs: frozenset[tuple[str, str]]) -> None:
        nonlocal best_cost, best_pairs, nodes_visited
        if cost_so_far >= best_cost or nodes_visited > 4000:
            return
        first_idx = -1
        for i in range(n):
            if not used[i]:
                first_idx = i
                break
        if first_idx == -1:
            best_cost = cost_so_far
            best_pairs = list(current)
            return

        used[first_idx] = True
        a = pool[first_idx]
        candidates: list[tuple[float, int, tuple[str, str]]] = []
        for j in range(first_idx + 1, n):
            if not used[j]:
                b = pool[j]
                cp = (a.cluster, b.cluster) if a.cluster < b.cluster else (b.cluster, a.cluster)
                step_c = _pair_cost(a, b)
                if cp in seen_cluster_pairs:
                    step_c += 60.0
                candidates.append((step_c, j, cp))
        candidates.sort(key=lambda item: item[0])

        for step_cost, j, cp in candidates:
            nodes_visited += 1
            used[j] = True
            current.append((a, pool[j]))
            _dfs(cost_so_far + step_cost, seen_cluster_pairs | {cp})
            current.pop()
            used[j] = False
        used[first_idx] = False

    _dfs(0.0, frozenset())
    out: list[tuple[ScheduledAccount, ScheduledAccount | None]] = [
        (a, b) for a, b in best_pairs
    ]
    if solo is not None:
        out.append((solo, None))
    return out


def _order_shift_pairs(
    raw_pairs: list[tuple[ScheduledAccount, ScheduledAccount | None]],
    shift: str,
    last_shifts: dict[str, str],
    start_index: int,
    *,
    force_late_shared: bool = False,
) -> list[ScheduledPair]:
    """Order pairs within a shift to eliminate back-to-back cluster overlap."""
    if not raw_pairs:
        return []

    def _perm_score(perm: tuple[tuple[ScheduledAccount, ScheduledAccount | None], ...]) -> float:
        score = 0.0
        for idx, (a, b) in enumerate(perm):
            c_curr = {a.cluster} | ({b.cluster} if b else set())
            if idx > 0:
                pa, pb = perm[idx - 1]
                c_prev = {pa.cluster} | ({pb.cluster} if pb else set())
                overlap = c_curr & c_prev
                score += len(overlap) * 1000.0
                # Heavy penalty for repeating the same /64 node (usa10X) back-to-back
                score += sum(3000.0 for c in overlap if not is_gateway_ipv6_cluster(c))
                # Extra penalty if two IPv6 gateway pairs run back-to-back when a pure Detroit pair exists
                if any(is_gateway_ipv6_cluster(c) for c in overlap):
                    score += 500.0
            # In evening shift, prefer East Coast (Detroit/NY) in earlier slot and West Coast (LA) in later slot
            has_la = "los_angeles" in (a.timezone or "").lower() or (
                b is not None and "los_angeles" in (b.timezone or "").lower()
            )
            if shift == "evening" and has_la and idx == 0 and len(perm) > 1:
                score += 25.0
            # Keep shared endpoint pair at the end of the shift for maximum cooldown
            has_shared = a.shared_endpoint or (b is not None and b.shared_endpoint)
            if force_late_shared and has_shared and idx < len(perm) - 1:
                score += 200.0
            # Keep solo (if any) at the very end
            if b is None and idx < len(perm) - 1:
                score += 800.0
            # Slight reward for alternating shift from previous cycle
            if last_shifts.get(a.name) == shift:
                score += 5.0
            if b is not None and last_shifts.get(b.name) == shift:
                score += 5.0
        return score

    if len(raw_pairs) <= 6:
        best_perm = min(itertools.permutations(raw_pairs), key=_perm_score)
    else:
        best_perm = tuple(raw_pairs)

    gw_counts_in_shift: dict[str, int] = {}
    for a, b in best_perm:
        for c in ({a.cluster} | ({b.cluster} if b else set())):
            if is_gateway_ipv6_cluster(c):
                gw_counts_in_shift[c] = gw_counts_in_shift.get(c, 0) + 1
    max_non_adj_in_shift = (len(best_perm) + 1) // 2

    result: list[ScheduledPair] = []
    for offset, (a, b) in enumerate(best_perm):
        # Put gateway account first in the pair display for visual consistency
        if b is not None and is_gateway_ipv6_cluster(b.cluster) and not is_gateway_ipv6_cluster(a.cluster):
            a, b = b, a
        collision = bool(b is not None and a.cluster and a.cluster == b.cluster)
        adj_overlap = False
        if offset > 0:
            prev_clusters = result[-1].clusters
            curr_clusters = {a.cluster} | ({b.cluster} if b else set())
            ov = prev_clusters & curr_clusters
            adj_overlap = any(
                (not is_gateway_ipv6_cluster(c))
                or gw_counts_in_shift.get(c, 0) <= max_non_adj_in_shift
                for c in ov
            )
        result.append(
            ScheduledPair(
                pair_index=start_index + offset,
                shift=shift,
                first=a,
                second=b,
                has_cluster_collision=collision,
                has_adjacent_overlap=adj_overlap,
            )
        )
    return result


def _split_and_order_day_shifts(
    day_pairs: list[tuple[ScheduledAccount, ScheduledAccount | None]],
    last_shifts: dict[str, str],
    *,
    day_index: int,
) -> tuple[list[ScheduledPair], list[ScheduledPair]]:
    """Split a day's pairs into Morning (`Ca Sáng`) and Evening (`Ca Tối`) to minimize adjacent overlaps."""
    total_p = len(day_pairs)
    if total_p == 0:
        return [], []
    if total_p == 1:
        return _order_shift_pairs(day_pairs, "morning", last_shifts, 1), []

    morning_count = (total_p + 1) // 2
    indices = range(total_p)

    best_score = float("inf")
    best_m: list[ScheduledPair] = []
    best_e: list[ScheduledPair] = []

    for m_idx_tuple in itertools.combinations(indices, morning_count):
        m_set = set(m_idx_tuple)
        m_raw = [day_pairs[i] for i in indices if i in m_set]
        e_raw = [day_pairs[i] for i in indices if i not in m_set]

        # On Day 0, if any pair has a shared endpoint (e.g. usa106:10291), prefer placing it in Evening shift
        shared_in_morning = any(
            a.shared_endpoint or (b is not None and b.shared_endpoint)
            for a, b in m_raw
        )
        # Also keep any odd solo in Evening shift
        solo_in_morning = any(b is None for _, b in m_raw)

        m_ordered = _order_shift_pairs(
            m_raw,
            "morning",
            last_shifts,
            1,
            force_late_shared=(day_index == 0),
        )
        e_ordered = _order_shift_pairs(
            e_raw,
            "evening",
            last_shifts,
            len(m_ordered) + 1,
            force_late_shared=True,
        )

        pen = 0.0
        if day_index == 0 and shared_in_morning:
            pen += 400.0
        if solo_in_morning:
            pen += 400.0
        # Penalize duplicate /64 non-gateway clusters (usa10X) within the same shift so 2 accounts
        # on the same /64 node are separated into Morning vs Evening shifts.
        for shift_list in (m_ordered, e_ordered):
            seen_non_gw: set[str] = set()
            for p in shift_list:
                for c in p.clusters:
                    if c and not is_gateway_ipv6_cluster(c):
                        if c in seen_non_gw:
                            pen += 2500.0
                        seen_non_gw.add(c)
        pen += sum(1000.0 for p in m_ordered if p.has_adjacent_overlap)
        pen += sum(1000.0 for p in e_ordered if p.has_adjacent_overlap)
        for p in m_ordered:
            if last_shifts.get(p.first.name) == "morning":
                pen += 8.0
            if p.second and last_shifts.get(p.second.name) == "morning":
                pen += 8.0
        for p in e_ordered:
            if last_shifts.get(p.first.name) == "evening":
                pen += 8.0
            if p.second and last_shifts.get(p.second.name) == "evening":
                pen += 8.0

        if pen < best_score:
            best_score = pen
            best_m = m_ordered
            best_e = e_ordered

    return best_m, best_e


def build_safe_schedule(
    profiles: list[Profile],
    snap: MonitorSnapshot,
    *,
    cycle_days: int = 5,
    last_pairings: list[list[str]] | None = None,
    last_shifts: dict[str, str] | None = None,
    shuffle_seed: int | None = None,
    now: dt.datetime | None = None,
) -> SchedulePlan:
    """Compute a conflict-free multi-day paired farming schedule for all Live profiles."""
    now_dt = now or dt.datetime.now()
    cycle_days = max(2, min(7, int(cycle_days)))
    seed_int = (
        shuffle_seed
        if shuffle_seed is not None
        else int(now_dt.strftime("%Y%m%d"))
    )

    prev_pair_set: set[tuple[str, str]] = set()
    for item in last_pairings or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            a_s, b_s = str(item[0]), str(item[1])
            prev_pair_set.add((a_s, b_s) if a_s < b_s else (b_s, a_s))
    prev_shifts = dict(last_shifts or {})

    # 1. Identify eligible Live profiles vs empty draft slots
    live_Raw: list[Profile] = []
    skipped_drafts: list[str] = []
    endpoint_counts: dict[str, int] = {}

    for p in profiles:
        grp = (p.group or "").strip().lower()
        pgs_lower = [pg.strip().lower() for pg in (p.pages or [])]
        if grp == "disconnected" or "disconnected" in pgs_lower:
            continue
        if not (p.proxy or "").strip():
            continue
        info = snap.profiles.get(p.name)
        if info and info.is_banned:
            continue
        has_disk_activity = bool(
            info
            and (
                info.hatch_gw
                or info.hatch_sess
                or info.cookie_count > 0
                or info.session_count > 0
                or info.history_pages > 0
            )
        )
        has_account = (
            bool((p.email or "").strip())
            or has_disk_activity
            or ("live" in pgs_lower and not p.name.strip().isdigit())
        )
        if not has_account:
            skipped_drafts.append(p.name)
            continue
        live_Raw.append(p)
        ep = extract_proxy_endpoint(p.proxy)
        if ep:
            endpoint_counts[ep] = endpoint_counts.get(ep, 0) + 1

    # 2. Build ScheduledAccount objects with exact rest_hours and probation flags
    sched_accounts: list[ScheduledAccount] = []
    completed_today_list: list[ScheduledAccount] = []

    for p in live_Raw:
        info = snap.profiles.get(p.name)
        lc = info.last_closed if info else None
        created_dt: dt.datetime | None = None
        if p.created_at:
            with contextlib.suppress(ValueError):
                created_dt = dt.datetime.strptime(p.created_at.strip()[:10], "%Y-%m-%d").replace(
                    hour=12, minute=0, second=0
                )
        if lc is None:
            lc = created_dt or (now_dt - dt.timedelta(hours=120))

        rest_h = max(0.0, (now_dt - lc).total_seconds() / 3600.0)
        if created_dt is not None:
            age_h = max(rest_h, (now_dt - created_dt).total_seconds() / 3600.0)
            days_old = (now_dt.date() - created_dt.date()).days
            if (
                info is not None
                and info.datr_age_days > 0.0
                and abs(days_old - info.datr_age_days) <= 3.0
            ):
                days_old = int(info.datr_age_days)
                age_h = max(rest_h, info.datr_age_days * 24.0)
        else:
            age_h = max(rest_h, 120.0)
            days_old = 5

        # Probation Guard: Accounts created within the last 2 calendar days (< 60h rest)
        # must be placed in Day >= 2 so they accumulate >= 72-96h rest before opening.
        is_probation = days_old <= 2 and rest_h < 60.0
        is_run = bool(info and info.running_mins is not None)
        ran_today = is_run or (
            lc.date() == now_dt.date()
            and lc.hour >= 6
            and days_old > 1
        )
        ep = extract_proxy_endpoint(p.proxy)
        acc = ScheduledAccount(
            name=p.name,
            email=(p.email or p.name).strip(),
            cluster=extract_proxy_cluster(p.proxy),
            port=extract_proxy_port(p.proxy),
            endpoint=ep,
            timezone=(p.timezone or "America/Detroit").strip(),
            rest_hours=rest_h,
            age_hours=age_h,
            is_probation=is_probation,
            shared_endpoint=(endpoint_counts.get(ep, 0) > 1),
            completed_today=ran_today,
        )
        sched_accounts.append(acc)
        if ran_today:
            completed_today_list.append(acc)

    # 3. Sort accounts strictly by rest_hours descending (longest rested first -> shortest rested last)
    # while pinning accounts already opened today (`completed_today`) to Day 1 (Today) so mid-day runs
    # never shift the remaining schedule or hide Today's port usage.
    def _urgency_key(a: ScheduledAccount) -> tuple[int, int, float]:
        ran_flag = -1 if a.completed_today else 0
        rest_band = -int(a.rest_hours // 3.0)
        return (ran_flag, rest_band, _jitter_hash(a.name, seed_int))

    sched_accounts.sort(key=_urgency_key)

    # 4. Partition into `cycle_days` daily buckets while enforcing:
    #    - Probation minimum day index (rest_hours + d_idx * 24 >= 72h for new accounts)
    #    - Shared Endpoint Separation (>= 2-3 days apart)
    #    - Cluster balance per day (`cluster_count_in_day <= min(pairs_in_day, ceil(total_c / cycle_days))`)
    quotas = _compute_even_day_quotas(len(sched_accounts), cycle_days)
    day_buckets: list[list[ScheduledAccount]] = [[] for _ in range(cycle_days)]
    day_cluster_counts: list[dict[str, int]] = [{} for _ in range(cycle_days)]
    endpoint_assigned_days: dict[str, list[int]] = {}

    total_by_cluster: dict[str, int] = {}
    for a in sched_accounts:
        if a.cluster:
            total_by_cluster[a.cluster] = total_by_cluster.get(a.cluster, 0) + 1

    max_pairs_per_day = [max(1, (q + 1) // 2) for q in quotas]

    def _min_day_for_account(a: ScheduledAccount) -> int:
        if a.completed_today:
            return 0
        if cycle_days >= 4 and a.rest_hours < 41.0:
            # Ensure every account that rested < 41h accumulates at least ~63h total rest
            # by its scheduled day (so 39-40h accounts can enter Day 2 at 63-64h rest,
            # 28-33h accounts enter Day 3/4 at 76-105h rest, and 7-8h new accounts enter Day 4/5 at 79-104h rest).
            needed_days = max(1, int((63.0 - a.rest_hours + 23.9) // 24.0))
            return min(cycle_days - 1, needed_days)
        if a.is_probation and cycle_days >= 4:
            return 1
        return 0

    # Pre-compute balanced per-day targets for any gateway / large cluster (e.g. 209.141.40.174)
    # so it distributes evenly across all days (e.g. [4, 4, 3, 3, 4]) instead of crowding the last day.
    gateway_day_targets: dict[str, list[int]] = {}
    for c_name, tot_c in total_by_cluster.items():
        if is_gateway_ipv6_cluster(c_name) and tot_c >= cycle_days:
            base_c = tot_c // cycle_days
            rem_c = tot_c % cycle_days
            # Give +1 to days with the largest quotas first (e.g. Day 0 and Day 4), then earliest days
            day_order = sorted(
                range(cycle_days),
                key=lambda d: (-max_pairs_per_day[d], d),
            )
            extra_days = set(day_order[:rem_c])
            gateway_day_targets[c_name] = [
                min(max_pairs_per_day[d], base_c + (1 if d in extra_days else 0))
                for d in range(cycle_days)
            ]

    def _cluster_cap_for_day(cluster: str, d_idx: int, *, relax: int = 0) -> int:
        p_cap = max_pairs_per_day[d_idx]
        if not cluster:
            return p_cap
        if cluster in gateway_day_targets and relax == 0:
            return gateway_day_targets[cluster][d_idx]
        tot_c = total_by_cluster.get(cluster, 1)
        even_c = (tot_c + cycle_days - 1) // cycle_days
        return min(p_cap, even_c + relax)

    # Place gateway cluster accounts first (in rest_hours descending order) so every day gets its exact
    # balanced share of gateway accounts before non-gateway accounts fill the remaining slots.
    gateway_accounts = [a for a in sched_accounts if a.cluster in gateway_day_targets]
    other_accounts = [a for a in sched_accounts if a.cluster not in gateway_day_targets]
    ordered_for_bucketing = [*gateway_accounts, *other_accounts]

    rem_by_cluster: dict[str, int] = dict(total_by_cluster)
    rem_other_total = len(other_accounts)

    def _effective_min_day(a: ScheduledAccount) -> int:
        raw_d = _min_day_for_account(a)
        if raw_d <= 0:
            return 0
        c = a.cluster
        rem_c = rem_by_cluster.get(c, 1) if c else 1
        is_gw = c in gateway_day_targets
        while raw_d > 0:
            # 1. Check if remaining cluster capacity in days [raw_d .. cycle_days-1] can hold `rem_c`
            avail_c = sum(
                max(
                    0,
                    _cluster_cap_for_day(c, k, relax=0)
                    - day_cluster_counts[k].get(c, 0),
                )
                for k in range(raw_d, cycle_days)
            )
            if avail_c < rem_c:
                raw_d -= 1
                continue
            # 2. For non-gateway accounts, check if remaining open slots in days [raw_d .. cycle_days-1] can hold `rem_other_total`
            if not is_gw:
                avail_open = sum(
                    max(0, quotas[k] - len(day_buckets[k]))
                    for k in range(raw_d, cycle_days)
                )
                if avail_open < rem_other_total:
                    raw_d -= 1
                    continue
            break
        return raw_d

    unassigned: list[ScheduledAccount] = []
    for acc in ordered_for_bucketing:
        min_d = _effective_min_day(acc)
        if acc.cluster:
            rem_by_cluster[acc.cluster] = max(0, rem_by_cluster.get(acc.cluster, 1) - 1)
        if acc.cluster not in gateway_day_targets:
            rem_other_total = max(0, rem_other_total - 1)
        placed = False
        for relax in (0, 1):
            for d_idx in range(min_d, cycle_days):
                if len(day_buckets[d_idx]) >= quotas[d_idx]:
                    continue
                if acc.shared_endpoint and acc.endpoint in endpoint_assigned_days:
                    min_gap = 2 if cycle_days >= 4 else 1
                    if any(
                        abs(d_idx - prev_d) < min_gap
                        for prev_d in endpoint_assigned_days[acc.endpoint]
                    ):
                        continue
                curr_c = day_cluster_counts[d_idx].get(acc.cluster, 0)
                if acc.cluster and curr_c >= _cluster_cap_for_day(acc.cluster, d_idx, relax=relax):
                    continue

                day_buckets[d_idx].append(acc)
                day_cluster_counts[d_idx][acc.cluster] = curr_c + 1
                if acc.shared_endpoint and acc.endpoint:
                    endpoint_assigned_days.setdefault(acc.endpoint, []).append(d_idx)
                placed = True
                break
            if placed:
                break
        if not placed:
            unassigned.append(acc)

    # Fallback placement for any overflow accounts (preserving min_d probation cool-off & shared_endpoint gap)
    min_gap = 2 if cycle_days >= 4 else 1

    def _can_place_in_day(a: ScheduledAccount, d: int, *, strict: bool = True) -> bool:
        if d < _min_day_for_account(a):
            return False
        if (
            a.shared_endpoint
            and a.endpoint in endpoint_assigned_days
            and any(
                abs(d - prev_d) < min_gap
                for prev_d in endpoint_assigned_days[a.endpoint]
            )
        ):
            return False
        return not (strict and day_cluster_counts[d].get(a.cluster, 0) >= max_pairs_per_day[d])

    for acc in unassigned:
        min_d = _min_day_for_account(acc)

        # 1. If any day with an open slot directly satisfies all constraints for `acc`, place it there directly
        direct_d = next(
            (
                d
                for d in range(min_d, cycle_days)
                if len(day_buckets[d]) < quotas[d] and _can_place_in_day(acc, d, strict=True)
            ),
            None,
        )
        if direct_d is not None:
            day_buckets[direct_d].append(acc)
            day_cluster_counts[direct_d][acc.cluster] = (
                day_cluster_counts[direct_d].get(acc.cluster, 0) + 1
            )
            if acc.shared_endpoint and acc.endpoint:
                endpoint_assigned_days.setdefault(acc.endpoint, []).append(direct_d)
            continue

        # 2. Otherwise find the open-slot day `target_d` and swap `acc` with the closest-rest eligible candidate in `other_d >= min_d`
        target_d = next(
            (d for d in range(min_d, cycle_days) if len(day_buckets[d]) < quotas[d]),
            next(
                (d for d in range(cycle_days - 1, -1, -1) if len(day_buckets[d]) < quotas[d]),
                cycle_days - 1,
            ),
        )
        swapped = False
        for strict_cap in (True, False):
            # Search from latest day down to min_d so we never disturb Day 0 unless necessary
            for other_d in range(cycle_days - 1, min_d - 1, -1):
                if other_d == target_d:
                    continue
                if (
                    acc.shared_endpoint
                    and acc.endpoint in endpoint_assigned_days
                    and any(
                        abs(other_d - prev_d) < min_gap
                        for prev_d in endpoint_assigned_days[acc.endpoint]
                    )
                ):
                    continue
                # Sort candidates in other_d by closest rest_hours to acc
                cand_indices = sorted(
                    range(len(day_buckets[other_d])),
                    key=lambda idx: abs(day_buckets[other_d][idx].rest_hours - acc.rest_hours),
                )
                for idx_cand in cand_indices:
                    cand = day_buckets[other_d][idx_cand]
                    if cand.shared_endpoint or _min_day_for_account(cand) > target_d:
                        continue
                    new_other_c = (
                        day_cluster_counts[other_d].get(acc.cluster, 0)
                        - (1 if cand.cluster == acc.cluster else 0)
                        + 1
                    )
                    new_target_c = day_cluster_counts[target_d].get(cand.cluster, 0) + 1
                    if strict_cap and (
                        new_other_c > max_pairs_per_day[other_d]
                        or new_target_c > max_pairs_per_day[target_d]
                    ):
                        continue
                    day_buckets[other_d][idx_cand] = acc
                    day_cluster_counts[other_d][cand.cluster] -= 1
                    day_cluster_counts[other_d][acc.cluster] = (
                        day_cluster_counts[other_d].get(acc.cluster, 0) + 1
                    )
                    day_buckets[target_d].append(cand)
                    day_cluster_counts[target_d][cand.cluster] = (
                        day_cluster_counts[target_d].get(cand.cluster, 0) + 1
                    )
                    if acc.shared_endpoint and acc.endpoint:
                        endpoint_assigned_days.setdefault(acc.endpoint, []).append(other_d)
                    swapped = True
                    break
                if swapped:
                    break
            if swapped:
                break
        if not swapped:
            day_buckets[target_d].append(acc)
            day_cluster_counts[target_d][acc.cluster] = (
                day_cluster_counts[target_d].get(acc.cluster, 0) + 1
            )
            if acc.shared_endpoint and acc.endpoint:
                endpoint_assigned_days.setdefault(acc.endpoint, []).append(target_d)

    # 4b. Rebalance any non-gateway /64 cluster (e.g. usa109) that exceeded its strict even cap
    # (`relax=0`, typically <= 2 per day) by swapping with an earlier/other day that has room.
    for d_over in range(cycle_days - 1, -1, -1):
        for c_over in list(day_cluster_counts[d_over].keys()):
            if not c_over or c_over in gateway_day_targets:
                continue
            cap_over = _cluster_cap_for_day(c_over, d_over, relax=0)
            while day_cluster_counts[d_over].get(c_over, 0) > cap_over:
                swapped_c = False
                over_indices = [
                    i
                    for i, a in enumerate(day_buckets[d_over])
                    if a.cluster == c_over and not a.shared_endpoint and not a.completed_today
                ]
                over_indices.sort(key=lambda i: -day_buckets[d_over][i].rest_hours)
                for idx_a in over_indices:
                    acc_over = day_buckets[d_over][idx_a]
                    for d_under in range(cycle_days - 1, -1, -1):
                        if d_under == d_over:
                            continue
                        if day_cluster_counts[d_under].get(c_over, 0) >= _cluster_cap_for_day(
                            c_over, d_under, relax=0
                        ):
                            continue
                        cand_indices = [
                            j
                            for j, b in enumerate(day_buckets[d_under])
                            if b.cluster != c_over
                            and b.cluster not in gateway_day_targets
                            and not b.shared_endpoint
                            and not b.completed_today
                            and day_cluster_counts[d_over].get(b.cluster, 0)
                            < _cluster_cap_for_day(b.cluster, d_over, relax=0)
                        ]
                        cand_indices.sort(
                            key=lambda j: (
                                day_buckets[d_under][j].rest_hours
                                if d_under < d_over
                                else -day_buckets[d_under][j].rest_hours
                            )
                        )
                        if cand_indices:
                            idx_b = cand_indices[0]
                            acc_under = day_buckets[d_under][idx_b]
                            day_buckets[d_over][idx_a] = acc_under
                            day_buckets[d_under][idx_b] = acc_over
                            day_cluster_counts[d_over][c_over] -= 1
                            day_cluster_counts[d_over][acc_under.cluster] = (
                                day_cluster_counts[d_over].get(acc_under.cluster, 0) + 1
                            )
                            day_cluster_counts[d_under][acc_under.cluster] -= 1
                            day_cluster_counts[d_under][c_over] = (
                                day_cluster_counts[d_under].get(c_over, 0) + 1
                            )
                            swapped_c = True
                            break
                    if swapped_c:
                        break
                if not swapped_c:
                    break

    # 4c. Intra-cluster monotonic rest order: within every cluster, ensure accounts with longer
    # rest_hours are always scheduled in earlier days and accounts with shorter rest_hours in later days.
    clusters_seen = {a.cluster for d in range(cycle_days) for a in day_buckets[d] if a.cluster}
    for c_name in clusters_seen:
        positions: list[tuple[int, int]] = []
        c_accs: list[ScheduledAccount] = []
        for d_idx in range(cycle_days):
            for s_idx, a in enumerate(day_buckets[d_idx]):
                if a.cluster == c_name and not a.completed_today and not a.shared_endpoint:
                    positions.append((d_idx, s_idx))
                    c_accs.append(a)
        if len(c_accs) > 1:
            c_accs.sort(key=lambda x: (-int(x.rest_hours // 3.0), -x.rest_hours))
            for (d_idx, s_idx), a_sorted in zip(positions, c_accs, strict=False):
                day_buckets[d_idx][s_idx] = a_sorted

    # 4d. Cross-cluster monotonic rest refinement: if an account in a later day has rested significantly
    # longer than an account in an earlier day, swap them whenever cluster caps and min_day allow.
    for _pass in range(3):
        any_swapped = False
        for d_early in range(cycle_days - 1):
            for d_late in range(cycle_days - 1, d_early, -1):
                for i_e, a_e in enumerate(day_buckets[d_early]):
                    if a_e.completed_today or a_e.shared_endpoint:
                        continue
                    for i_l, a_l in enumerate(day_buckets[d_late]):
                        if a_l.completed_today or a_l.shared_endpoint:
                            continue
                        if a_l.rest_hours <= a_e.rest_hours + 4.0:
                            continue
                        if d_early < _min_day_for_account(a_l) or d_late < _min_day_for_account(a_e):
                            continue
                        if a_e.cluster != a_l.cluster:
                            if a_e.cluster in gateway_day_targets or a_l.cluster in gateway_day_targets:
                                continue
                            c_l_in_early = day_cluster_counts[d_early].get(a_l.cluster, 0) + 1
                            c_e_in_late = day_cluster_counts[d_late].get(a_e.cluster, 0) + 1
                            if c_l_in_early > _cluster_cap_for_day(a_l.cluster, d_early, relax=0):
                                continue
                            if c_e_in_late > _cluster_cap_for_day(a_e.cluster, d_late, relax=0):
                                continue
                            day_cluster_counts[d_early][a_e.cluster] -= 1
                            day_cluster_counts[d_early][a_l.cluster] = c_l_in_early
                            day_cluster_counts[d_late][a_l.cluster] -= 1
                            day_cluster_counts[d_late][a_e.cluster] = c_e_in_late
                        day_buckets[d_early][i_e] = a_l
                        day_buckets[d_late][i_l] = a_e
                        a_e = a_l
                        any_swapped = True
        if not any_swapped:
            break

    # 5. Pair accounts within each day and order Morning / Evening shifts
    days_out: list[ScheduledDay] = []
    collision_total = 0
    adj_overlap_total = 0
    repeated_pair_total = 0

    for d_idx in range(cycle_days):
        day_dt = now_dt + dt.timedelta(days=d_idx)
        wd = day_dt.weekday()  # 0=Mon .. 6=Sun
        raw_pairs = _pair_day_accounts(
            day_buckets[d_idx],
            prev_pair_set,
            seed_int + d_idx * 31,
        )
        m_pairs, e_pairs = _split_and_order_day_shifts(
            raw_pairs,
            prev_shifts,
            day_index=d_idx,
        )
        for pr in [*m_pairs, *e_pairs]:
            if pr.has_cluster_collision:
                collision_total += 1
            if pr.has_adjacent_overlap:
                adj_overlap_total += 1
            if pr.second is not None:
                k = (
                    (pr.first.name, pr.second.name)
                    if pr.first.name < pr.second.name
                    else (pr.second.name, pr.first.name)
                )
                if k in prev_pair_set:
                    repeated_pair_total += 1

        days_out.append(
            ScheduledDay(
                day_index=d_idx,
                date_str=day_dt.strftime("%d/%m/%Y"),
                weekday_tag=WEEKDAY_TAGS[wd],
                weekday_name_vi=WEEKDAY_NAMES_VI[wd],
                weekday_name_en=WEEKDAY_NAMES_EN[wd],
                morning_pairs=m_pairs,
                evening_pairs=e_pairs,
            )
        )

    return SchedulePlan(
        generated_at=now_dt.strftime("%Y-%m-%d %H:%M"),
        cycle_days=cycle_days,
        total_live=len(sched_accounts),
        days=days_out,
        completed_today=completed_today_list,
        skipped_drafts=skipped_drafts,
        collision_count=collision_total,
        adjacent_overlap_count=adj_overlap_total,
        repeated_pair_count=repeated_pair_total,
    )


def format_schedule_markdown(plan: SchedulePlan) -> str:
    """Format a `SchedulePlan` into a readable Markdown summary for clipboard copy."""
    lines: list[str] = [
        f"# 📅 LỊCH NUÔI AN TOÀN ({plan.cycle_days} NGÀY — {plan.total_live} ACC LIVE)",
        f"- Tạo lúc: {plan.generated_at} · Trùng cụm cùng lúc: {plan.collision_count}",
    ]
    if plan.skipped_drafts:
        lines.append(f"- Bỏ qua ô trống chưa gắn acc: {', '.join(plan.skipped_drafts)}")
    lines.append("")

    def _fmt_acc(a: ScheduledAccount, day_idx: int) -> str:
        eff = int(round(a.rest_hours + day_idx * 24))
        label = a.name if a.name == a.email else f"{a.name} ({a.email})"
        if a.completed_today:
            return f"**{label}** (`{a.cluster}{a.port}`, ✓ Đã mở hôm nay)"
        return f"**{label}** (`{a.cluster}{a.port}`, mở ~{eff}h)"

    for d in plan.days:
        lines.append(
            f"## Ngày {d.day_index + 1} — {d.weekday_name_vi} ({d.weekday_tag}) · {d.date_str} ({d.total_accounts} acc)"
        )
        if d.morning_pairs:
            lines.append("### ☀️ Ca Sáng (08:00 – 11:30 VN)")
            for pr in d.morning_pairs:
                s1 = _fmt_acc(pr.first, d.day_index)
                s2 = f" + {_fmt_acc(pr.second, d.day_index)}" if pr.second else " *(Đi lẻ 1 máy)*"
                lines.append(f"- **Cặp {pr.pair_index}:** {s1}{s2}")
        if d.evening_pairs:
            lines.append("### 🌙 Ca Tối (19:30 – 23:00 VN)")
            for pr in d.evening_pairs:
                s1 = _fmt_acc(pr.first, d.day_index)
                s2 = f" + {_fmt_acc(pr.second, d.day_index)}" if pr.second else " *(Đi lẻ 1 máy)*"
                lines.append(f"- **Cặp {pr.pair_index}:** {s1}{s2}")
        lines.append("")
    return "\n".join(lines).strip()


@dataclass
class SquadCandidate:
    name: str
    email: str
    cluster: str
    port: str
    endpoint: str
    trust_score: int
    trust_tier: str
    rest_hours: float
    today_minutes: int
    active_days: int
    total_minutes: int
    hatch_gw: bool
    is_rising_star: bool = False
    is_conflict: bool = False


@dataclass
class WorkSquadPlan:
    mode: str  # "rotation" | "prime" | "fixed"
    target_size: int
    candidates: list[SquadCandidate] = field(default_factory=list)
    busy_clusters: dict[str, list[str]] = field(default_factory=dict)
    squad_index: int = 0
    total_squads: int = 1
    is_pinned: bool = False
    pinned_indices: set[int] = field(default_factory=set)
    all_squads: list[list[SquadCandidate]] = field(default_factory=list)


def _build_fixed_work_squad_plan(
    profiles: list[Profile],
    snap: MonitorSnapshot,
    active_set: set[str],
    busy_clusters: dict[str, list[str]],
    *,
    target_n: int,
    shuffle_seed: int,
    squad_index: int,
    pinned_squads: dict[str, list[str]] | None,
    now_dt: dt.datetime,
) -> WorkSquadPlan:
    """Partition eligible accounts into deterministic, time-invariant conflict-free /64 squads."""
    cand_by_name: dict[str, SquadCandidate] = {}
    rank_by_name: dict[str, tuple[int, int, float, str]] = {}

    for p in profiles:
        grp = (p.group or "").strip().lower()
        pgs_lower = [pg.strip().lower() for pg in (p.pages or [])]
        if grp == "disconnected" or "disconnected" in pgs_lower:
            continue
        if not (p.proxy or "").strip():
            continue
        info = snap.profiles.get(p.name)
        if info is None or info.is_banned:
            continue
        if any(
            c in info.audit_codes
            for c in ("burned_proxy", "burned_ip", "duplicate_fingerprint")
        ):
            continue
        has_disk_activity = bool(
            info.hatch_gw
            or info.hatch_sess
            or info.cookie_count > 0
            or info.session_count > 0
            or info.history_pages > 0
        )
        has_account = (
            bool((p.email or "").strip())
            or has_disk_activity
            or (not p.name.strip().isdigit())
        )
        if not has_account:
            continue

        cl = extract_proxy_cluster(p.proxy)
        if not cl:
            continue

        lc = info.last_closed
        rest_h = (
            max(0.0, (now_dt - lc).total_seconds() / 3600.0)
            if lc is not None
            else 72.0
        )
        is_rising = bool(info.hatch_gw and 48 <= info.trust_score < 80)
        is_conflict = (p.name not in active_set) and (
            bool(info.has_warning)
            or bool(info.cluster_busy_by)
            or (not is_gateway_ipv6_cluster(cl) and cl in busy_clusters)
        )
        cand = SquadCandidate(
            name=p.name,
            email=(p.email or p.name).strip(),
            cluster=cl,
            port=extract_proxy_port(p.proxy),
            endpoint=extract_proxy_endpoint(p.proxy),
            trust_score=info.trust_score,
            trust_tier=info.trust_tier,
            rest_hours=rest_h,
            today_minutes=info.today_minutes,
            active_days=info.active_days,
            total_minutes=info.total_minutes,
            hatch_gw=info.hatch_gw,
            is_rising_star=is_rising,
            is_conflict=is_conflict,
        )
        cand_by_name[p.name] = cand

        gw_rank = 0 if info.hatch_gw else (1 if info.hatch_sess else 2)
        tier_rank = (
            0
            if info.trust_tier == "diamond"
            else (1 if info.trust_tier == "trusted" else 2)
        )
        jitter = _jitter_hash(p.name, shuffle_seed + 19) if shuffle_seed > 0 else 0.0
        rank_by_name[p.name] = (gw_rank, tier_rank, jitter, p.name.casefold())

    pinned_by_idx: dict[int, list[SquadCandidate]] = {}
    used_names: set[str] = set()
    prefix = f"{target_n}:"
    for raw_k, member_names in sorted((pinned_squads or {}).items()):
        if not isinstance(raw_k, str) or not raw_k.startswith(prefix):
            continue
        with contextlib.suppress(ValueError):
            s_idx = int(raw_k.split(":", 1)[1])
            if s_idx < 0:
                continue
            sq_cands: list[SquadCandidate] = []
            seen_cls: set[str] = set()
            seen_eps: set[str] = set()
            for m_name in member_names:
                c = cand_by_name.get(m_name)
                if c is None or m_name in used_names:
                    continue
                if not is_gateway_ipv6_cluster(c.cluster) and c.cluster in seen_cls:
                    continue
                if c.endpoint and c.endpoint in seen_eps:
                    continue
                seen_cls.add(c.cluster)
                if c.endpoint:
                    seen_eps.add(c.endpoint)
                sq_cands.append(c)
                used_names.add(m_name)
            if sq_cands:
                pinned_by_idx[s_idx] = sq_cands[:target_n]

    # Group remaining profiles by cluster; place primary unique endpoints ahead of shared-port siblings
    raw_by_cluster: dict[str, list[SquadCandidate]] = {}
    for name, cand in cand_by_name.items():
        if name not in used_names:
            raw_by_cluster.setdefault(cand.cluster, []).append(cand)

    by_cluster: dict[str, list[SquadCandidate]] = {}
    for cl, items in raw_by_cluster.items():
        items.sort(key=lambda c: rank_by_name[c.name])
        primary_ep: list[SquadCandidate] = []
        secondary_ep: list[SquadCandidate] = []
        seen_ep: set[str] = set()
        for c in items:
            if c.endpoint and c.endpoint in seen_ep:
                secondary_ep.append(c)
            else:
                if c.endpoint:
                    seen_ep.add(c.endpoint)
                primary_ep.append(c)
        by_cluster[cl] = primary_ep + secondary_ep

    def _fill_squad_slots(existing: list[SquadCandidate]) -> list[SquadCandidate]:
        sq = list(existing)
        used_cls = {c.cluster for c in sq}
        used_eps = {c.endpoint for c in sq if c.endpoint}
        rem_gw = sum(len(v) for k, v in by_cluster.items() if is_gateway_ipv6_cluster(k))
        rem_usa = sum(len(v) for k, v in by_cluster.items() if not is_gateway_ipv6_cluster(k))
        avail_usa_cls = sum(
            1 for k, v in by_cluster.items() if v and not is_gateway_ipv6_cluster(k)
        )
        need_second_gw = (target_n > 2) and (
            rem_gw >= rem_usa or avail_usa_cls < target_n - 1
        )
        max_gw = (
            min(3, max(2, (target_n + 1) // 2))
            if need_second_gw
            else (1 if target_n <= 6 else min(3, max(1, (target_n + 2) // 3)))
        )
        gw_counts: dict[str, int] = {}
        for c in sq:
            if is_gateway_ipv6_cluster(c.cluster):
                gw_counts[c.cluster] = gw_counts.get(c.cluster, 0) + 1

        while len(sq) < target_n:
            avail_cls = []
            for cl, pool in by_cluster.items():
                if not pool:
                    continue
                is_gw = is_gateway_ipv6_cluster(cl)
                if not is_gw and cl in used_cls:
                    continue
                if is_gw and gw_counts.get(cl, 0) >= max_gw:
                    continue
                # Find first candidate in pool whose endpoint is not in used_eps
                valid_idx = next(
                    (
                        i
                        for i, cand in enumerate(pool)
                        if not cand.endpoint or cand.endpoint not in used_eps
                    ),
                    -1,
                )
                if valid_idx >= 0:
                    # Prefer distinct non-gateway clusters unless gateway pool is >= remaining non-gateway pool
                    gw_slot_pen = (
                        0
                        if (is_gw and gw_counts.get(cl, 0) <= 1 and need_second_gw)
                        else (gw_counts.get(cl, 0) * 100)
                    )
                    avail_cls.append(
                        (
                            gw_slot_pen,
                            -len(pool),
                            rank_by_name[pool[valid_idx].name],
                            cl,
                            valid_idx,
                        )
                    )
            if not avail_cls:
                break
            avail_cls.sort()
            _, _, _, best_cl, pick_idx = avail_cls[0]
            chosen = by_cluster[best_cl].pop(pick_idx)
            sq.append(chosen)
            used_cls.add(best_cl)
            if chosen.endpoint:
                used_eps.add(chosen.endpoint)
            if is_gateway_ipv6_cluster(best_cl):
                gw_counts[best_cl] = gw_counts.get(best_cl, 0) + 1
        return sq

    # Top up any partial pinned squads first
    for s_idx in sorted(pinned_by_idx):
        if len(pinned_by_idx[s_idx]) < target_n:
            pinned_by_idx[s_idx] = _fill_squad_slots(pinned_by_idx[s_idx])

    # Build remaining squads deterministically until all eligible accounts are placed
    auto_squads: list[list[SquadCandidate]] = []
    while True:
        sq = _fill_squad_slots([])
        if not sq:
            break
        auto_squads.append(sq)

    total_slots = max(
        len(auto_squads) + len(pinned_by_idx),
        (max(pinned_by_idx) + 1) if pinned_by_idx else 0,
    )
    all_squads: list[list[SquadCandidate]] = []
    pinned_final_indices: set[int] = set()
    auto_iter = iter(auto_squads)
    for slot_idx in range(total_slots):
        if slot_idx in pinned_by_idx:
            pinned_final_indices.add(len(all_squads))
            all_squads.append(pinned_by_idx[slot_idx])
        else:
            nxt = next(auto_iter, None)
            if nxt is not None:
                all_squads.append(nxt)
    for rem in auto_iter:
        all_squads.append(rem)

    if not all_squads:
        return WorkSquadPlan(
            mode="fixed",
            target_size=target_n,
            candidates=[],
            busy_clusters=busy_clusters,
            squad_index=0,
            total_squads=0,
            is_pinned=False,
            pinned_indices=set(),
            all_squads=[],
        )

    eff_idx = max(0, min(len(all_squads) - 1, int(squad_index)))
    return WorkSquadPlan(
        mode="fixed",
        target_size=target_n,
        candidates=all_squads[eff_idx],
        busy_clusters=busy_clusters,
        squad_index=eff_idx,
        total_squads=len(all_squads),
        is_pinned=(eff_idx in pinned_final_indices),
        pinned_indices=pinned_final_indices,
        all_squads=all_squads,
    )


def select_parallel_work_squad(
    profiles: list[Profile],
    snap: MonitorSnapshot,
    running_names: set[str] | None = None,
    *,
    count: int = 4,
    mode: str = "rotation",
    shuffle_seed: int = 0,
    squad_index: int = 0,
    pinned_squads: dict[str, list[str]] | None = None,
    now: dt.datetime | None = None,
) -> WorkSquadPlan:
    """Select `count` conflict-free profiles across distinct IPv6 /64 clusters for parallel work."""
    now_dt = now or dt.datetime.now()
    active_set = set(running_names or ())
    target_n = max(1, min(12, int(count)))

    busy_clusters: dict[str, list[str]] = {}
    for p in profiles:
        if p.name not in active_set:
            continue
        cl = extract_proxy_cluster(p.proxy)
        if cl:
            busy_clusters.setdefault(cl, []).append(p.name)

    if mode == "fixed":
        return _build_fixed_work_squad_plan(
            profiles,
            snap,
            active_set,
            busy_clusters,
            target_n=target_n,
            shuffle_seed=shuffle_seed,
            squad_index=squad_index,
            pinned_squads=pinned_squads,
            now_dt=now_dt,
        )

    fresh_by_cluster: dict[str, list[tuple[float, SquadCandidate]]] = {}
    rested_fallback_by_cluster: dict[str, list[tuple[float, SquadCandidate]]] = {}

    for p in profiles:
        if p.name in active_set:
            continue
        grp = (p.group or "").strip().lower()
        pgs_lower = [pg.strip().lower() for pg in (p.pages or [])]
        if grp == "disconnected" or "disconnected" in pgs_lower:
            continue
        if not (p.proxy or "").strip():
            continue
        info = snap.profiles.get(p.name)
        if info is None or info.is_banned:
            continue
        if info.has_warning or info.cluster_busy_by:
            continue
        if any(
            c in info.audit_codes
            for c in ("burned_proxy", "burned_ip", "duplicate_fingerprint")
        ):
            continue
        has_disk_activity = bool(
            info.hatch_gw
            or info.hatch_sess
            or info.cookie_count > 0
            or info.session_count > 0
            or info.history_pages > 0
        )
        has_account = (
            bool((p.email or "").strip())
            or has_disk_activity
            or (not p.name.strip().isdigit())
        )
        if not has_account:
            continue

        cl = extract_proxy_cluster(p.proxy)
        if not cl:
            continue
        if not is_gateway_ipv6_cluster(cl) and cl in busy_clusters:
            continue

        lc = info.last_closed
        rest_h = (
            max(0.0, (now_dt - lc).total_seconds() / 3600.0)
            if lc is not None
            else 72.0
        )
        is_rising = bool(info.hatch_gw and 48 <= info.trust_score < 80)
        cand = SquadCandidate(
            name=p.name,
            email=(p.email or p.name).strip(),
            cluster=cl,
            port=extract_proxy_port(p.proxy),
            endpoint=extract_proxy_endpoint(p.proxy),
            trust_score=info.trust_score,
            trust_tier=info.trust_tier,
            rest_hours=rest_h,
            today_minutes=info.today_minutes,
            active_days=info.active_days,
            total_minutes=info.total_minutes,
            hatch_gw=info.hatch_gw,
            is_rising_star=is_rising,
        )

        # Fatigue check: avoid picking accounts that already worked today or closed recently
        min_rest_h = 18.0 if mode == "rotation" else 12.0
        is_fatigued = info.today_minutes >= 15 or rest_h < min_rest_h

        gw_bonus = 1000.0 if info.hatch_gw else (300.0 if info.hatch_sess else 0.0)
        jitter = _jitter_hash(p.name, shuffle_seed + 7) * 16.0
        if mode == "prime":
            rank_score = (
                gw_bonus
                + info.trust_score * 5.0
                + min(96.0, rest_h) * 0.6
                + jitter
            )
        else:
            # Rotation mode: prioritize well-rested accounts and rising stars so strong accounts rest safely
            rising_bonus = 40.0 if (is_rising and rest_h >= 24.0) else 0.0
            rank_score = (
                gw_bonus
                + min(120.0, rest_h) * 2.5
                + info.trust_score * 0.8
                + rising_bonus
                + jitter
            )

        bucket = rested_fallback_by_cluster if is_fatigued else fresh_by_cluster
        bucket.setdefault(cl, []).append((rank_score, cand))

    # Pick top representative(s) per cluster: 1 for each usa10X /64 node, up to 2-3 for /48 gateway
    picked_pool: list[tuple[int, float, SquadCandidate]] = []
    all_clusters = sorted(set(fresh_by_cluster) | set(rested_fallback_by_cluster))
    max_gw_take = min(3, max(1, (target_n + 2) // 3))

    for cl in all_clusters:
        is_gw = is_gateway_ipv6_cluster(cl)
        busy_cnt = len(busy_clusters.get(cl, []))
        cap = max(0, max_gw_take - busy_cnt) if is_gw else 1
        if cap <= 0:
            continue
        fresh_list = sorted(fresh_by_cluster.get(cl, []), key=lambda x: -x[0])
        fb_list = sorted(rested_fallback_by_cluster.get(cl, []), key=lambda x: -x[0])
        chosen_for_cl: list[tuple[int, float, SquadCandidate]] = []
        seen_eps: set[str] = set()
        for tier_priority, src_list in ((0, fresh_list), (1, fb_list)):
            for sc, c_item in src_list:
                if len(chosen_for_cl) >= cap:
                    break
                if c_item.endpoint and c_item.endpoint in seen_eps:
                    continue
                if c_item.endpoint:
                    seen_eps.add(c_item.endpoint)
                # Slightly defer extra gateway slots (2nd/3rd on 209.141.40.174) behind primary /64 nodes
                slot_pen = len(chosen_for_cl) * 120.0
                picked_pool.append((tier_priority, -(sc - slot_pen), c_item))
                chosen_for_cl.append((tier_priority, sc, c_item))

    picked_pool.sort(key=lambda item: (item[0], item[1], item[2].name.casefold()))
    final_candidates = [item[2] for item in picked_pool[:target_n]]

    return WorkSquadPlan(
        mode=mode,
        target_size=target_n,
        candidates=final_candidates,
        busy_clusters=busy_clusters,
    )


