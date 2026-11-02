"""Интеграционные тесты репозиториев review-service на настоящем PostgreSQL.

Запуск: TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/postgres pytest tests/integration
Без переменной окружения тесты пропускаются.
"""
import os
import uuid
from datetime import timedelta

import pytest

from app.db import Base, make_engine, make_sessions
from app.domain import Appeal, AssignmentStatus, Work, WorkStatus, utcnow
from app.repositories import (ConcurrencyError, DuplicateError, SqlAppealRepository,
                              SqlWorkRepository)

URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_POSTGRES_URL is not set")

DEADLINE = timedelta(minutes=30)


@pytest.fixture()
async def sessions():
    engine = make_engine(URL)
    async with engine.begin() as conn:            # чистая схема для каждого теста
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessions(engine)
    await engine.dispose()


@pytest.fixture()
def repo(sessions):
    return SqlWorkRepository(sessions)


def new_work(jury=None, contest_id=None):
    jury = jury or [uuid.uuid4() for _ in range(3)]
    work, _ = Work.start(uuid.uuid4(), contest_id or uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                         jury, uuid.uuid4(), utcnow(), DEADLINE, {})
    return work


async def test_add_and_get_roundtrip(repo):
    work = new_work()
    await repo.add(work)
    loaded = await repo.get(work.id)
    assert loaded.submission_id == work.submission_id
    assert loaded.jury == work.jury
    assert [a.id for a in loaded.assignments] == [a.id for a in work.assignments]
    assert all(a.deadline_at is not None for a in loaded.assignments)


async def test_second_work_for_same_submission_is_rejected(repo):
    work = new_work()
    await repo.add(work)
    duplicate = new_work()
    duplicate.submission_id = work.submission_id
    with pytest.raises(DuplicateError):
        await repo.add(duplicate)


async def test_save_persists_grades_and_new_assignments(repo):
    work = new_work()
    await repo.add(work)
    work = await repo.get(work.id)
    j1 = work.assignments[0].reviewer_id
    work.grade(j1, 7, 10, "ok", utcnow())
    work.decline(work.assignments[1].reviewer_id, utcnow(), DEADLINE, {})
    await repo.save(work)

    loaded = await repo.get(work.id)
    assert loaded.version == 1
    assert [a.status for a in loaded.assignments] == [
        AssignmentStatus.GRADED, AssignmentStatus.DECLINED, AssignmentStatus.ACTIVE]
    assert loaded.assignments[0].score == 7


async def test_stale_version_is_rejected(repo):
    work = new_work()
    await repo.add(work)
    first, second = await repo.get(work.id), await repo.get(work.id)
    first.grade(first.assignments[0].reviewer_id, 5, 10, None, utcnow())
    await repo.save(first)
    second.grade(second.assignments[1].reviewer_id, 5, 10, None, utcnow())
    with pytest.raises(ConcurrencyError):
        await repo.save(second)


async def test_list_filters_by_judge_status_and_contest(repo):
    judges = [uuid.uuid4() for _ in range(3)]
    contest = uuid.uuid4()
    works = [new_work(judges, contest) for _ in range(3)]
    for w in works:
        await repo.add(w)
    busy = works[0].assignments[0].reviewer_id

    items, total = await repo.list(busy, None, None, 0, 10)
    assert total == len([w for w in works if w.active_assignment_of(busy)])
    assert all(w.active_assignment_of(busy) for w in items)

    items, total = await repo.list(None, WorkStatus.IN_REVIEW, contest, 0, 2)
    assert (len(items), total) == (2, 3)
    assert (await repo.list(None, WorkStatus.APPROVED, contest, 0, 10))[1] == 0


async def test_active_load_counts_active_assignments(repo):
    judges = [uuid.uuid4() for _ in range(2)]
    for _ in range(3):
        await repo.add(new_work(judges))
    load = await repo.active_load(judges)
    assert load == {judges[0]: 3, judges[1]: 3}


async def test_appeal_roundtrip_and_uniqueness(repo, sessions):
    appeals = SqlAppealRepository(sessions)
    work = new_work()
    j1, j2 = (a.reviewer_id for a in work.assignments)
    work.grade(j1, 6, 10, None, utcnow())
    work.grade(j2, 6, 10, None, utcnow())
    await repo.add(work)

    appeal = Appeal.file(work, work.participant_id, "учтите стиль", utcnow())
    await appeals.add(appeal)
    appeal.resolve(work, work.arbiter_id, 8, "учтено", utcnow())
    await appeals.save(appeal)
    loaded = await appeals.get(appeal.id)
    assert (loaded.status.value, loaded.old_score, loaded.new_score) == ("resolved", 6, 8)

    with pytest.raises(DuplicateError):
        await appeals.add(Appeal.file(work, work.participant_id, "ещё раз", utcnow()))
