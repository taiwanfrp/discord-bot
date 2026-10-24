from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from bot.tickets.channel_name import validate_template

# Discord ID 在 JSON 裡一律用字串（超過 JavaScript 的安全整數範圍）
Snowflake = Annotated[int, PlainSerializer(str, return_type=str, when_used="json")]


class MeOut(BaseModel):
    id: Snowflake
    username: str
    global_name: str | None
    nickname: str | None
    avatar_url: str
    in_guild: bool
    is_admin: bool
    # 有分類身份組的分類（管理員可以存取所有分類）
    category_ids: list[int]


class ExchangeIn(BaseModel):
    code: str


class ChannelOut(BaseModel):
    id: Snowflake
    name: str
    # text、category
    type: str
    parent_id: Snowflake | None
    position: int


class RoleOut(BaseModel):
    id: Snowflake
    name: str
    color: int
    position: int
    hoist: bool
    mentionable: bool
    managed: bool
    # bot 能不能指派這個身份組（排在 bot 的身份組下面，而且不是整合服務管理的）
    assignable: bool


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: int = Field(0, ge=0, le=0xFFFFFF)
    hoist: bool = False
    mentionable: bool = False


class RolePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    color: int | None = Field(None, ge=0, le=0xFFFFFF)
    hoist: bool | None = None
    mentionable: bool | None = None


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    panel_channel_id: Snowflake | None
    panel_message_id: Snowflake | None
    log_channel_id: Snowflake | None
    default_discord_category_id: Snowflake | None
    channel_name_template: str
    max_open_tickets_per_user: int
    inactivity_remind_after_hours: int | None
    inactivity_close_after_hours: int | None
    max_attachment_size: int
    # null 表示使用內建的預設類型
    allowed_file_types: list[str] | None
    updated_at: datetime


# 副檔名，例如 png
FileType = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9]{1,16}$"),
    AfterValidator(str.lower),
]
Hours = Annotated[int, Field(ge=1, le=24 * 365)]


class SettingsPatch(BaseModel):
    """只會修改有送出的欄位。面板的頻道和訊息由 POST /panel/publish 設定。"""

    log_channel_id: Snowflake | None = None
    default_discord_category_id: Snowflake | None = None
    channel_name_template: str | None = Field(None, min_length=1, max_length=100)
    max_open_tickets_per_user: int | None = Field(None, ge=1, le=100)
    # null 表示停用
    inactivity_remind_after_hours: Hours | None = None
    inactivity_close_after_hours: Hours | None = None
    max_attachment_size: int | None = Field(None, ge=1, le=1024**3)
    # 副檔名（不含 .），null 表示使用內建的預設類型
    allowed_file_types: list[FileType] | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("channel_name_template", "max_open_tickets_per_user", "max_attachment_size"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能是 null")
        if self.channel_name_template is not None:
            validate_template(self.channel_name_template)
        if self.allowed_file_types is not None:
            self.allowed_file_types = sorted(set(self.allowed_file_types))
        return self


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    emoji: str | None
    description: str | None
    position: int
    enabled: bool
    # null 表示使用 settings.default_discord_category_id
    discord_category_id: Snowflake | None
    created_at: datetime
    updated_at: datetime


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    emoji: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=100)
    position: int = 0
    enabled: bool = True
    discord_category_id: Snowflake | None = None


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    emoji: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=100)
    position: int | None = None
    enabled: bool | None = None
    discord_category_id: Snowflake | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("name", "position", "enabled"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能是 null")
        return self


class CategoryRoles(BaseModel):
    category_id: int
    role_ids: list[Snowflake]


class PermissionRoles(BaseModel):
    """身份組的用途（docs/tickets.md 4.1）。PUT 時整份取代。"""

    admin_role_ids: list[Snowflake]
    category_roles: list[CategoryRoles]


class PanelPublishIn(BaseModel):
    channel_id: Snowflake


class PanelOut(BaseModel):
    channel_id: Snowflake
    message_id: Snowflake
