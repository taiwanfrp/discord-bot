from typing import Annotated

from pydantic import BaseModel, PlainSerializer

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
    managed: bool
    # bot 能不能指派這個身份組（排在 bot 的身份組下面，而且不是整合服務管理的）
    assignable: bool
