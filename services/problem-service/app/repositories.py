"""Репозитории агрегата «Задача».

ProblemRepository описывает интерфейс; InMemoryProblemRepository хранит данные
в памяти (модульные тесты, запуск без БД); SqlProblemRepository работает с
PostgreSQL. Сервисный слой не знает, какая реализация ему передана.
"""
from __future__ import annotations

import copy
import uuid
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db import CriterionRow, ProblemRow
from app.domain import Criterion, NotFound, Problem, ProblemStatus


class ConcurrencyError(Exception):
    """Агрегат изменён параллельно (оптимистическая блокировка)."""


class ProblemRepository(Protocol):
    async def add(self, problem: Problem) -> None: ...
    async def get(self, problem_id: uuid.UUID) -> Problem: ...
    async def save(self, problem: Problem) -> None: ...
    async def list(self, status: ProblemStatus | None, author_id: uuid.UUID | None,
                   offset: int, count: int) -> tuple[list[Problem], int]: ...


class InMemoryProblemRepository:
    def __init__(self) -> None:
        self._items: dict[uuid.UUID, Problem] = {}

    async def add(self, problem: Problem) -> None:
        if problem.id in self._items:
            raise ConcurrencyError(f"problem {problem.id} already exists")
        self._items[problem.id] = copy.deepcopy(problem)

    async def get(self, problem_id: uuid.UUID) -> Problem:
        if problem_id not in self._items:
            raise NotFound(f"problem {problem_id} not found")
        return copy.deepcopy(self._items[problem_id])

    async def save(self, problem: Problem) -> None:
        stored = self._items.get(problem.id)
        if stored is None:
            raise NotFound(f"problem {problem.id} not found")
        if stored.version != problem.version:
            raise ConcurrencyError("problem was modified concurrently")
        problem.version += 1
        self._items[problem.id] = copy.deepcopy(problem)

    async def list(self, status, author_id, offset, count):
        items = [p for p in self._items.values()
                 if (status is None or p.status is status)
                 and (author_id is None or p.author_id == author_id)]
        items.sort(key=lambda p: p.created_at, reverse=True)
        return [copy.deepcopy(p) for p in items[offset:offset + count]], len(items)


def _to_domain(row: ProblemRow) -> Problem:
    return Problem(
        id=row.id, title=row.title, statement=row.statement, author_id=row.author_id,
        status=ProblemStatus(row.status), created_at=row.created_at,
        published_at=row.published_at, version=row.version,
        criteria=[Criterion(id=c.id, name=c.name, max_points=c.max_points, position=c.position)
                  for c in sorted(row.criteria, key=lambda c: c.position)],
    )


def _criteria_rows(problem: Problem) -> list[CriterionRow]:
    return [CriterionRow(id=c.id, problem_id=problem.id, name=c.name,
                         max_points=c.max_points, position=c.position) for c in problem.criteria]


class SqlProblemRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def add(self, problem: Problem) -> None:
        async with self.sessions.begin() as s:
            s.add(ProblemRow(
                id=problem.id, title=problem.title, statement=problem.statement,
                author_id=problem.author_id, status=problem.status.value,
                created_at=problem.created_at, published_at=problem.published_at,
                version=problem.version,
            ))
            s.add_all(_criteria_rows(problem))

    async def get(self, problem_id: uuid.UUID) -> Problem:
        async with self.sessions() as s:
            row = await s.scalar(select(ProblemRow).options(selectinload(ProblemRow.criteria))
                                 .where(ProblemRow.id == problem_id))
            if row is None:
                raise NotFound(f"problem {problem_id} not found")
            return _to_domain(row)

    async def save(self, problem: Problem) -> None:
        """Сохраняет агрегат целиком в одной транзакции с проверкой версии."""
        async with self.sessions.begin() as s:
            result = await s.execute(
                update(ProblemRow)
                .where(ProblemRow.id == problem.id, ProblemRow.version == problem.version)
                .values(title=problem.title, statement=problem.statement,
                        status=problem.status.value, published_at=problem.published_at,
                        version=problem.version + 1)
            )
            if result.rowcount == 0:
                raise ConcurrencyError("problem was modified concurrently")
            await s.execute(delete(CriterionRow).where(CriterionRow.problem_id == problem.id))
            s.add_all(_criteria_rows(problem))
        problem.version += 1

    async def list(self, status, author_id, offset, count):
        query = select(ProblemRow).options(selectinload(ProblemRow.criteria))
        total = select(func.count()).select_from(ProblemRow)
        if status is not None:
            query = query.where(ProblemRow.status == status.value)
            total = total.where(ProblemRow.status == status.value)
        if author_id is not None:
            query = query.where(ProblemRow.author_id == author_id)
            total = total.where(ProblemRow.author_id == author_id)
        query = query.order_by(ProblemRow.created_at.desc()).offset(offset).limit(count)
        async with self.sessions() as s:
            rows = (await s.scalars(query)).all()
            return [_to_domain(r) for r in rows], await s.scalar(total)
