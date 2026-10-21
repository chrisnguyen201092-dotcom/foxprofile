from src.services.profile.manager import ProfileManager
from src.ui.state import AppState


def test_select_all_accumulates_across_pages():
    """select_all should add to existing selection, not replace it."""
    state = AppState()
    state.toggle_selection("profile-a")
    state.toggle_selection("profile-b")

    # Simulate selecting a second page
    state.select_all(["profile-c", "profile-d"])

    selected = state.selected_names()
    assert "profile-a" in selected
    assert "profile-b" in selected
    assert "profile-c" in selected
    assert "profile-d" in selected


def test_select_all_deduplicates():
    """select_all with already-selected names should not duplicate."""
    state = AppState()
    state.toggle_selection("profile-a")

    state.select_all(["profile-a", "profile-b"])

    selected = state.selected_names()
    assert selected == {"profile-a", "profile-b"}


def test_profile_color_and_ordering(tmp_path, monkeypatch):
    """ProfileManager persists color, move_profile, reorder_profiles, and preserves position on rename."""
    import src.services.profile.manager as pm_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = ProfileManager()

    assert pm.add_profile("p1", None, "windows", color="#ef4444")
    assert pm.add_profile("p2", None, "windows")
    assert pm.add_profile("p3", None, "windows", color="#3b82f6")

    assert [p.name for p in pm.list_profiles()] == ["p1", "p2", "p3"]
    assert pm.profiles["p1"].color == "#ef4444"
    assert pm.profiles["p2"].color is None

    # Move p2 up -> p2, p1, p3
    assert pm.move_profile("p2", -1)
    assert [p.name for p in pm.list_profiles()] == ["p2", "p1", "p3"]

    # Rename p1 (middle item) to p1-renamed and update color -> stays in middle!
    assert pm.update_profile("p1", "p1-renamed", None, "windows", new_color="#22c55e")
    assert [p.name for p in pm.list_profiles()] == ["p2", "p1-renamed", "p3"]
    assert pm.profiles["p1-renamed"].color == "#22c55e"

    # Reorder explicitly -> p3, p2, p1-renamed
    assert pm.reorder_profiles(["p3", "p2", "p1-renamed"])
    assert [p.name for p in pm.list_profiles()] == ["p3", "p2", "p1-renamed"]

    # Reload from disk and verify order + colors persisted
    pm2 = ProfileManager()
    assert [p.name for p in pm2.list_profiles()] == ["p3", "p2", "p1-renamed"]
    assert pm2.profiles["p3"].color == "#3b82f6"
    assert pm2.profiles["p2"].color is None
    assert pm2.profiles["p1-renamed"].color == "#22c55e"


def test_profile_group_and_notes(tmp_path, monkeypatch):
    """ProfileManager persists group and notes and allows clearing them."""
    import src.services.profile.manager as pm_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = ProfileManager()

    assert pm.add_profile(
        "shop-1",
        None,
        "windows",
        group="TikTok US",
        notes="acc1@mail.com | 2FA: 123456",
    )
    assert pm.profiles["shop-1"].group == "TikTok US"
    assert pm.profiles["shop-1"].notes == "acc1@mail.com | 2FA: 123456"

    # Update group and notes
    assert pm.update_profile(
        "shop-1",
        "shop-1",
        None,
        "windows",
        new_group="FB Ads",
        new_notes="Updated note",
    )
    assert pm.profiles["shop-1"].group == "FB Ads"
    assert pm.profiles["shop-1"].notes == "Updated note"

    # Reload from disk and verify
    pm2 = ProfileManager()
    assert pm2.profiles["shop-1"].group == "FB Ads"
    assert pm2.profiles["shop-1"].notes == "Updated note"

    # Clear group and notes
    assert pm2.update_profile(
        "shop-1",
        "shop-1",
        None,
        "windows",
        new_group=None,
        new_notes=None,
    )
    assert pm2.profiles["shop-1"].group is None
    assert pm2.profiles["shop-1"].notes is None


def test_custom_pages_crud(tmp_path, monkeypatch):
    """ProfileManager supports creating, editing, overlapping, and deleting custom pages."""
    import src.services.profile.manager as pm_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = ProfileManager()

    for name in ("p1", "p2", "p3", "p4"):
        assert pm.add_profile(name, None, "windows")

    # Create "Disconnect" page with p1, p2
    assert pm.save_custom_page("Disconnect", ["p1", "p2"])
    # Create "Chạy Ads" page with overlapping p2, p3
    assert pm.save_custom_page("Chạy Ads", ["p2", "p3"])

    assert pm.list_custom_pages() == ["Disconnect", "Chạy Ads"]
    assert pm.profiles["p1"].pages == ["Disconnect"]
    assert pm.profiles["p2"].pages == ["Disconnect", "Chạy Ads"]
    assert pm.profiles["p3"].pages == ["Chạy Ads"]
    assert pm.profiles["p4"].pages == []

    # Add p4 to "Disconnect" via bulk helper
    assert pm.add_profiles_to_page("Disconnect", ["p4"])
    assert "Disconnect" in pm.profiles["p4"].pages

    # Remove p1 from "Disconnect"
    assert pm.remove_profiles_from_page("Disconnect", ["p1"])
    assert "Disconnect" not in pm.profiles["p1"].pages

    # Rename page "Disconnect" -> "Lỗi Mạng" and update membership to [p1, p4]
    assert pm.save_custom_page("Lỗi Mạng", ["p1", "p4"], original_name="Disconnect")
    assert pm.list_custom_pages() == ["Lỗi Mạng", "Chạy Ads"]
    assert pm.profiles["p2"].pages == ["Chạy Ads"]
    assert pm.profiles["p1"].pages == ["Lỗi Mạng"]

    # Reload from disk and verify custom pages + profile memberships persist
    pm2 = ProfileManager()
    assert pm2.list_custom_pages() == ["Lỗi Mạng", "Chạy Ads"]
    assert pm2.reorder_custom_pages(["Chạy Ads", "Lỗi Mạng"])
    assert pm2.list_custom_pages() == ["Chạy Ads", "Lỗi Mạng"]
    assert pm2.profiles["p1"].pages == ["Lỗi Mạng"]
    assert pm2.profiles["p2"].pages == ["Chạy Ads"]

    # Delete custom page "Chạy Ads"
    assert pm2.delete_custom_page("Chạy Ads")
    assert pm2.list_custom_pages() == ["Lỗi Mạng"]
    assert pm2.profiles["p2"].pages == []
