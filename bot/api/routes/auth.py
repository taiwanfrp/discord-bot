import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from redis.exceptions import RedisError

from bot.api.deps import (
    BotDep,
    DBDep,
    GuildDep,
    RedisDep,
    SessionRedisDep,
    Viewer,
    ViewerDep,
    api_session_key,
    read_api_session,
    resolve_viewer,
    ticket_session_key,
)
from bot.api.schemas import ExchangeIn, MeOut
from bot.core.config import get_settings
from bot.db.types import utcnow

router = APIRouter()


def me_out(viewer: Viewer) -> MeOut:
    return MeOut(
        id=viewer.id,
        username=viewer.user.name,
        global_name=viewer.user.global_name,
        nickname=viewer.member.nick if viewer.member else None,
        avatar_url=viewer.user.display_avatar.url,
        in_guild=viewer.member is not None,
        guild_id=get_settings().guild_id,
        is_admin=viewer.access.is_admin,
        category_ids=sorted(viewer.access.category_ids),
    )


@router.post("/auth/exchange")
async def exchange(
    body: ExchangeIn,
    request: Request,
    response: Response,
    bot: BotDep,
    guild: GuildDep,
    db: DBDep,
    redis: RedisDep,
    session_redis: SessionRedisDep,
) -> MeOut:
    """用主要 API 的授權碼換成 bot 的 session cookie（docs/auth.md 3.1）。"""
    settings = get_settings()
    try:
        # 授權碼只能使用一次
        api_session_hash = await session_redis.getdel(f"auth:ticket_code:{body.code}")
        # 用了 decode_responses，查得到時一定是 str
        if not isinstance(api_session_hash, str):
            raise HTTPException(401, "Invalid code")
        remaining = await session_redis.ttl(api_session_key(api_session_hash))
        discord_id = await read_api_session(session_redis, api_session_hash)
        # TTL -2：session 不存在
        if remaining == -2 or discord_id is None:
            raise HTTPException(401, "Session expired")
        # TTL -1：沒有設定過期；ticket session 不超過主要 API session 的剩餘時間
        ttl = settings.ticket_session_ttl
        if remaining != -1:
            ttl = min(ttl, remaining)

        token = secrets.token_urlsafe(32)
        key = ticket_session_key(token)
        async with redis.pipeline() as pipe:
            pipe.hset(
                key,
                mapping={"api_session_hash": api_session_hash, "created_at": utcnow().isoformat()},
            )
            pipe.expire(key, ttl)
            await pipe.execute()
    except RedisError as error:
        raise HTTPException(503, "Session store unavailable") from error

    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=ttl,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return me_out(await resolve_viewer(request, int(discord_id), bot, guild, db))


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, redis: RedisDep) -> None:
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    if token:
        try:
            await redis.delete(ticket_session_key(token))
        except RedisError as error:
            raise HTTPException(503, "Session store unavailable") from error
    response.delete_cookie(
        settings.auth_cookie_name, path="/", secure=True, httponly=True, samesite="lax"
    )


@router.get("/me")
async def me(viewer: ViewerDep) -> MeOut:
    return me_out(viewer)
