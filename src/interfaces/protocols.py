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

    def delete_profile(self, name: str) -> bool: ...

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
