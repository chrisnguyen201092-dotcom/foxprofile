from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from ..models.profile import Profile


class IProfileManager(Protocol):
    profiles: dict[str, Profile]
    custom_pages: list[str]

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
        recovery_email: str | None = None,
    ) -> bool: ...

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
        new_status: Any = ...,
        new_pinned: bool | None = None,
        new_email: Any = ...,
        new_password: Any = ...,
        new_two_fa: Any = ...,
        new_recovery_email: Any = ...,
    ) -> bool: ...

    def move_profile(self, name: str, direction: int) -> bool: ...

    def reorder_profiles(self, names: list[str]) -> bool: ...

    def list_custom_pages(self) -> list[str]: ...

    def save_custom_page(
        self,
        page_name: str,
        profile_names: list[str] | None = None,
        original_name: str | None = None,
    ) -> bool: ...

    def delete_custom_page(self, page_name: str) -> bool: ...

    def add_profiles_to_page(self, page_name: str, profile_names: list[str]) -> bool: ...

    def remove_profiles_from_page(self, page_name: str, profile_names: list[str]) -> bool: ...

    def reorder_custom_pages(self, ordered_pages: list[str]) -> bool: ...

    def delete_profile(self, name: str) -> bool: ...

    def clone_profile(
        self,
        source_name: str,
        new_name: str,
        copy_proxy: bool = True,
    ) -> bool: ...

    def trash_profile(self, name: str) -> bool: ...

    def restore_profile(self, name: str) -> bool: ...

    def delete_trashed_profile(self, name: str) -> bool: ...

    def empty_trash(self) -> int: ...

    def list_trashed_profiles(self) -> list[dict[str, Any]]: ...

    def bulk_update_group(self, names: list[str], group: str | None) -> int: ...

    def bulk_update_color(self, names: list[str], color: str | None) -> int: ...

    def bulk_update_status(self, names: list[str], status: str | None) -> int: ...

    def apply_schedule_tags(self, tag_map: dict[str, str]) -> int: ...

    def clear_schedule_tags(self) -> int: ...

    def update_profile_dates(
        self,
        name: str,
        created_at: str | None,
        banned_at: str | None,
    ) -> bool: ...

    def update_profile_email(self, name: str, email: str | None) -> bool: ...

    def update_profile_credentials(
        self,
        name: str,
        email: Any = ...,
        password: Any = ...,
        two_fa: Any = ...,
        recovery_email: Any = ...,
        notes: Any = ...,
        mail_token: Any = ...,
    ) -> bool: ...

    def bulk_import_credentials(self, raw_lines: list[str] | str) -> tuple[int, int]: ...

    def toggle_pin_profile(self, name: str) -> bool | None: ...

    def bulk_update_pinned(self, names: list[str], pinned: bool) -> int: ...

    def list_status_tags(self) -> list[dict[str, str]]: ...

    def save_status_tag(self, code_or_new: str, label: str, color: str) -> bool: ...

    def delete_status_tag(self, code: str) -> bool: ...

    def reset_status_tags(self) -> list[dict[str, str]]: ...

    def replace_proxy_across_profiles(
        self,
        old_proxy_or_port: str,
        new_proxy: str | None,
        *,
        skip_names: set[str] | None = None,
    ) -> list[str]: ...

    def list_proxy_pool(self) -> list[str]: ...

    def list_unassigned_proxies(self) -> list[str]: ...

    def add_proxies_to_pool(self, raw_lines: list[str]) -> tuple[int, int]: ...

    def remove_proxy_from_pool(self, proxy_or_port: str) -> bool: ...

    def list_mail_pool(self) -> list[str]: ...

    def list_unassigned_mails(self) -> list[str]: ...

    def add_mails_to_pool(self, raw_lines: list[str]) -> tuple[int, int]: ...

    def remove_mail_from_pool(self, email_or_line: str) -> bool: ...

    def assign_mail_to_profile(self, name: str, raw_line: str) -> bool: ...

    def export_profiles_csv(self, csv_path: str, names: list[str] | None = None) -> int: ...

    def import_profiles_csv(
        self,
        csv_path: str,
        running_names: set[str] | None = None,
    ) -> tuple[int, int, int]: ...

    def bulk_update_proxies(self, assignments: dict[str, str | None]) -> int: ...

    def pin_missing_geo(
        self,
        name: str,
        timezone: str | None,
        locale: str | None,
    ) -> bool: ...

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
    ) -> tuple[list[str], list[str]]: ...

    def list_config_backups(self) -> list[Any]: ...

    def inspect_config_backup(self, backup_path: Any) -> dict[str, Any] | None: ...

    def restore_config_backup(
        self,
        backup_path: Any,
        *,
        keep_newer_profiles: bool = True,
    ) -> tuple[bool, int]: ...

    def list_profiles(self) -> list[Profile]: ...

    def export_profile(
        self,
        name: str,
        export_path: str,
        include_data: bool = True,
    ) -> tuple[bool, str]: ...

    def import_profile(
        self,
        zip_path: str,
        overwrite: bool = False,
    ) -> tuple[bool, str]: ...


class IBrowserLauncher(Protocol):
    def start_thread(
        self,
        profile: Profile,
        log_callback: Callable[[str], None],
        on_start: Callable[[], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> bool: ...

    def stop_profile(self, profile_name: str, timeout: int = 2) -> bool: ...

    def running_profile_names(self) -> set[str]: ...

    def running_count(self) -> int: ...

    def is_running(self, profile_name: str) -> bool: ...

    def focus_profile(self, profile_name: str) -> bool: ...

    def switch_profile(self, profile_name: str, *, single_stage: bool = True) -> bool: ...

    def arrange_windows(self, profile_names: list[str] | None = None) -> int: ...

    def exclusive(self, profile_name: str) -> AbstractContextManager[None]: ...

    def control(
        self,
        profile_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any: ...

    def wait_for_launch(
        self,
        profile_name: str,
        timeout: float,
    ) -> tuple[bool, str | None]: ...


class IProxyService(Protocol):
    def check_proxy_sync(
        self,
        proxy_str: str,
        timeout: int = 10,
    ) -> tuple[bool, str]: ...
