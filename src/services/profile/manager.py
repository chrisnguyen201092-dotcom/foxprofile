import contextlib
import csv
import json
import os
import pathlib
import shutil
import threading
import time
from datetime import datetime

from ...core.config import DATA_DIR, PROFILES_FILE
from ...core.logging import get_logger
from ...models.profile import (
    ENGINES,
    Profile,
    default_status_tags,
    has_ban_page,
    is_ban_status,
    parse_flexible_date,
    set_active_status_tags,
    today_ymd,
)
from ...services.monitor import extract_proxy_port
from ...utils.proxy_parser import normalize_proxy
from ...utils.totp import normalize_totp_secret
from ...utils.validation import (
    valid_or_none,
    validate_color,
    validate_email,
    validate_group,
    validate_locale,
    validate_notes,
    validate_password,
    validate_profile_name,
    validate_proxy_format,
    validate_status,
    validate_timezone,
    validate_two_fa,
)
from .transfer import export_to_zip, import_from_zip, read_profile_meta

logger = get_logger("profile.manager")
_UNSET = object()


def _engine_or_default(value: object) -> str:
    return value if value in ENGINES else "camoufox"


def _clean_pages(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            s = item.strip()
            if s and validate_group(s)[0] and s.casefold() not in seen:
                seen.add(s.casefold())
                result.append(s)
    return result


def _replace(src: pathlib.Path, dst: pathlib.Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows it fails with a sharing violation
    while another process (antivirus, indexer, a reader) has the target open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


class ProfileManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.profiles: dict[str, Profile] = {}
        self.custom_pages: list[str] = []
        self.all_page_index: int = 0
        self.proxy_pool: list[str] = []
        self.status_tags: list[dict[str, str]] = default_status_tags()
        set_active_status_tags(self.status_tags)
        self._load_profiles()
        pathlib.Path(DATA_DIR).mkdir(exist_ok=True, parents=True)

    @staticmethod
    def _backups_dir() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).parent / "_backups"

    def list_config_backups(self) -> list[pathlib.Path]:
        bdir = self._backups_dir()
        if not bdir.exists():
            return []
        return sorted(bdir.glob("profiles_*.json"))

    def _write_config_backup(self, serialized_profiles: str) -> None:
        with contextlib.suppress(Exception):
            bdir = self._backups_dir()
            bdir.mkdir(parents=True, exist_ok=True)
            day_tag = time.strftime("%Y%m%d")
            target = bdir / f"profiles_{day_tag}.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(serialized_profiles, encoding="utf-8")
            _replace(tmp, target)
            backups = sorted(bdir.glob("profiles_*.json"))
            for old_bak in backups[:-10]:
                with contextlib.suppress(OSError):
                    old_bak.unlink(missing_ok=True)

    @staticmethod
    def _pages_file() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).with_name("custom_pages.json")

    @staticmethod
    def _proxy_pool_file() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).with_name("proxy_pool.json")

    @staticmethod
    def _status_tags_file() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).with_name("status_tags.json")

    def _save_status_tags(self) -> None:
        st_path = self._status_tags_file()
        tmp = st_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.status_tags, indent=2), encoding="utf-8")
            _replace(tmp, st_path)
            set_active_status_tags(self.status_tags)
        except Exception as e:
            logger.exception("Error saving status tags: %s", e)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _save_proxy_pool(self) -> None:
        pool_path = self._proxy_pool_file()
        tmp = pool_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.proxy_pool, indent=2), encoding="utf-8")
            _replace(tmp, pool_path)
        except Exception as e:
            logger.exception("Error saving proxy pool: %s", e)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _trash_file() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).with_name("trash_profiles.json")

    @staticmethod
    def _trash_data_dir() -> pathlib.Path:
        return pathlib.Path(DATA_DIR) / ".trash"

    def _load_trash_dict(self) -> dict[str, dict]:
        path = self._trash_file()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_trash_dict(self, data: dict[str, dict]) -> None:
        path = self._trash_file()
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _replace(tmp, path)
        except Exception as e:
            logger.exception("Error saving trash profiles: %s", e)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _find_by_casefold(self, name: str) -> str | None:
        """Return the existing profile key that case-folds to *name*, or None.

        Used to enforce case-insensitive uniqueness across all platforms so that
        a profiles.json exported on Linux (case-sensitive FS) can be imported on
        Windows/macOS (case-insensitive FS) without data-dir collisions.
        """
        lower = name.casefold()
        return next((k for k in self.profiles if k.casefold() == lower), None)

    def _find_page_by_casefold(self, page_name: str) -> str | None:
        lower = page_name.casefold()
        return next((pg for pg in self.custom_pages if pg.casefold() == lower), None)

    def _data_path(self, name: str) -> str:
        return os.path.join(DATA_DIR, name)

    def _infer_created_at(self, name: str) -> str:
        pdir = pathlib.Path(self._data_path(name))
        if pdir.exists():
            with contextlib.suppress(OSError, ValueError):
                st = pdir.stat()
                ts = (
                    min(st.st_ctime, st.st_mtime)
                    if st.st_ctime and st.st_mtime
                    else (st.st_ctime or st.st_mtime)
                )
                if ts and ts > 0:
                    return datetime.fromtimestamp(ts).date().isoformat()
        return today_ymd()

    def _infer_banned_at(self, name: str) -> str:
        pdir = pathlib.Path(self._data_path(name))
        if pdir.exists():
            latest_ts = 0.0
            for sub in ("prefs.js", "places.sqlite"):
                f = pdir / sub
                with contextlib.suppress(OSError):
                    if f.exists():
                        latest_ts = max(latest_ts, f.stat().st_mtime)
            if latest_ts <= 0:
                with contextlib.suppress(OSError):
                    latest_ts = pdir.stat().st_mtime
            if latest_ts > 0:
                with contextlib.suppress(OSError, ValueError):
                    return datetime.fromtimestamp(latest_ts).date().isoformat()
        return today_ymd()

    @staticmethod
    def _is_profile_banned(p: Profile) -> bool:
        return is_ban_status(p.status) or has_ban_page(p.pages)

    def _sync_ban_state(
        self,
        p: Profile,
        *,
        was_banned: bool = False,
        infer_from_disk: bool = False,
    ) -> None:
        now_banned = self._is_profile_banned(p)
        if now_banned and not p.banned_at:
            p.banned_at = self._infer_banned_at(p.name) if infer_from_disk else today_ymd()
        elif was_banned and not now_banned:
            p.banned_at = None

    def _load_profiles(self) -> None:
        pages_path = self._pages_file()
        if pages_path.exists():
            with contextlib.suppress(Exception):
                raw_pages = json.loads(pages_path.read_text(encoding="utf-8"))
                if isinstance(raw_pages, list):
                    cleaned: list[str] = []
                    all_idx = 0
                    for item in raw_pages:
                        if isinstance(item, str) and item.strip() == "__all__":
                            all_idx = len(cleaned)
                        elif isinstance(item, str):
                            s = item.strip()
                            if (
                                s
                                and validate_group(s)[0]
                                and not any(c.casefold() == s.casefold() for c in cleaned)
                            ):
                                cleaned.append(s)
                    self.custom_pages = cleaned
                    self.all_page_index = all_idx

        pool_path = self._proxy_pool_file()
        if pool_path.exists():
            with contextlib.suppress(Exception):
                raw_pool = json.loads(pool_path.read_text(encoding="utf-8"))
                if isinstance(raw_pool, list):
                    seen_px: set[str] = set()
                    clean_pool: list[str] = []
                    for item in raw_pool:
                        if (
                            isinstance(item, str)
                            and item.strip()
                            and validate_proxy_format(item.strip())[0]
                        ):
                            norm = normalize_proxy(item.strip())
                            if norm not in seen_px:
                                seen_px.add(norm)
                                clean_pool.append(norm)
                    self.proxy_pool = clean_pool

        st_path = self._status_tags_file()
        if st_path.exists():
            with contextlib.suppress(Exception):
                raw_tags = json.loads(st_path.read_text(encoding="utf-8"))
                if isinstance(raw_tags, list):
                    clean_tags: list[dict[str, str]] = []
                    for item in raw_tags:
                        if isinstance(item, dict):
                            code = str(item.get("code") or "").strip()
                            label = str(item.get("label") or "").strip()
                            color = str(item.get("color") or "#0284C7").strip()
                            if code and validate_status(label or code)[0]:
                                if not validate_color(color)[0]:
                                    color = "#0284C7"
                                clean_tags.append({"code": code, "label": label, "color": color})
                    self.status_tags = clean_tags
                    set_active_status_tags(self.status_tags)

        if pathlib.Path(PROFILES_FILE).exists():
            try:
                with pathlib.Path(PROFILES_FILE).open(encoding="utf-8") as f:
                    data = json.load(f)
                    for name, p_data in data.items():
                        if not validate_profile_name(name)[0] or p_data.get("name") != name:
                            logger.warning("Skipping profile with unsafe name: %r", name)
                            continue
                        pages = _clean_pages(p_data.get("pages"))
                        for pg in pages:
                            if self._find_page_by_casefold(pg) is None:
                                self.custom_pages.append(pg)
                        canon_pages = [self._find_page_by_casefold(pg) or pg for pg in pages]
                        status_val = valid_or_none(p_data.get("status"), validate_status)
                        created_val = parse_flexible_date(p_data.get("created_at"))
                        if not created_val:
                            created_val = self._infer_created_at(name)
                        banned_val = parse_flexible_date(p_data.get("banned_at"))
                        if not banned_val and (
                            is_ban_status(status_val) or has_ban_page(canon_pages)
                        ):
                            banned_val = self._infer_banned_at(name)
                        clean_data = {
                            "name": p_data.get("name"),
                            "proxy": p_data.get("proxy"),
                            "os_type": p_data.get(
                                "os_type",
                                p_data.get("config", {}).get("os", "windows"),
                            ),
                            "timezone": valid_or_none(p_data.get("timezone"), validate_timezone),
                            "locale": valid_or_none(p_data.get("locale"), validate_locale),
                            "engine": _engine_or_default(p_data.get("engine")),
                            "color": valid_or_none(p_data.get("color"), validate_color),
                            "group": valid_or_none(p_data.get("group"), validate_group),
                            "notes": valid_or_none(p_data.get("notes"), validate_notes),
                            "pages": canon_pages,
                            "status": status_val,
                            "pinned": bool(p_data.get("pinned", False)),
                            "created_at": created_val,
                            "banned_at": banned_val,
                            "email": valid_or_none(p_data.get("email"), validate_email),
                            "password": valid_or_none(p_data.get("password"), validate_password),
                            "two_fa": normalize_totp_secret(
                                valid_or_none(p_data.get("two_fa"), validate_two_fa)
                            )
                            or None,
                        }
                        self.profiles[name] = Profile(**clean_data)
                logger.info("Loaded %d profiles", len(self.profiles))
            except Exception as e:
                logger.exception("Error loading profiles: %s", e)

    def save_profiles(self) -> None:
        """Atomic write: write to a temp file then os.replace."""
        path = pathlib.Path(PROFILES_FILE)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(
                    {name: p.to_dict() for name, p in self.profiles.items()},
                    indent=4,
                ),
                encoding="utf-8",
            )
            _replace(tmp, path)
            pages_path = self._pages_file()
            if self.custom_pages or self.all_page_index > 0 or pages_path.exists():
                idx = max(0, min(self.all_page_index, len(self.custom_pages)))
                serialized_pages = (
                    [*self.custom_pages[:idx], "__all__", *self.custom_pages[idx:]]
                    if idx > 0
                    else list(self.custom_pages)
                )
                pages_tmp = pages_path.with_suffix(".json.tmp")
                pages_tmp.write_text(
                    json.dumps(serialized_pages, indent=2),
                    encoding="utf-8",
                )
                _replace(pages_tmp, pages_path)
            logger.debug("Profiles saved")
            if path.exists():
                self._write_config_backup(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.exception("Error saving profiles: %s", e)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _register_pages(self, pages: list[str] | None) -> list[str]:
        cleaned = _clean_pages(pages or [])
        canonical: list[str] = []
        for pg in cleaned:
            existing = self._find_page_by_casefold(pg)
            if existing is None:
                self.custom_pages.append(pg)
                canonical.append(pg)
            else:
                canonical.append(existing)
        return canonical

    def add_profile(
        self,
        name: str,
        proxy: str,
        os_type: str,
        timezone: str | None = None,
        locale: str | None = None,
        engine: str = "camoufox",
        color: str | None = None,
        group: str | None = None,
        notes: str | None = None,
        pages: list[str] | None = None,
        status: str | None = None,
        pinned: bool = False,
        email: str | None = None,
        password: str | None = None,
        two_fa: str | None = None,
    ) -> bool:
        with self._lock:
            if self._find_by_casefold(name) is not None:
                return False
            # Create directory before touching profiles.json so a mkdir failure
            # does not leave an entry with no data dir on disk.
            try:
                pathlib.Path(self._data_path(name)).mkdir(exist_ok=True, parents=True)
            except OSError as e:
                logger.warning("Could not create data dir for %s: %s", name, e)
                return False
            canon_pages = self._register_pages(pages)
            clean_status = valid_or_none(status, validate_status)
            now_ymd = today_ymd()
            self.profiles[name] = Profile(
                name=name,
                proxy=proxy or None,
                os_type=os_type,
                timezone=timezone or None,
                locale=locale or None,
                engine=engine,
                color=color or None,
                group=group or None,
                notes=notes or None,
                pages=canon_pages,
                status=clean_status,
                pinned=bool(pinned),
                created_at=now_ymd,
                banned_at=now_ymd
                if (is_ban_status(clean_status) or has_ban_page(canon_pages))
                else None,
                email=valid_or_none(email, validate_email),
                password=valid_or_none(password, validate_password),
                two_fa=normalize_totp_secret(valid_or_none(two_fa, validate_two_fa)) or None,
            )
            self.save_profiles()
            logger.info("Created profile: %s", name)
            return True

    def update_profile(
        self,
        original_name: str,
        new_name: str,
        new_proxy: str,
        new_os: str,
        new_timezone: str | None = None,
        new_locale: str | None = None,
        new_color: str | None = None,
        new_group: str | None = None,
        new_notes: str | None = None,
        new_pages: list[str] | None = None,
        new_status: object = _UNSET,
        new_pinned: bool | None = None,
        new_email: object = _UNSET,
        new_password: object = _UNSET,
        new_two_fa: object = _UNSET,
    ) -> bool:
        """Replace every field; a None/empty timezone or locale means automatic."""
        with self._lock:
            if original_name not in self.profiles:
                return False

            if new_name != original_name:
                # Case-insensitive uniqueness: a case-only rename of the profile
                # itself is allowed; renaming to another profile's casefold is not.
                conflicting = self._find_by_casefold(new_name)
                if conflicting is not None and conflicting != original_name:
                    return False

            # Rename the data directory BEFORE mutating in-memory state so that
            # an OSError leaves memory and JSON untouched.
            if new_name != original_name:
                old_dir = pathlib.Path(self._data_path(original_name))
                new_dir = pathlib.Path(self._data_path(new_name))
                if old_dir.exists():
                    if new_dir.exists():
                        # On case-insensitive filesystems (Windows/macOS) a
                        # case-only rename sees the target as existing because
                        # both paths point to the same directory entry.  Allow
                        # the rename only in that situation.
                        try:
                            same = old_dir.samefile(new_dir)
                        except OSError:
                            same = False
                        if not same:
                            # Refuse: do NOT delete potential user data.
                            logger.warning(
                                "Cannot rename %s -> %s: target data dir already exists",
                                original_name,
                                new_name,
                            )
                            return False
                    try:
                        old_dir.rename(new_dir)
                    except OSError as e:
                        logger.warning("Could not rename data dir for %s: %s", original_name, e)
                        return False

            profile = self.profiles[original_name]
            was_banned = self._is_profile_banned(profile)
            profile.name = new_name
            profile.proxy = new_proxy or None
            profile.os_type = new_os
            profile.timezone = new_timezone or None
            profile.locale = new_locale or None
            profile.color = new_color or None
            profile.group = new_group or None
            profile.notes = new_notes or None
            if new_pages is not None:
                profile.pages = self._register_pages(new_pages)
            if new_status is not _UNSET:
                profile.status = valid_or_none(
                    new_status if isinstance(new_status, str) else None,
                    validate_status,
                )
            if new_pinned is not None:
                profile.pinned = bool(new_pinned)
            if new_email is not _UNSET:
                profile.email = valid_or_none(
                    new_email if isinstance(new_email, str) else None,
                    validate_email,
                )
            if new_password is not _UNSET:
                profile.password = valid_or_none(
                    new_password if isinstance(new_password, str) else None,
                    validate_password,
                )
            if new_two_fa is not _UNSET:
                raw_2fa = valid_or_none(
                    new_two_fa if isinstance(new_two_fa, str) else None,
                    validate_two_fa,
                )
                profile.two_fa = normalize_totp_secret(raw_2fa) or None
            if not profile.created_at:
                profile.created_at = self._infer_created_at(new_name)
            self._sync_ban_state(profile, was_banned=was_banned)

            if new_name != original_name:
                self.profiles = {
                    (new_name if k == original_name else k): (profile if k == original_name else v)
                    for k, v in self.profiles.items()
                }

            self.save_profiles()
            logger.info("Updated profile: %s -> %s", original_name, new_name)
            return True

    def list_custom_pages(self) -> list[str]:
        with self._lock:
            return list(self.custom_pages)

    def save_custom_page(
        self,
        page_name: str,
        profile_names: list[str] | None = None,
        original_name: str | None = None,
    ) -> bool:
        """Create or update a custom page and optionally set its member profiles."""
        clean_name = page_name.strip()
        if not clean_name or not validate_group(clean_name)[0]:
            return False
        with self._lock:
            existing = self._find_page_by_casefold(clean_name)
            if original_name is not None:
                orig_match = self._find_page_by_casefold(original_name)
                if orig_match is None:
                    return False
                if existing is not None and existing != orig_match:
                    return False
                self.custom_pages = [
                    clean_name if pg == orig_match else pg for pg in self.custom_pages
                ]
                for p in self.profiles.values():
                    p.pages = [clean_name if pg == orig_match else pg for pg in p.pages]
                target_page = clean_name
            else:
                if existing is not None:
                    return False
                self.custom_pages.append(clean_name)
                target_page = clean_name

            if profile_names is not None:
                chosen = set(profile_names)
                for p in self.profiles.values():
                    was_banned = self._is_profile_banned(p)
                    has_page = any(pg.casefold() == target_page.casefold() for pg in p.pages)
                    if p.name in chosen and not has_page:
                        p.pages.append(target_page)
                        self._sync_ban_state(p, was_banned=was_banned)
                    elif p.name not in chosen and has_page:
                        p.pages = [pg for pg in p.pages if pg.casefold() != target_page.casefold()]
                        self._sync_ban_state(p, was_banned=was_banned)

            self.save_profiles()
            return True

    def delete_custom_page(self, page_name: str) -> bool:
        with self._lock:
            match = self._find_page_by_casefold(page_name.strip())
            if match is None:
                return False
            self.custom_pages = [pg for pg in self.custom_pages if pg != match]
            for p in self.profiles.values():
                was_banned = self._is_profile_banned(p)
                p.pages = [pg for pg in p.pages if pg.casefold() != match.casefold()]
                self._sync_ban_state(p, was_banned=was_banned)
            self.save_profiles()
            return True

    def add_profiles_to_page(self, page_name: str, profile_names: list[str]) -> bool:
        clean_name = page_name.strip()
        if not clean_name or not validate_group(clean_name)[0]:
            return False
        with self._lock:
            target = self._find_page_by_casefold(clean_name)
            if target is None:
                self.custom_pages.append(clean_name)
                target = clean_name
            for name in profile_names:
                p = self.profiles.get(name)
                if p is not None and not any(pg.casefold() == target.casefold() for pg in p.pages):
                    was_banned = self._is_profile_banned(p)
                    p.pages.append(target)
                    self._sync_ban_state(p, was_banned=was_banned)
            self.save_profiles()
            return True

    def remove_profiles_from_page(self, page_name: str, profile_names: list[str]) -> bool:
        clean_name = page_name.strip()
        with self._lock:
            target = self._find_page_by_casefold(clean_name)
            if target is None:
                return False
            for name in profile_names:
                p = self.profiles.get(name)
                if p is not None:
                    was_banned = self._is_profile_banned(p)
                    p.pages = [pg for pg in p.pages if pg.casefold() != target.casefold()]
                    self._sync_ban_state(p, was_banned=was_banned)
            self.save_profiles()
            return True

    def reorder_custom_pages(self, ordered_pages: list[str]) -> bool:
        """Reorder custom pages (and optional '__all__' slot) according to *ordered_pages*."""
        with self._lock:
            seen: set[str] = set()
            new_order: list[str] = []
            new_all_idx: int | None = None
            for raw in ordered_pages:
                s = raw.strip()
                if s == "__all__":
                    if new_all_idx is None:
                        new_all_idx = len(new_order)
                    continue
                match = self._find_page_by_casefold(s)
                if match is not None and match.casefold() not in seen:
                    seen.add(match.casefold())
                    new_order.append(match)
            for pg in self.custom_pages:
                if pg.casefold() not in seen:
                    new_order.append(pg)
            self.custom_pages = new_order
            if new_all_idx is not None:
                self.all_page_index = max(0, min(new_all_idx, len(self.custom_pages)))
            else:
                self.all_page_index = max(0, min(self.all_page_index, len(self.custom_pages)))
            self.save_profiles()
            return True

    def move_profile(self, name: str, direction: int) -> bool:
        """Move a profile up (direction < 0) or down (direction > 0) in custom order."""
        with self._lock:
            if name not in self.profiles or direction == 0:
                return False
            keys = list(self.profiles.keys())
            idx = keys.index(name)
            new_idx = idx + (-1 if direction < 0 else 1)
            if not 0 <= new_idx < len(keys):
                return False
            keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
            self.profiles = {k: self.profiles[k] for k in keys}
            self.save_profiles()
            return True

    def reorder_profiles(self, names: list[str]) -> bool:
        """Reorder profiles according to *names*; unlisted profiles stay at the end."""
        with self._lock:
            seen: set[str] = set()
            ordered: list[str] = []
            for n in names:
                if n in self.profiles and n not in seen:
                    seen.add(n)
                    ordered.append(n)
            for k in self.profiles:
                if k not in seen:
                    ordered.append(k)
            self.profiles = {k: self.profiles[k] for k in ordered}
            self.save_profiles()
            return True

    def delete_profile(self, name: str) -> bool:
        with self._lock:
            if name not in self.profiles:
                return False
            del self.profiles[name]
            self.save_profiles()
            # Inside the lock: a profile re-created under the same (or, on a
            # case-insensitive file system, a case-variant) name must not have
            # its fresh data dir removed by this delete.
            shutil.rmtree(self._data_path(name), ignore_errors=True)
        logger.info("Deleted profile: %s", name)
        return True

    def clone_profile(
        self,
        source_name: str,
        new_name: str,
        copy_proxy: bool = True,
    ) -> bool:
        clean_new = new_name.strip()
        if not validate_profile_name(clean_new)[0]:
            return False
        with self._lock:
            src = self.profiles.get(source_name)
            if src is None:
                return False
            return self.add_profile(
                name=clean_new,
                proxy=(src.proxy or "") if copy_proxy else "",
                os_type=src.os_type,
                timezone=src.timezone,
                locale=src.locale,
                engine=src.engine,
                color=src.color,
                group=src.group,
                notes=src.notes,
                pages=list(src.pages),
                status=src.status,
                email=src.email,
                password=src.password,
                two_fa=src.two_fa,
            )

    def trash_profile(self, name: str) -> bool:
        with self._lock:
            if name not in self.profiles:
                return False
            profile = self.profiles.pop(name)
            self.save_profiles()
            trash_data = self._load_trash_dict()
            entry = profile.to_dict()
            entry["deleted_at"] = int(time.time())
            trash_data[name] = entry
            self._save_trash_dict(trash_data)

            src_dir = pathlib.Path(self._data_path(name))
            if src_dir.exists():
                trash_root = self._trash_data_dir()
                trash_root.mkdir(exist_ok=True, parents=True)
                dst_dir = trash_root / name
                if dst_dir.exists():
                    shutil.rmtree(dst_dir, ignore_errors=True)
                try:
                    shutil.move(str(src_dir), str(dst_dir))
                except OSError:
                    shutil.rmtree(src_dir, ignore_errors=True)
        logger.info("Moved profile to trash: %s", name)
        return True

    def restore_profile(self, name: str) -> bool:
        with self._lock:
            trash_data = self._load_trash_dict()
            key = next(
                (k for k in trash_data if k == name or k.casefold() == name.casefold()), None
            )
            if key is None:
                return False
            p_data = trash_data.pop(key)
            target_name = key
            if self._find_by_casefold(target_name) is not None:
                idx = 1
                while True:
                    candidate = f"{key} ({idx})"
                    if (
                        validate_profile_name(candidate)[0]
                        and self._find_by_casefold(candidate) is None
                    ):
                        target_name = candidate
                        break
                    idx += 1

            src_trash_dir = self._trash_data_dir() / key
            dst_dir = pathlib.Path(self._data_path(target_name))
            if src_trash_dir.exists():
                if dst_dir.exists():
                    shutil.rmtree(dst_dir, ignore_errors=True)
                try:
                    shutil.move(str(src_trash_dir), str(dst_dir))
                except OSError:
                    dst_dir.mkdir(exist_ok=True, parents=True)
            else:
                dst_dir.mkdir(exist_ok=True, parents=True)

            pages = self._register_pages(p_data.get("pages"))
            status_val = valid_or_none(p_data.get("status"), validate_status)
            created_val = parse_flexible_date(p_data.get("created_at")) or self._infer_created_at(
                target_name
            )
            banned_val = parse_flexible_date(p_data.get("banned_at"))
            if not banned_val and (is_ban_status(status_val) or has_ban_page(pages)):
                banned_val = self._infer_banned_at(target_name)
            self.profiles[target_name] = Profile(
                name=target_name,
                proxy=p_data.get("proxy") or None,
                os_type=p_data.get("os_type", "windows"),
                timezone=valid_or_none(p_data.get("timezone"), validate_timezone),
                locale=valid_or_none(p_data.get("locale"), validate_locale),
                engine=_engine_or_default(p_data.get("engine")),
                color=valid_or_none(p_data.get("color"), validate_color),
                group=valid_or_none(p_data.get("group"), validate_group),
                notes=valid_or_none(p_data.get("notes"), validate_notes),
                pages=pages,
                status=status_val,
                pinned=bool(p_data.get("pinned", False)),
                created_at=created_val,
                banned_at=banned_val,
                email=valid_or_none(p_data.get("email"), validate_email),
                password=valid_or_none(p_data.get("password"), validate_password),
                two_fa=normalize_totp_secret(
                    valid_or_none(p_data.get("two_fa"), validate_two_fa)
                )
                or None,
            )
            self._save_trash_dict(trash_data)
            self.save_profiles()
        logger.info("Restored profile from trash: %s -> %s", name, target_name)
        return True

    def delete_trashed_profile(self, name: str) -> bool:
        with self._lock:
            trash_data = self._load_trash_dict()
            key = next(
                (k for k in trash_data if k == name or k.casefold() == name.casefold()), None
            )
            if key is None:
                return False
            del trash_data[key]
            self._save_trash_dict(trash_data)
            shutil.rmtree(self._trash_data_dir() / key, ignore_errors=True)
        logger.info("Permanently deleted trashed profile: %s", name)
        return True

    def empty_trash(self) -> int:
        with self._lock:
            trash_data = self._load_trash_dict()
            count = len(trash_data)
            self._save_trash_dict({})
            shutil.rmtree(self._trash_data_dir(), ignore_errors=True)
        logger.info("Emptied trash (%d profiles)", count)
        return count

    def list_trashed_profiles(self) -> list[dict]:
        with self._lock:
            trash_data = self._load_trash_dict()
            items = list(trash_data.values())
            items.sort(key=lambda d: int(d.get("deleted_at") or 0), reverse=True)
            return items

    def bulk_update_group(self, names: list[str], group: str | None) -> int:
        clean_group = (group or "").strip() or None
        if clean_group is not None and not validate_group(clean_group)[0]:
            return 0
        count = 0
        with self._lock:
            for name in names:
                p = self.profiles.get(name)
                if p is not None:
                    p.group = clean_group
                    count += 1
            if count:
                self.save_profiles()
        return count

    def bulk_update_color(self, names: list[str], color: str | None) -> int:
        clean_color = valid_or_none(color, validate_color)
        count = 0
        with self._lock:
            for name in names:
                p = self.profiles.get(name)
                if p is not None:
                    p.color = clean_color
                    count += 1
            if count:
                self.save_profiles()
        return count

    def bulk_update_status(self, names: list[str], status: str | None) -> int:
        clean_status = (status or "").strip() or None
        if clean_status is not None and not validate_status(clean_status)[0]:
            return 0
        count = 0
        with self._lock:
            for name in names:
                p = self.profiles.get(name)
                if p is not None:
                    was_banned = self._is_profile_banned(p)
                    p.status = clean_status
                    self._sync_ban_state(p, was_banned=was_banned)
                    count += 1
            if count:
                self.save_profiles()
        return count

    def update_profile_dates(
        self,
        name: str,
        created_at: str | None,
        banned_at: str | None,
    ) -> bool:
        raw_created = (created_at or "").strip()
        raw_banned = (banned_at or "").strip()
        parsed_created = parse_flexible_date(raw_created) if raw_created else None
        if raw_created and parsed_created is None:
            return False
        parsed_banned = parse_flexible_date(raw_banned) if raw_banned else None
        if raw_banned and parsed_banned is None:
            return False
        with self._lock:
            p = self.profiles.get(name)
            if p is None:
                return False
            p.created_at = parsed_created or p.created_at or self._infer_created_at(name)
            p.banned_at = parsed_banned
            self.save_profiles()
            return True

    def update_profile_email(self, name: str, email: str | None) -> bool:
        clean_email = (email or "").strip() or None
        if clean_email is not None and not validate_email(clean_email)[0]:
            return False
        with self._lock:
            p = self.profiles.get(name)
            if p is None:
                return False
            p.email = clean_email
            self.save_profiles()
            return True

    def toggle_pin_profile(self, name: str) -> bool | None:
        with self._lock:
            p = self.profiles.get(name)
            if p is None:
                return None
            p.pinned = not bool(p.pinned)
            self.save_profiles()
            return p.pinned

    def bulk_update_pinned(self, names: list[str], pinned: bool) -> int:
        target_val = bool(pinned)
        count = 0
        with self._lock:
            for name in names:
                p = self.profiles.get(name)
                if p is not None and bool(p.pinned) != target_val:
                    p.pinned = target_val
                    count += 1
            if count:
                self.save_profiles()
        return count

    def list_status_tags(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(t) for t in self.status_tags]

    def save_status_tag(self, code_or_new: str, label: str, color: str) -> bool:
        clean_label = (label or "").strip()
        if not clean_label or not validate_status(clean_label)[0]:
            return False
        clean_color = (color or "").strip()
        if not clean_color.startswith("#"):
            clean_color = f"#{clean_color}"
        if not validate_color(clean_color)[0]:
            clean_color = "#0284C7"
        target_code = (code_or_new or "").strip()
        with self._lock:
            for tag in self.status_tags:
                if target_code and tag["code"].casefold() == target_code.casefold():
                    old_code = tag["code"]
                    old_label = tag.get("label") or ""
                    tag["label"] = clean_label
                    tag["color"] = clean_color.upper()
                    # Also update profiles that stored old_label as custom string
                    changed_prof = False
                    for p in self.profiles.values():
                        if p.status and p.status.casefold() in {
                            old_code.casefold(),
                            old_label.casefold(),
                        } - {""}:
                            p.status = old_code
                            changed_prof = True
                    self._save_status_tags()
                    if changed_prof:
                        self.save_profiles()
                    return True
            # Check duplicate by label/code
            if any(
                t["code"].casefold() == clean_label.casefold()
                or (t.get("label") or "").casefold() == clean_label.casefold()
                for t in self.status_tags
            ):
                return False
            self.status_tags.append(
                {
                    "code": clean_label,
                    "label": clean_label,
                    "color": clean_color.upper(),
                }
            )
            self._save_status_tags()
            return True

    def delete_status_tag(self, code: str) -> bool:
        target = (code or "").strip().casefold()
        if not target:
            return False
        with self._lock:
            removed_tags = [
                t
                for t in self.status_tags
                if t["code"].casefold() == target or (t.get("label") or "").casefold() == target
            ]
            if not removed_tags:
                return False
            removed_keys = {
                s.casefold() for t in removed_tags for s in (t["code"], t.get("label") or "") if s
            }
            self.status_tags = [
                t
                for t in self.status_tags
                if t["code"].casefold() != target and (t.get("label") or "").casefold() != target
            ]
            self._save_status_tags()
            cleared = False
            for p in self.profiles.values():
                if p.status and p.status.strip().casefold() in removed_keys:
                    p.status = None
                    cleared = True
            if cleared:
                self.save_profiles()
            return True

    def reset_status_tags(self) -> list[dict[str, str]]:
        with self._lock:
            self.status_tags = default_status_tags()
            self._save_status_tags()
            return [dict(t) for t in self.status_tags]

    def replace_proxy_across_profiles(
        self,
        old_proxy_or_port: str,
        new_proxy: str | None,
        *,
        skip_names: set[str] | None = None,
    ) -> list[str]:
        """Replace a proxy (matched by exact string or by port) across all attached profiles."""
        target = (old_proxy_or_port or "").strip()
        if not target:
            return []
        clean_new = (new_proxy or "").strip()
        if clean_new:
            ok, _ = validate_proxy_format(clean_new)
            if not ok:
                return []
            clean_new = normalize_proxy(clean_new)
        target_port = target if target.startswith(":") else extract_proxy_port(target)
        skipped = set(skip_names or ())
        updated: list[str] = []
        with self._lock:
            for name, p in self.profiles.items():
                if name in skipped:
                    continue
                cur = (p.proxy or "").strip()
                if not cur:
                    continue
                cur_port = extract_proxy_port(cur)
                if cur == target or (target_port and cur_port == target_port):
                    p.proxy = clean_new or None
                    updated.append(name)
            if updated:
                self.proxy_pool = [
                    px
                    for px in self.proxy_pool
                    if px != target and (not target_port or extract_proxy_port(px) != target_port)
                ]
                pool_path = self._proxy_pool_file()
                if self.proxy_pool or pool_path.exists():
                    self._save_proxy_pool()
                self.save_profiles()
        return updated

    def list_proxy_pool(self) -> list[str]:
        with self._lock:
            return list(self.proxy_pool)

    def list_unassigned_proxies(self) -> list[str]:
        """Return proxies in the saved pool that are not currently assigned to any profile."""
        with self._lock:
            used_exact: set[str] = set()
            used_ports: set[str] = set()
            for p in self.profiles.values():
                cur = (p.proxy or "").strip()
                if cur:
                    used_exact.add(normalize_proxy(cur))
                    port = extract_proxy_port(cur)
                    if port:
                        used_ports.add(port)
            return [
                px
                for px in self.proxy_pool
                if px not in used_exact
                and (not extract_proxy_port(px) or extract_proxy_port(px) not in used_ports)
            ]

    def add_proxies_to_pool(self, raw_lines: list[str]) -> tuple[int, int]:
        """Validate, normalize, and add new proxies to the central proxy pool."""
        added = 0
        skipped = 0
        with self._lock:
            existing: set[str] = set(self.proxy_pool)
            existing_ports: set[str] = {
                extract_proxy_port(px) for px in self.proxy_pool if extract_proxy_port(px)
            }
            for p in self.profiles.values():
                cur = (p.proxy or "").strip()
                if cur:
                    existing.add(normalize_proxy(cur))
                    port = extract_proxy_port(cur)
                    if port:
                        existing_ports.add(port)
            for line in raw_lines:
                s = (line or "").strip()
                if not s:
                    continue
                if not validate_proxy_format(s)[0]:
                    skipped += 1
                    continue
                norm = normalize_proxy(s)
                port = extract_proxy_port(norm)
                if norm in existing or (port and port in existing_ports):
                    skipped += 1
                    continue
                existing.add(norm)
                if port:
                    existing_ports.add(port)
                self.proxy_pool.append(norm)
                added += 1
            if added:
                self._save_proxy_pool()
        return added, skipped

    def remove_proxy_from_pool(self, proxy_or_port: str) -> bool:
        target = (proxy_or_port or "").strip()
        if not target:
            return False
        target_port = target if target.startswith(":") else extract_proxy_port(target)
        with self._lock:
            before = len(self.proxy_pool)
            self.proxy_pool = [
                px
                for px in self.proxy_pool
                if px != target and (not target_port or extract_proxy_port(px) != target_port)
            ]
            if len(self.proxy_pool) < before:
                self._save_proxy_pool()
                return True
        return False

    def export_profiles_csv(self, csv_path: str, names: list[str] | None = None) -> int:
        """Export profiles metadata to a UTF-8-SIG CSV file (Excel-compatible)."""
        fieldnames = [
            "name",
            "status",
            "group",
            "pages",
            "proxy",
            "os_type",
            "timezone",
            "locale",
            "color",
            "notes",
            "pinned",
            "created_at",
            "banned_at",
            "email",
            "password",
            "two_fa",
        ]
        with self._lock:
            selected_set = set(names) if names is not None else None
            rows = [
                p for p in self.profiles.values() if selected_set is None or p.name in selected_set
            ]
        path = pathlib.Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for p in rows:
                writer.writerow(
                    {
                        "name": p.name,
                        "email": p.email or "",
                        "password": p.password or "",
                        "two_fa": p.two_fa or "",
                        "pinned": "1" if p.pinned else "",
                        "status": p.status or "",
                        "group": p.group or "",
                        "pages": ";".join(p.pages),
                        "proxy": p.proxy or "",
                        "os_type": p.os_type,
                        "timezone": p.timezone or "",
                        "locale": p.locale or "",
                        "color": p.color or "",
                        "notes": p.notes or "",
                        "created_at": p.created_at or "",
                        "banned_at": p.banned_at or "",
                    }
                )
        return len(rows)

    def import_profiles_csv(
        self,
        csv_path: str,
        running_names: set[str] | None = None,
    ) -> tuple[int, int, int]:
        """Import or sync profile configurations from a CSV file without touching camoufox_data/."""
        path = pathlib.Path(csv_path)
        if not path.exists():
            return 0, 0, 0
        active = set(running_names or ())
        created = 0
        updated = 0
        skipped = 0
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
            reader = csv.DictReader(content.splitlines())
        except Exception:
            return 0, 0, 0

        with self._lock:
            for row in reader:
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                raw_name = str(row.get("name") or "").strip()
                if not raw_name or not validate_profile_name(raw_name)[0]:
                    skipped += 1
                    continue
                raw_proxy = str(row.get("proxy") or "").strip()
                if raw_proxy:
                    if not validate_proxy_format(raw_proxy)[0]:
                        skipped += 1
                        continue
                    raw_proxy = normalize_proxy(raw_proxy)
                os_val = str(row.get("os_type") or "windows").strip().lower()
                if os_val not in ("windows", "macos", "linux"):
                    os_val = "windows"
                tz_val = valid_or_none(str(row.get("timezone") or "").strip(), validate_timezone)
                loc_val = valid_or_none(str(row.get("locale") or "").strip(), validate_locale)
                col_val = valid_or_none(str(row.get("color") or "").strip(), validate_color)
                grp_val = valid_or_none(str(row.get("group") or "").strip(), validate_group)
                not_val = valid_or_none(str(row.get("notes") or "").strip(), validate_notes)
                st_val = valid_or_none(str(row.get("status") or "").strip(), validate_status)
                has_pinned_col = "pinned" in row and row.get("pinned") is not None
                raw_pinned = str(row.get("pinned") or "").strip().lower()
                pinned_val = raw_pinned in ("1", "true", "yes", "x", "ghim", "pinned")
                raw_pages_str = str(row.get("pages") or "").strip()
                page_list = (
                    [s.strip() for s in raw_pages_str.split(";") if s.strip()]
                    if raw_pages_str
                    else []
                )
                canonical_pages = self._register_pages(page_list)

                has_email_col = "email" in row and row.get("email") is not None
                email_val = valid_or_none(str(row.get("email") or "").strip(), validate_email)
                has_pass_col = "password" in row and row.get("password") is not None
                pass_val = valid_or_none(str(row.get("password") or "").strip(), validate_password)
                has_2fa_col = ("two_fa" in row and row.get("two_fa") is not None) or (
                    "2fa" in row and row.get("2fa") is not None
                )
                raw_2fa_col = str(row.get("two_fa") or row.get("2fa") or "").strip()
                two_fa_val = (
                    normalize_totp_secret(valid_or_none(raw_2fa_col, validate_two_fa)) or None
                )
                has_created_col = "created_at" in row and row.get("created_at") is not None
                created_val = parse_flexible_date(row.get("created_at"))
                has_banned_col = "banned_at" in row and row.get("banned_at") is not None
                banned_val = parse_flexible_date(row.get("banned_at"))

                existing_key = self._find_by_casefold(raw_name)
                if existing_key is not None:
                    if existing_key in active:
                        skipped += 1
                        continue
                    cur = self.profiles[existing_key]
                    was_banned = self._is_profile_banned(cur)
                    cur.proxy = raw_proxy or None
                    cur.os_type = os_val
                    cur.timezone = tz_val
                    cur.locale = loc_val
                    cur.color = col_val
                    cur.group = grp_val
                    cur.notes = not_val
                    cur.status = st_val
                    if has_email_col:
                        cur.email = email_val
                    if has_pass_col:
                        cur.password = pass_val
                    if has_2fa_col:
                        cur.two_fa = two_fa_val
                    if has_pinned_col:
                        cur.pinned = pinned_val
                    if canonical_pages:
                        cur.pages = canonical_pages
                    if has_created_col and created_val:
                        cur.created_at = created_val
                    if has_banned_col:
                        cur.banned_at = banned_val
                    else:
                        self._sync_ban_state(cur, was_banned=was_banned)
                    updated += 1
                else:
                    try:
                        pathlib.Path(self._data_path(raw_name)).mkdir(exist_ok=True, parents=True)
                    except OSError:
                        skipped += 1
                        continue
                    now_ymd = today_ymd()
                    self.profiles[raw_name] = Profile(
                        name=raw_name,
                        proxy=raw_proxy or None,
                        os_type=os_val,
                        timezone=tz_val,
                        locale=loc_val,
                        engine="camoufox",
                        color=col_val,
                        group=grp_val,
                        notes=not_val,
                        pages=canonical_pages,
                        status=st_val,
                        pinned=pinned_val,
                        created_at=created_val or now_ymd,
                        banned_at=banned_val
                        or (
                            now_ymd
                            if (is_ban_status(st_val) or has_ban_page(canonical_pages))
                            else None
                        ),
                        email=email_val,
                        password=pass_val,
                        two_fa=two_fa_val,
                    )
                    created += 1
            if created or updated:
                self.save_profiles()
        return created, updated, skipped

    def bulk_update_proxies(self, assignments: dict[str, str | None]) -> int:
        count = 0
        with self._lock:
            for name, raw_proxy in assignments.items():
                p = self.profiles.get(name)
                if p is None:
                    continue
                clean = (raw_proxy or "").strip()
                if clean and not validate_proxy_format(clean)[0]:
                    continue
                p.proxy = clean or None
                count += 1
            if count:
                self.save_profiles()
        return count

    def pin_missing_geo(
        self,
        name: str,
        timezone: str | None,
        locale: str | None,
    ) -> bool:
        """Fill missing timezone/locale on a profile without ever overwriting existing pinned values."""
        clean_tz = valid_or_none(timezone, validate_timezone)
        clean_loc = valid_or_none(locale, validate_locale)
        if not clean_tz and not clean_loc:
            return False
        with self._lock:
            p = self.profiles.get(name)
            if p is None:
                return False
            changed = False
            if not p.timezone and clean_tz:
                p.timezone = clean_tz
                changed = True
            if not p.locale and clean_loc:
                p.locale = clean_loc
                changed = True
            if changed:
                self.save_profiles()
            return changed

    def batch_create_profiles(
        self,
        entries: list[tuple[str, str | None]],
        *,
        os_type: str = "windows",
        timezone: str | None = None,
        locale: str | None = None,
        group: str | None = None,
        color: str | None = None,
        pages: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Batch-create new profiles safely; never touches or overwrites existing profiles."""
        clean_tz = valid_or_none(timezone, validate_timezone)
        clean_loc = valid_or_none(locale, validate_locale)
        clean_grp = valid_or_none(group, validate_group)
        clean_col = valid_or_none(color, validate_color)
        clean_os = os_type if os_type in ("windows", "macos", "linux") else "windows"
        created: list[str] = []
        skipped: list[str] = []

        with self._lock:
            canonical_pages = self._register_pages(pages)
            for raw_name, raw_proxy in entries:
                name = (raw_name or "").strip()
                if not name or not validate_profile_name(name)[0]:
                    skipped.append(name or raw_name)
                    continue
                if self._find_by_casefold(name) is not None:
                    skipped.append(name)
                    continue
                proxy_clean = (raw_proxy or "").strip()
                if proxy_clean and not validate_proxy_format(proxy_clean)[0]:
                    skipped.append(name)
                    continue
                try:
                    pathlib.Path(self._data_path(name)).mkdir(exist_ok=True, parents=True)
                except OSError:
                    skipped.append(name)
                    continue
                now_ymd = today_ymd()
                self.profiles[name] = Profile(
                    name=name,
                    proxy=proxy_clean or None,
                    os_type=clean_os,
                    timezone=clean_tz,
                    locale=clean_loc,
                    engine="camoufox",
                    color=clean_col,
                    group=clean_grp,
                    notes=None,
                    pages=list(canonical_pages),
                    created_at=now_ymd,
                    banned_at=now_ymd if has_ban_page(canonical_pages) else None,
                )
                created.append(name)
            if created:
                self.save_profiles()
        return created, skipped

    def inspect_config_backup(self, backup_path: pathlib.Path) -> dict[str, object] | None:
        """Read a backup file and return metadata without modifying any state."""
        if not backup_path.exists():
            return None
        try:
            raw = json.loads(backup_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            st = backup_path.stat()
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        except Exception:
            return None

        with self._lock:
            missing = 0
            changed = 0
            for b_name, b_data in raw.items():
                if not isinstance(b_data, dict) or not validate_profile_name(b_name)[0]:
                    continue
                existing_key = self._find_by_casefold(b_name)
                if existing_key is None:
                    missing += 1
                else:
                    cur = self.profiles[existing_key]
                    if (
                        (b_data.get("proxy") or None) != cur.proxy
                        or (b_data.get("group") or None) != cur.group
                        or (b_data.get("notes") or None) != cur.notes
                        or (b_data.get("timezone") or None) != cur.timezone
                        or (b_data.get("locale") or None) != cur.locale
                        or (b_data.get("color") or None) != cur.color
                    ):
                        changed += 1
            return {
                "path": backup_path,
                "name": backup_path.name,
                "mtime_str": mtime_str,
                "total": len(raw),
                "missing": missing,
                "changed": changed,
            }

    def restore_config_backup(
        self,
        backup_path: pathlib.Path,
        *,
        keep_newer_profiles: bool = True,
    ) -> tuple[bool, int]:
        """Safely restore profile settings from a backup without touching camoufox_data/ files."""
        if not backup_path.exists():
            return False, 0
        try:
            raw = json.loads(backup_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return False, 0
        except Exception:
            return False, 0

        with self._lock:
            # Save current snapshot before restoring
            cur_file = pathlib.Path(PROFILES_FILE)
            if cur_file.exists():
                self._write_config_backup(cur_file.read_text(encoding="utf-8"))

            restored_map: dict[str, Profile] = {}
            updated_count = 0
            for b_name, p_data in raw.items():
                if (
                    not isinstance(p_data, dict)
                    or not validate_profile_name(b_name)[0]
                    or p_data.get("name") != b_name
                ):
                    continue
                existing_key = self._find_by_casefold(b_name)
                canonical_name = existing_key or b_name
                with contextlib.suppress(OSError):
                    pathlib.Path(self._data_path(canonical_name)).mkdir(exist_ok=True, parents=True)
                pages = self._register_pages(p_data.get("pages"))
                status_val = valid_or_none(p_data.get("status"), validate_status)
                created_val = parse_flexible_date(p_data.get("created_at"))
                if not created_val:
                    created_val = (
                        self.profiles[existing_key].created_at
                        if existing_key is not None and self.profiles[existing_key].created_at
                        else self._infer_created_at(canonical_name)
                    )
                banned_val = parse_flexible_date(p_data.get("banned_at"))
                if not banned_val and (is_ban_status(status_val) or has_ban_page(pages)):
                    banned_val = self._infer_banned_at(canonical_name)
                prof = Profile(
                    name=canonical_name,
                    proxy=p_data.get("proxy") or None,
                    os_type=p_data.get(
                        "os_type",
                        p_data.get("config", {}).get("os", "windows"),
                    ),
                    timezone=valid_or_none(p_data.get("timezone"), validate_timezone),
                    locale=valid_or_none(p_data.get("locale"), validate_locale),
                    engine=_engine_or_default(p_data.get("engine")),
                    color=valid_or_none(p_data.get("color"), validate_color),
                    group=valid_or_none(p_data.get("group"), validate_group),
                    notes=valid_or_none(p_data.get("notes"), validate_notes),
                    pages=pages,
                    status=status_val,
                    pinned=bool(p_data.get("pinned", False)),
                    created_at=created_val,
                    banned_at=banned_val,
                    email=valid_or_none(p_data.get("email"), validate_email),
                    password=valid_or_none(p_data.get("password"), validate_password),
                    two_fa=normalize_totp_secret(
                        valid_or_none(p_data.get("two_fa"), validate_two_fa)
                    )
                    or None,
                )
                if existing_key is None or self.profiles[existing_key].to_dict() != prof.to_dict():
                    updated_count += 1
                restored_map[canonical_name] = prof

            if keep_newer_profiles:
                seen_lower = {k.casefold() for k in restored_map}
                for cur_name, cur_prof in self.profiles.items():
                    if cur_name.casefold() not in seen_lower:
                        restored_map[cur_name] = cur_prof

            self.profiles = restored_map
            self.save_profiles()
            return True, updated_count

    def list_profiles(self) -> list[Profile]:
        with self._lock:
            return list(self.profiles.values())

    def export_profile(
        self,
        name: str,
        export_path: str,
        include_data: bool = True,
    ) -> tuple[bool, str]:
        with self._lock:
            if name not in self.profiles:
                return False, "Profile not found"
            profile = self.profiles[name]
            data_path = self._data_path(name)
        return export_to_zip(profile, data_path, export_path, include_data)

    def import_profile(
        self,
        zip_path: str,
        overwrite: bool = False,
    ) -> tuple[bool, str]:
        # Decide before extracting: an archive must not write into an existing
        # profile's data unless overwriting, and an overwrite that changes the
        # engine starts from an empty directory (the engines' data do not mix).
        meta = read_profile_meta(zip_path) or {}
        name = meta.get("name")
        with self._lock:
            # Case-insensitive collision check: find any existing profile whose
            # name casefolds to the imported name so the check is consistent on
            # all platforms (Windows/macOS case-insensitive FS, Linux sensitive).
            existing_key = self._find_by_casefold(name) if name else None
            if existing_key is not None:
                if not overwrite:
                    return False, f"Profile '{name}' already exists"
                if (meta.get("engine") or "camoufox") != self.profiles[existing_key].engine:
                    shutil.rmtree(self._data_path(existing_key), ignore_errors=True)

            success, result = import_from_zip(zip_path, DATA_DIR)
            if not success:
                return False, result

            profile = result
            profile.pages = self._register_pages(profile.pages)
            if not profile.created_at:
                profile.created_at = self._infer_created_at(profile.name)
            self._sync_ban_state(profile, infer_from_disk=True)

            # An overwrite under a different case replaces the old entry rather
            # than leaving two names for one data directory.
            if existing_key is not None and existing_key != profile.name:
                old_dir = pathlib.Path(self._data_path(existing_key))
                new_dir = pathlib.Path(self._data_path(profile.name))
                if old_dir.exists() and not (new_dir.exists() and old_dir.samefile(new_dir)):
                    shutil.rmtree(old_dir, ignore_errors=True)
                del self.profiles[existing_key]
            self.profiles[profile.name] = profile
            self.save_profiles()
            logger.info("Registered imported profile: %s", profile.name)
            return True, profile.name
