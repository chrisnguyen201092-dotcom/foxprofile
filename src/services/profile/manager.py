import contextlib
import json
import os
import pathlib
import shutil
import threading
import time

from ...core.config import DATA_DIR, PROFILES_FILE
from ...core.logging import get_logger
from ...models.profile import ENGINES, Profile
from ...utils.validation import (
    valid_or_none,
    validate_color,
    validate_group,
    validate_locale,
    validate_notes,
    validate_profile_name,
    validate_timezone,
)
from .transfer import export_to_zip, import_from_zip, read_profile_meta

logger = get_logger("profile.manager")


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
        self._load_profiles()
        pathlib.Path(DATA_DIR).mkdir(exist_ok=True, parents=True)

    @staticmethod
    def _pages_file() -> pathlib.Path:
        return pathlib.Path(PROFILES_FILE).with_name("custom_pages.json")

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

    def _load_profiles(self) -> None:
        pages_path = self._pages_file()
        if pages_path.exists():
            with contextlib.suppress(Exception):
                raw_pages = json.loads(pages_path.read_text(encoding="utf-8"))
                self.custom_pages = _clean_pages(raw_pages)

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
                            "pages": [self._find_page_by_casefold(pg) or pg for pg in pages],
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
            if self.custom_pages or pages_path.exists():
                pages_tmp = pages_path.with_suffix(".json.tmp")
                pages_tmp.write_text(
                    json.dumps(self.custom_pages, indent=2),
                    encoding="utf-8",
                )
                _replace(pages_tmp, pages_path)
            logger.debug("Profiles saved")
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
                pages=self._register_pages(pages),
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
                    has_page = any(pg.casefold() == target_page.casefold() for pg in p.pages)
                    if p.name in chosen and not has_page:
                        p.pages.append(target_page)
                    elif p.name not in chosen and has_page:
                        p.pages = [pg for pg in p.pages if pg.casefold() != target_page.casefold()]

            self.save_profiles()
            return True

    def delete_custom_page(self, page_name: str) -> bool:
        with self._lock:
            match = self._find_page_by_casefold(page_name.strip())
            if match is None:
                return False
            self.custom_pages = [pg for pg in self.custom_pages if pg != match]
            for p in self.profiles.values():
                p.pages = [pg for pg in p.pages if pg.casefold() != match.casefold()]
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
                    p.pages.append(target)
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
                    p.pages = [pg for pg in p.pages if pg.casefold() != target.casefold()]
            self.save_profiles()
            return True

    def reorder_custom_pages(self, ordered_pages: list[str]) -> bool:
        """Reorder custom pages according to *ordered_pages*; unlisted pages stay at the end."""
        with self._lock:
            seen: set[str] = set()
            new_order: list[str] = []
            for raw in ordered_pages:
                match = self._find_page_by_casefold(raw.strip())
                if match is not None and match.casefold() not in seen:
                    seen.add(match.casefold())
                    new_order.append(match)
            for pg in self.custom_pages:
                if pg.casefold() not in seen:
                    new_order.append(pg)
            self.custom_pages = new_order
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
