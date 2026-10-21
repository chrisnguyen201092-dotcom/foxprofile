from dataclasses import asdict, dataclass, field

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

# Optional highlight colors for profile rows on the UI (None = default, no bar).
PROFILE_COLOR_PALETTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "palette_vibrant",
        (
            "#EF4444",  # Red
            "#F97316",  # Orange
            "#EAB308",  # Amber
            "#22C55E",  # Green
            "#06B6D4",  # Cyan
            "#3B82F6",  # Blue
            "#8B5CF6",  # Violet
            "#EC4899",  # Pink
        ),
    ),
    (
        "palette_deep",
        (
            "#B91C1C",  # Crimson
            "#C2410C",  # Rust
            "#A16207",  # Bronze
            "#15803D",  # Forest
            "#0E7490",  # Teal
            "#1D4ED8",  # Royal Blue
            "#6D28D9",  # Indigo
            "#BE185D",  # Magenta
        ),
    ),
    (
        "palette_pastel",
        (
            "#F87171",  # Coral
            "#FB923C",  # Peach
            "#FACC15",  # Lemon
            "#4ADE80",  # Mint
            "#22D3EE",  # Sky
            "#60A5FA",  # Periwinkle
            "#A78BFA",  # Lavender
            "#F472B6",  # Rose
        ),
    ),
    (
        "palette_earth",
        (
            "#78350F",  # Espresso
            "#92400E",  # Terracotta
            "#3F6212",  # Olive
            "#115E59",  # Pine
            "#1E3A8A",  # Navy
            "#4C1D95",  # Plum
            "#475569",  # Slate
            "#1E293B",  # Charcoal
        ),
    ),
)

PROFILE_COLORS: tuple[str, ...] = tuple(c for _, group in PROFILE_COLOR_PALETTES for c in group)


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    color: str | None = None
    group: str | None = None
    notes: str | None = None
    pages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
