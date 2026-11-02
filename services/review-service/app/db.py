"""ORM-схема базы review_db и подключение к PostgreSQL."""
import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

log = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


class WorkRow(Base):
    __tablename__ = "works"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # уникальность submission_id делает обработку solution.accepted идемпотентной
    submission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    contest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    problem_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    participant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    jury: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    arbiter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)

    assignments: Mapped[list["AssignmentRow"]] = relationship(lazy="raise")


class AssignmentRow(Base):
    __tablename__ = "assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    work_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("works.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    role: Mapped[str] = mapped_column(String(16))
    slot: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppealRow(Base):
    __tablename__ = "appeals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    work_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("works.id", ondelete="CASCADE"), unique=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine, attempts: int = 30, delay: float = 2.0) -> None:
    for i in range(attempts):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:  # БД может стартовать дольше сервиса
            log.warning("PostgreSQL is not ready (%s), retry %s/%s", exc, i + 1, attempts)
            await asyncio.sleep(delay)
    raise RuntimeError("PostgreSQL is unavailable")
