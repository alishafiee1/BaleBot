from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # naive UTC: the DateTime columns are timezone-less (asyncpg rejects aware values for them)
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bale_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_quota: Mapped[int | None] = mapped_column(Integer, default=None)  # per-user override
    selected_model: Mapped[str | None] = mapped_column(String(128), default=None)
    context_reset_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Admin-tunable runtime settings (base model, prompt, default quota)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)


class UserKey(Base):
    """A user's own provider key. Only the encrypted blob is stored, never the key."""

    __tablename__ = "user_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(300))
    auth: Mapped[str] = mapped_column(String(16))
    chat_path: Mapped[str] = mapped_column(String(128))
    models_path: Mapped[str | None] = mapped_column(String(128), default=None)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    last4: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserModel(Base):
    """A model reachable through one of the user's keys (shown in that user's menu only)."""

    __tablename__ = "user_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("user_keys.id"), index=True)
    model: Mapped[str] = mapped_column(String(128))


class PendingAction(Base):
    """Where a user is in the add-key conversation. Never holds the key itself."""

    __tablename__ = "pending_actions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    step: Mapped[str] = mapped_column(String(16))
    data: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Sandbox(Base):
    """Bookkeeping for a user's sandbox container (the container itself lives in Incus)."""

    __tablename__ = "sandboxes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
