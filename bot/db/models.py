from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base, Snowflake
from bot.db.types import EncryptedText, utcnow


class RoleKind(StrEnum):
    ADMIN = "admin"
    CATEGORY = "category"


class TicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"


class Source(StrEnum):
    DISCORD = "discord"
    WEB = "web"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class AttachmentStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"


class GuildSettings(Base):
    """全域設定，只有一筆，id 固定為 1。"""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_channel_id: Mapped[Snowflake | None]
    panel_message_id: Mapped[Snowflake | None]
    log_channel_id: Mapped[Snowflake | None]
    default_discord_category_id: Mapped[Snowflake | None]
    channel_name_template: Mapped[str] = mapped_column(String(100), default="客服單-{id}")
    max_open_tickets_per_user: Mapped[int] = mapped_column(default=3)
    inactivity_remind_after_hours: Mapped[int | None] = mapped_column(default=96)
    inactivity_close_after_hours: Mapped[int | None] = mapped_column(default=24)
    max_attachment_size: Mapped[int] = mapped_column(BigInteger, default=1024**3)
    # null 表示使用內建的預設類型
    allowed_file_types: Mapped[list[str] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TicketCategory(Base):
    __tablename__ = "ticket_category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    emoji: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    # null 表示使用 settings.default_discord_category_id
    discord_category_id: Mapped[Snowflake | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class PermissionRole(Base):
    __tablename__ = "permission_role"
    # category_id 是 null 時（管理員身份組），資料庫不會檢查重複，由程式負責
    __table_args__ = (UniqueConstraint("role_id", "kind", "category_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[Snowflake]
    kind: Mapped[RoleKind]
    # kind=category 時才有值
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_category.id", ondelete="CASCADE"), index=True
    )


class Ticket(Base):
    __tablename__ = "ticket"
    __table_args__ = (
        Index("ix_ticket_opener_id_status", "opener_id", "status"),
        Index("ix_ticket_status_last_message_at", "status", "last_message_at"),
        Index("ix_ticket_status_opener_added_at", "status", "opener_added_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ticket_category.id"), index=True)
    status: Mapped[TicketStatus] = mapped_column(default=TicketStatus.OPEN)
    source: Mapped[Source]

    channel_id: Mapped[Snowflake | None] = mapped_column(unique=True)
    webhook_id: Mapped[Snowflake | None]
    webhook_token: Mapped[str | None] = mapped_column(EncryptedText)

    opener_id: Mapped[Snowflake]
    # null 表示開單者還沒加進頻道
    opener_added_at: Mapped[datetime | None]
    subject: Mapped[str] = mapped_column(EncryptedText)
    content: Mapped[str] = mapped_column(EncryptedText)

    claimed_by_id: Mapped[Snowflake | None]
    claimed_at: Mapped[datetime | None]

    last_message_at: Mapped[datetime] = mapped_column(default=utcnow)
    inactivity_reminded_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    closed_at: Mapped[datetime | None]
    # null 表示系統自動關閉
    closed_by_id: Mapped[Snowflake | None]
    close_reason: Mapped[str | None] = mapped_column(EncryptedText)

    rating: Mapped[int | None]
    feedback: Mapped[str | None] = mapped_column(EncryptedText)
    rated_at: Mapped[datetime | None]


class TicketUser(Base):
    """客服單裡每個人的快照，關單後不再更新。"""

    __tablename__ = "ticket_user"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), primary_key=True)
    user_id: Mapped[Snowflake] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32))
    global_name: Mapped[str | None] = mapped_column(String(32))
    nickname: Mapped[str | None] = mapped_column(String(32))
    # null 表示使用預設頭像
    avatar_hash: Mapped[str | None] = mapped_column(String(64))
    discord_avatar_url: Mapped[str | None] = mapped_column(String(512))
    # null 表示還沒存到 S3
    avatar_key: Mapped[str | None] = mapped_column(String(255))
    # 最高的分隔顯示身份組
    role_name: Mapped[str | None] = mapped_column(String(100))
    role_color: Mapped[int | None]
    is_bot: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TicketMember(Base):
    """額外加入的成員。"""

    __tablename__ = "ticket_member"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), primary_key=True)
    user_id: Mapped[Snowflake] = mapped_column(primary_key=True)
    added_by_id: Mapped[Snowflake]
    added_at: Mapped[datetime] = mapped_column(default=utcnow)


class TicketMessage(Base):
    __tablename__ = "ticket_message"
    __table_args__ = (Index("ix_ticket_message_ticket_id_id", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"))
    source: Mapped[Source]
    # 唯一：同一則 Discord 訊息不會存兩次
    discord_message_id: Mapped[Snowflake | None] = mapped_column(unique=True)
    author_id: Mapped[Snowflake]
    content: Mapped[str] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    edited_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    # 網頁訊息才有
    delivery_status: Mapped[DeliveryStatus | None]
    attempts: Mapped[int] = mapped_column(default=0)


class TicketAttachment(Base):
    __tablename__ = "ticket_attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), index=True)
    # 網頁上傳時，送出訊息前是 null
    message_id: Mapped[int | None] = mapped_column(ForeignKey("ticket_message.id"), index=True)
    uploader_id: Mapped[Snowflake]
    source: Mapped[Source]
    # Discord 的附件 id，worker 用來重新取得下載網址（Discord 的網址會過期）
    discord_attachment_id: Mapped[Snowflake | None]
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[AttachmentStatus] = mapped_column(default=AttachmentStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    # 分享連結 token 的 SHA-256（hex），只有網頁上傳的附件才有
    share_token_hash: Mapped[str | None] = mapped_column(String(64))
    share_revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TicketEvent(Base):
    """時間軸與稽核紀錄；所有 SSE 事件都會寫在這裡。"""

    __tablename__ = "ticket_event"
    __table_args__ = (Index("ix_ticket_event_ticket_id_id", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"))
    type: Mapped[str] = mapped_column(String(64))
    # null 表示系統
    actor_id: Mapped[Snowflake | None]
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
