"""ORM-схема базы problem_db и подключение к PostgreSQL."""
import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

log = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


class ProblemRow(Base):
    __tablename__ = "problems"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    statement: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)

    criteria: Mapped[list["CriterionRow"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan", lazy="raise")


class CriterionRow(Base):
    __tablename__ = "criteria"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    max_points: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)

    problem: Mapped[ProblemRow] = relationship(back_populates="criteria")


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine, attempts: int = 30, delay: float = 2.0) -> None:
    """Создаёт таблицы; ждёт, пока PostgreSQL примет подключение."""
    for i in range(attempts):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:  # ConnectionRefused, CannotConnectNow и т.п. при старте БД
            log.warning("PostgreSQL is not ready (%s), retry %s/%s", exc, i + 1, attempts)
            await asyncio.sleep(delay)
    raise RuntimeError("PostgreSQL is unavailable")
