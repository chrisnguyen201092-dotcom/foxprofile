from pydantic import BaseModel, Field


class ProfileCreate(BaseModel):
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    timezone: str | None = Field(
        None, description="IANA timezone, e.g. Asia/Ho_Chi_Minh. Unset = follow the IP"
    )
    locale: str | None = Field(
        None, description="language-REGION, e.g. vi-VN. Unset = follow the IP"
    )
    engine: str = Field(
        "camoufox",
        description="camoufox (Firefox) or chrome (experimental, fingerprint-chromium). "
        "Fixed once the profile is created",
    )
    color: str | None = Field(None, description="Optional #RRGGBB highlight color. Unset = default")
    group: str | None = Field(None, description="Optional group/folder name")
    notes: str | None = Field(None, description="Optional free-text notes")
    pages: list[str] = Field(
        default_factory=list, description="Custom pages this profile belongs to"
    )


class ProfileUpdate(BaseModel):
    """All fields optional — only supplied fields are changed."""

    name: str | None = None
    proxy: str | None = None
    os_type: str | None = None
    timezone: str | None = Field(None, description='"" switches back to automatic')
    locale: str | None = Field(None, description='"" switches back to automatic')
    engine: str | None = Field(None, description="Cannot be changed; only the current value")
    color: str | None = Field(None, description='"" switches back to default (no color)')
    group: str | None = Field(None, description='"" clears the group')
    notes: str | None = Field(None, description='"" clears the notes')
    pages: list[str] | None = Field(None, description="Custom pages this profile belongs to")


class CustomPageRequest(BaseModel):
    name: str
    profile_names: list[str] | None = None
    original_name: str | None = None


class CustomPageMembersRequest(BaseModel):
    profile_names: list[str]


class ProfileOrderRequest(BaseModel):
    names: list[str]


class ProfileMoveRequest(BaseModel):
    direction: int = Field(..., description="-1 to move up, 1 to move down")


class ProfileResponse(BaseModel):
    name: str
    proxy: str | None
    os_type: str
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    color: str | None = None
    group: str | None = None
    notes: str | None = None
    pages: list[str] = Field(default_factory=list)
    data_dir: str
    is_running: bool


class ProfileListResponse(BaseModel):
    profiles: list[ProfileResponse]
    total: int
    custom_pages: list[str] = Field(default_factory=list)


class DataDirResponse(BaseModel):
    name: str
    data_dir: str
    exists: bool


class ExportRequest(BaseModel):
    export_dir: str
    include_data: bool = True


class ExportResponse(BaseModel):
    success: bool
    zip_path: str | None = None
    error: str | None = None


class ImportRequest(BaseModel):
    zip_path: str
    overwrite: bool = False


class ImportResponse(BaseModel):
    success: bool
    profile_name: str | None = None
    error: str | None = None


class CookieImportRequest(BaseModel):
    """Cookies as JSON (Cookie-Editor / EditThisCookie / Playwright) or Netscape text."""

    content: str


class CookieImportResponse(BaseModel):
    success: bool
    imported: int = 0
    error: str | None = None


class FingerprintResponse(BaseModel):
    name: str
    exists: bool
    os: str | None = None
    platform: str | None = None
    screen: str | None = None
    hardware_concurrency: int | None = None
