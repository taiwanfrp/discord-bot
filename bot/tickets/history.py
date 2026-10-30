"""訊息的附件與編輯前的內容，網頁與匯出共用同一套規則。

被刪除的訊息、編輯前的內容、編輯時移除的附件，只有這張單的客服、管理員（staff）看得到
（docs/tickets.md 8.1）。
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TicketAttachment, TicketMessage, TicketMessageRevision


async def visible_attachments(
    db: AsyncSession, messages: Sequence[TicketMessage], *, staff: bool
) -> dict[int, list[TicketAttachment]]:
    """訊息 id → 附件（依照 id 排序）。"""
    message_ids = [message.id for message in messages if staff or message.deleted_at is None]
    query = select(TicketAttachment).where(TicketAttachment.message_id.in_(message_ids))
    if not staff:
        query = query.where(TicketAttachment.removed_at.is_(None))
    result: dict[int, list[TicketAttachment]] = {}
    for attachment in await db.scalars(query.order_by(TicketAttachment.id)):
        assert attachment.message_id is not None
        result.setdefault(attachment.message_id, []).append(attachment)
    return result


async def visible_revisions(
    db: AsyncSession, messages: Sequence[TicketMessage], *, staff: bool
) -> dict[int, list[TicketMessageRevision]]:
    """訊息 id → 編輯前的內容（舊的在前面）；不是 staff 時一律是空的。"""
    if not staff:
        return {}
    result: dict[int, list[TicketMessageRevision]] = {}
    for revision in await db.scalars(
        select(TicketMessageRevision)
        .where(
            TicketMessageRevision.message_id.in_(
                [message.id for message in messages if message.edited_at is not None]
            )
        )
        .order_by(TicketMessageRevision.id)
    ):
        result.setdefault(revision.message_id, []).append(revision)
    return result
