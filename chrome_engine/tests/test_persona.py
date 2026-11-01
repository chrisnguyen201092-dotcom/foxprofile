import json

import pytest

from chrome_engine import persona as personas
from chrome_engine.persona import Persona, PersonaError


def test_generate_is_deterministic_per_seed_and_platform():
    a = personas.generate("windows", seed=12345)
    b = personas.generate("windows", seed=12345)
    assert a == b
    assert personas.generate("macos", seed=12345).platform == "macos"


def test_generate_varies_with_seed():
    values = {
        (p.platform_version, p.hardware_concurrency)
        for p in (personas.generate("windows", seed=s) for s in range(1, 200))
    }
    assert len(values) > 5


@pytest.mark.parametrize("platform", personas.PLATFORMS)
def test_generated_values_are_valid(platform):
    for seed in range(1, 100):
        p = personas.generate(platform, seed=seed)
        personas.validate(p)
        assert 1 <= p.seed <= personas.SEED_MAX


def test_random_seed_when_none():
    seeds = {personas.generate("linux").seed for _ in range(20)}
    assert len(seeds) > 15


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"seed": 0}, "seed"),
        ({"seed": 2**31}, "seed"),
        ({"platform": "android"}, "platform"),
        ({"platform_version": "15"}, "platform_version"),
        ({"brand": "Firefox"}, "brand"),
        ({"brand": "Edge", "platform": "linux", "platform_version": "6.8.0"}, "Edge"),
        ({"hardware_concurrency": 0}, "hardware_concurrency"),
        ({"hardware_concurrency": 128}, "hardware_concurrency"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"locale": "english"}, "locale"),
        ({"brand_version": "v148"}, "brand_version"),
        ({"gpu_vendor": "   "}, "gpu_vendor"),
        ({"gpu_renderer": ""}, "gpu_renderer"),
        ({"screen_width": 1920}, "screen_width and screen_height"),
        ({"screen_width": 100, "screen_height": 100}, "valid dimensions"),
    ],
)
def test_validation(changes, message):
    base = {"seed": 7, "platform": "windows", "platform_version": "15.0.0"}
    with pytest.raises(PersonaError, match=message):
        Persona(**{**base, **changes})


def test_to_args():
    p = Persona(
        seed=42,
        platform="macos",
        platform_version="15.6.1",
        brand="Edge",
        hardware_concurrency=10,
        timezone="Asia/Tokyo",
        locale="ja-JP",
    )
    assert p.to_args() == [
        "--fingerprint=42",
        "--fingerprint-platform=macos",
        "--fingerprint-platform-version=15.6.1",
        "--fingerprint-brand=Edge",
        "--fingerprint-hardware-concurrency=10",
        "--timezone=Asia/Tokyo",
        "--lang=ja-JP",
        "--accept-lang=ja-JP,ja,en-US,en",
    ]


def test_to_args_with_hardware_and_brand_overrides():
    p = Persona(
        seed=42,
        platform="windows",
        platform_version="19.0.0",
        brand="Chrome",
        hardware_concurrency=16,
        brand_version="148.0.7778.215",
        gpu_vendor="Google Inc. (NVIDIA)",
        gpu_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        screen_width=2560,
        screen_height=1440,
    )
    args = p.to_args()
    assert "--fingerprint-brand-version=148.0.7778.215" in args
    assert "--fingerprint-gpu-vendor=Google Inc. (NVIDIA)" in args
    assert (
        "--fingerprint-gpu-renderer=ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        in args
    )
    assert p.effective_screen() == (2560, 1440)
    assert p.effective_gpu() == (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    )


def test_effective_screen_and_gpu_are_deterministic_per_seed():
    p1 = personas.generate("windows", seed=8888)
    p2 = personas.generate("windows", seed=8888)
    assert p1.effective_screen() == p2.effective_screen()
    assert p1.effective_gpu() == p2.effective_gpu()
    w, h = p1.effective_screen()
    assert w >= 1280 and h >= 720
    vendor, renderer = p1.effective_gpu()
    assert "Google Inc." in vendor
    assert "Direct3D11" in renderer


def test_to_args_leaves_geo_to_the_ip():
    args = Persona(seed=1, platform="windows", platform_version="10.0.0").to_args()
    assert not any(a.startswith(("--timezone", "--lang", "--accept-lang")) for a in args)


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("vi-VN", "vi-VN,vi,en-US,en"),
        ("en-US", "en-US,en"),
        ("en-GB", "en-GB,en"),
        ("fr", "fr,en-US,en"),
    ],
)
def test_accept_languages(locale, expected):
    assert personas.accept_languages(locale) == expected


def test_with_geo_only_fills_open_fields():
    pinned = Persona(seed=1, platform="windows", platform_version="10.0.0", timezone="Europe/Paris")
    filled = pinned.with_geo("Asia/Tokyo", "ja-JP")
    assert filled.timezone == "Europe/Paris"
    assert filled.locale == "ja-JP"


def test_json_roundtrip_ignores_unknown_keys():
    p = personas.generate(
        "windows",
        seed=99,
        locale="vi-VN",
        gpu_vendor="Google Inc. (Intel)",
        gpu_renderer="ANGLE (Intel, Iris Xe Direct3D11 vs_5_0 ps_5_0, D3D11)",
        screen_width=1920,
        screen_height=1080,
    )
    data = {**p.to_json(), "future_field": 1}
    assert Persona.from_json(data) == p


def test_load_or_create_persists(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    assert personas.persona_path(tmp_path).is_file()
    assert personas.load_or_create(tmp_path, "windows") == first


def test_load_or_create_regenerates_when_os_changes(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    second = personas.load_or_create(tmp_path, "macos")
    assert second.platform == "macos"
    assert personas.load(tmp_path) == second
    assert second != first


def test_overrides_keep_the_device(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    pinned = personas.load_or_create(tmp_path, "windows", timezone="Asia/Tokyo", locale="ja-JP")
    assert (pinned.seed, pinned.platform_version) == (first.seed, first.platform_version)
    assert (pinned.timezone, pinned.locale) == ("Asia/Tokyo", "ja-JP")
    # None leaves the saved value, "" clears it.
    kept = personas.load_or_create(tmp_path, "windows", timezone=None)
    assert kept.timezone == "Asia/Tokyo"
    cleared = personas.load_or_create(tmp_path, "windows", timezone="", locale="")
    assert (cleared.timezone, cleared.locale) == (None, None)
    assert cleared.seed == first.seed


def test_corrupt_file_is_regenerated(tmp_path):
    personas.persona_path(tmp_path).write_text("{not json", encoding="utf-8")
    p = personas.load_or_create(tmp_path, "linux")
    assert json.loads(personas.persona_path(tmp_path).read_text(encoding="utf-8"))["seed"] == p.seed


def test_invalid_saved_values_are_regenerated(tmp_path):
    personas.persona_path(tmp_path).write_text(
        json.dumps({"seed": -1, "platform": "windows"}), "utf-8"
    )
    assert personas.load(tmp_path) is None
    assert personas.load_or_create(tmp_path, "windows").seed > 0


@pytest.mark.parametrize(
    ("host", "platform", "level"),
    [
        ("windows", "windows", personas.OK),
        ("windows", "macos", personas.WARN),
        ("windows", "linux", personas.UNSUPPORTED),
        ("linux", "windows", personas.WARN),
        ("macos", "macos", personas.OK),
        ("macos", "windows", personas.WARN),
        ("macos", "linux", personas.UNSUPPORTED),
    ],
)
def test_compatibility(host, platform, level):
    assert personas.compatibility(host, platform)[0] == level
