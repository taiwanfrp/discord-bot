import discord
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TicketUser


def display_name(row: TicketUser) -> str:
    """跟 Discord 顯示名稱的規則一樣：伺服器暱稱 → 顯示名稱 → 帳號名稱。"""
    return row.nickname or row.global_name or row.username


def top_hoisted_role(member: discord.Member) -> discord.Role | None:
    """最高的分隔顯示身份組，紀錄頁面用它的顏色顯示名稱。"""
    hoisted = [role for role in member.roles if role.hoist]
    return max(hoisted, key=lambda role: role.position, default=None)


async def update_snapshot(
    db: AsyncSession, ticket_id: int, user: discord.User | discord.Member
) -> TicketUser:
    """更新使用者在這張客服單裡的快照（名稱與頭像，docs/tickets.md 8.6）。

    呼叫的人負責 commit；客服單關閉後不要再呼叫。
    """
    row = await db.get(TicketUser, (ticket_id, user.id))
    if row is None:
        row = TicketUser(ticket_id=ticket_id, user_id=user.id)
        db.add(row)

    member = user if isinstance(user, discord.Member) else None
    # 伺服器頭像優先，沒有自訂頭像時是 None（前端顯示預設頭像）
    avatar = (member.guild_avatar if member else None) or user.avatar
    role = top_hoisted_role(member) if member else None

    row.username = user.name
    row.global_name = user.global_name
    row.nickname = member.nick if member else None
    if row.avatar_hash != (avatar.key if avatar else None):
        row.avatar_hash = avatar.key if avatar else None
        # 靜態頭像用 webp、動態頭像用 gif（mirror_avatars 存到 S3 的格式也一樣）
        row.discord_avatar_url = (
            avatar.replace(size=256, static_format="webp").url if avatar else None
        )
        # 頭像換了，舊的 S3 檔案不再使用（mirror_avatars 會存新的）
        row.avatar_key = None
    row.role_name = role.name if role else None
    row.role_color = role.color.value if role else None
    row.is_bot = user.bot
    return row
