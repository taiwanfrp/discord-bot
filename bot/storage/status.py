from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AttachmentStatus, TicketAttachment, TicketEvent


async def finish(db: AsyncSession, attachment: TicketAttachment, status: AttachmentStatus) -> None:
    """附件處理完（Discord 的附件存到 S3、網頁上傳的檢查完），用 attachment.updated 通知網頁。"""
    attachment.status = status
    db.add(
        TicketEvent(
            ticket_id=attachment.ticket_id,
            type="attachment.updated",
            data={
                "attachment_id": str(attachment.id),
                "message_id": str(attachment.message_id) if attachment.message_id else None,
                "status": status.value,
            },
        )
    )
    await db.commit()
