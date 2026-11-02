"""Репозитории агрегатов «Работа на проверку» и «Апелляция».

Агрегат сохраняется целиком в одной транзакции; версия работы защищает от
параллельных изменений (оптимистическая блокировка).
"""
from __future__ import annotations

import copy
import uuid
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db import AppealRow, AssignmentRow, WorkRow
from app.domain import (Appeal, AppealStatus, Assignment, AssignmentStatus, NotFound, Role, Work,
                        WorkStatus)


class ConcurrencyError(Exception):
    """Агрегат изменён параллельно."""


class DuplicateError(Exception):
    """Нарушена уникальность (работа по этому решению или апелляция уже есть)."""


class WorkRepository(Protocol):
    async def add(self, work: Work) -> None: ...
    async def get(self, work_id: uuid.UUID) -> Work: ...
    async def find_by_submission(self, submission_id: uuid.UUID) -> Work | None: ...
    async def save(self, work: Work) -> None: ...
    async def list(self, judge_id, status, contest_id, offset, count) -> tuple[list[Work], int]: ...
    async def active_load(self, reviewer_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]: ...


class AppealRepository(Protocol):
    async def add(self, appeal: Appeal) -> None: ...
    async def get(self, appeal_id: uuid.UUID) -> Appeal: ...
    async def save(self, appeal: Appeal) -> None: ...


# ======================================================================= память
class InMemoryWorkRepository:
    def __init__(self) -> None:
        self._items: dict[uuid.UUID, Work] = {}

    async def add(self, work: Work) -> None:
        if any(w.submission_id == work.submission_id for w in self._items.values()):
            raise DuplicateError(f"work for submission {work.submission_id} already exists")
        self._items[work.id] = copy.deepcopy(work)

    async def get(self, work_id: uuid.UUID) -> Work:
        if work_id not in self._items:
            raise NotFound(f"work {work_id} not found")
        return copy.deepcopy(self._items[work_id])

    async def find_by_submission(self, submission_id):
        w = next((w for w in self._items.values() if w.submission_id == submission_id), None)
        return copy.deepcopy(w) if w else None

    async def save(self, work: Work) -> None:
        stored = self._items.get(work.id)
        if stored is None:
            raise NotFound(f"work {work.id} not found")
        if stored.version != work.version:
            raise ConcurrencyError("work was modified concurrently")
        work.version += 1
        self._items[work.id] = copy.deepcopy(work)

    async def list(self, judge_id, status, contest_id, offset, count):
        items = [w for w in self._items.values()
                 if (status is None or w.status is status)
                 and (contest_id is None or w.contest_id == contest_id)
                 and (judge_id is None or w.active_assignment_of(judge_id) is not None)]
        items.sort(key=lambda w: w.created_at)
        return [copy.deepcopy(w) for w in items[offset:offset + count]], len(items)

    async def active_load(self, reviewer_ids):
        load: dict[uuid.UUID, int] = {}
        for w in self._items.values():
            for a in w.assignments:
                if a.status is AssignmentStatus.ACTIVE and a.reviewer_id in reviewer_ids:
                    load[a.reviewer_id] = load.get(a.reviewer_id, 0) + 1
        return load


class InMemoryAppealRepository:
    def __init__(self) -> None:
        self._items: dict[uuid.UUID, Appeal] = {}

    async def add(self, appeal: Appeal) -> None:
        if any(a.work_id == appeal.work_id for a in self._items.values()):
            raise DuplicateError("appeal for this work already exists")
        self._items[appeal.id] = copy.deepcopy(appeal)

    async def get(self, appeal_id):
        if appeal_id not in self._items:
            raise NotFound(f"appeal {appeal_id} not found")
        return copy.deepcopy(self._items[appeal_id])

    async def save(self, appeal: Appeal) -> None:
        self._items[appeal.id] = copy.deepcopy(appeal)


# ======================================================================= PostgreSQL
def _assignment_row(a: Assignment) -> AssignmentRow:
    return AssignmentRow(id=a.id, work_id=a.work_id, reviewer_id=a.reviewer_id, role=a.role.value,
                         slot=a.slot, attempt=a.attempt, status=a.status.value,
                         created_at=a.created_at, deadline_at=a.deadline_at, score=a.score,
                         comment=a.comment, graded_at=a.graded_at)


def _work_values(w: Work) -> dict:
    return dict(status=w.status.value, final_score=w.final_score, max_score=w.max_score,
                approved_by=w.approved_by.value if w.approved_by else None,
                approved_at=w.approved_at, comment=w.comment)


def _to_work(row: WorkRow) -> Work:
    return Work(
        id=row.id, submission_id=row.submission_id, contest_id=row.contest_id,
        problem_id=row.problem_id, participant_id=row.participant_id, jury=list(row.jury),
        arbiter_id=row.arbiter_id, status=WorkStatus(row.status), created_at=row.created_at,
        final_score=row.final_score, max_score=row.max_score,
        approved_by=Role(row.approved_by) if row.approved_by else None,
        approved_at=row.approved_at, comment=row.comment, version=row.version,
        assignments=[Assignment(id=a.id, work_id=a.work_id, reviewer_id=a.reviewer_id,
                                role=Role(a.role), slot=a.slot, attempt=a.attempt,
                                status=AssignmentStatus(a.status), created_at=a.created_at,
                                deadline_at=a.deadline_at, score=a.score, comment=a.comment,
                                graded_at=a.graded_at)
                     for a in sorted(row.assignments, key=lambda a: a.created_at)],
    )


class SqlWorkRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def add(self, work: Work) -> None:
        try:
            async with self.sessions.begin() as s:
                s.add(WorkRow(id=work.id, submission_id=work.submission_id,
                              contest_id=work.contest_id, problem_id=work.problem_id,
                              participant_id=work.participant_id, jury=work.jury,
                              arbiter_id=work.arbiter_id, created_at=work.created_at,
                              version=work.version, **_work_values(work)))
                await s.flush()
                s.add_all(_assignment_row(a) for a in work.assignments)
        except IntegrityError as exc:
            raise DuplicateError(f"work for submission {work.submission_id} already exists") from exc

    async def _one(self, condition) -> WorkRow | None:
        async with self.sessions() as s:
            return await s.scalar(select(WorkRow).options(selectinload(WorkRow.assignments))
                                  .where(condition))

    async def get(self, work_id: uuid.UUID) -> Work:
        row = await self._one(WorkRow.id == work_id)
        if row is None:
            raise NotFound(f"work {work_id} not found")
        return _to_work(row)

    async def find_by_submission(self, submission_id):
        row = await self._one(WorkRow.submission_id == submission_id)
        return _to_work(row) if row else None

    async def save(self, work: Work) -> None:
        async with self.sessions.begin() as s:
            result = await s.execute(
                update(WorkRow).where(WorkRow.id == work.id, WorkRow.version == work.version)
                .values(version=work.version + 1, **_work_values(work)))
            if result.rowcount == 0:
                raise ConcurrencyError("work was modified concurrently")
            for a in work.assignments:
                await s.merge(_assignment_row(a))
        work.version += 1

    async def list(self, judge_id, status, contest_id, offset, count):
        conditions = []
        if status is not None:
            conditions.append(WorkRow.status == status.value)
        if contest_id is not None:
            conditions.append(WorkRow.contest_id == contest_id)
        if judge_id is not None:
            conditions.append(WorkRow.id.in_(
                select(AssignmentRow.work_id).where(
                    AssignmentRow.reviewer_id == judge_id,
                    AssignmentRow.status == AssignmentStatus.ACTIVE.value)))
        query = (select(WorkRow).options(selectinload(WorkRow.assignments)).where(*conditions)
                 .order_by(WorkRow.created_at).offset(offset).limit(count))
        async with self.sessions() as s:
            rows = (await s.scalars(query)).all()
            total = await s.scalar(select(func.count()).select_from(WorkRow).where(*conditions))
            return [_to_work(r) for r in rows], total

    async def active_load(self, reviewer_ids):
        if not reviewer_ids:
            return {}
        query = (select(AssignmentRow.reviewer_id, func.count())
                 .where(AssignmentRow.reviewer_id.in_(reviewer_ids),
                        AssignmentRow.status == AssignmentStatus.ACTIVE.value)
                 .group_by(AssignmentRow.reviewer_id))
        async with self.sessions() as s:
            return {rid: n for rid, n in (await s.execute(query)).all()}


class SqlAppealRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def _row(a: Appeal) -> AppealRow:
        return AppealRow(id=a.id, work_id=a.work_id, participant_id=a.participant_id,
                         reason=a.reason, status=a.status.value, created_at=a.created_at,
                         old_score=a.old_score, new_score=a.new_score, comment=a.comment,
                         resolved_by=a.resolved_by, resolved_at=a.resolved_at)

    async def add(self, appeal: Appeal) -> None:
        try:
            async with self.sessions.begin() as s:
                s.add(self._row(appeal))
        except IntegrityError as exc:
            raise DuplicateError("appeal for this work already exists") from exc

    async def get(self, appeal_id):
        async with self.sessions() as s:
            r = await s.get(AppealRow, appeal_id)
            if r is None:
                raise NotFound(f"appeal {appeal_id} not found")
            return Appeal(id=r.id, work_id=r.work_id, participant_id=r.participant_id,
                          reason=r.reason, status=AppealStatus(r.status), created_at=r.created_at,
                          old_score=r.old_score, new_score=r.new_score, comment=r.comment,
                          resolved_by=r.resolved_by, resolved_at=r.resolved_at)

    async def save(self, appeal: Appeal) -> None:
        async with self.sessions.begin() as s:
            await s.merge(self._row(appeal))
