from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ..dialogs import open_export_dialog


async def import_profile(
    file_picker: ft.FilePicker,
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
    running_names: set[str] | None = None,
) -> None:
    files = await file_picker.pick_files(
        allow_multiple=True,
        file_type=ft.FilePickerFileType.CUSTOM,
        allowed_extensions=["zip", "csv"],
        dialog_title=get_string("import_profile"),
    )
    if not files:
        return
    ok_count = 0
    for f in files:
        if not f.path:
            continue
        if f.path.lower().endswith(".csv") and hasattr(pm, "import_profiles_csv"):
            c, u, s = pm.import_profiles_csv(f.path, running_names=running_names)
            log(get_string("csv_imported_log", created=c, updated=u, skipped=s))
            if c or u:
                ok_count += 1
        else:
            success, result = pm.import_profile(f.path)
            if success:
                ok_count += 1
                log(get_string("import_success") + f": {result}")
            else:
                log(get_string("import_error", error=result))
    if ok_count:
        refresh()


def export_profile(
    page: ft.Page,
    file_picker: ft.FilePicker,
    pm: IProfileManager,
    log: Callable[[str], None],
    *,
    preselected_names: set[str] | None = None,
) -> None:
    profiles = pm.list_profiles()
    if not profiles:
        return

    def on_complete(names: list[str], dir_path: str, include_data: bool) -> None:
        for name in names:
            success, result = pm.export_profile(name, dir_path, include_data)
            if success:
                log(get_string("export_success") + f": {result}")
            else:
                log(get_string("export_error", error=result))

    def on_csv_complete(names: list[str], csv_path: str) -> None:
        if hasattr(pm, "export_profiles_csv"):
            cnt = pm.export_profiles_csv(csv_path, names=names)
            log(get_string("csv_exported_log", count=cnt, file=csv_path))

    open_export_dialog(
        page,
        file_picker,
        profiles,
        on_complete,
        preselected_names=preselected_names,
        on_export_csv=on_csv_complete,
    )
