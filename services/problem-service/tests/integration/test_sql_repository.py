"""Интеграционные тесты репозитория problem-service на настоящем PostgreSQL.

Запуск: TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/postgres pytest tests/integration
Без переменной окружения тесты пропускаются.
"""
import os
import uuid

import pytest

from app.db import Base, make_engine, make_sessions
from app.domain import NotFound, Problem, ProblemStatus
from app.repositories import ConcurrencyError, SqlProblemRepository

URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_POSTGRES_URL is not set")

AUTHOR = uuid.uuid4()


@pytest.fixture()
async def repo():
    engine = make_engine(URL)
    async with engine.begin() as conn:            # чистая схема для каждого теста
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield SqlProblemRepository(make_sessions(engine))
    await engine.dispose()


def new_problem(author=AUTHOR, title="Сумма"):
    return Problem.create(title, "Сложите два числа", author)


async def test_add_and_get_roundtrip(repo):
    p = new_problem()
    p.set_criteria([("Корректность", 6), ("Эффективность", 3), ("Стиль", 1)])
    await repo.add(p)
    loaded = await repo.get(p.id)
    assert loaded.title == "Сумма" and loaded.status is ProblemStatus.DRAFT
    assert [(c.name, c.max_points) for c in loaded.criteria] == [
        ("Корректность", 6), ("Эффективность", 3), ("Стиль", 1)]
    assert loaded.max_score == 10


async def test_get_unknown_problem_raises_not_found(repo):
    with pytest.raises(NotFound):
        await repo.get(uuid.uuid4())


async def test_save_replaces_criteria_and_increments_version(repo):
    p = new_problem()
    p.set_criteria([("Корректность", 5)])
    await repo.add(p)
    p = await repo.get(p.id)
    p.set_criteria([("Стиль", 2), ("Тесты", 8)])
    p.publish()
    await repo.save(p)
    loaded = await repo.get(p.id)
    assert loaded.version == 1 and loaded.status is ProblemStatus.PUBLISHED
    assert [c.name for c in loaded.criteria] == ["Стиль", "Тесты"]


async def test_concurrent_update_is_rejected(repo):
    p = new_problem()
    await repo.add(p)
    first, second = await repo.get(p.id), await repo.get(p.id)
    first.set_criteria([("Корректность", 5)])
    await repo.save(first)
    second.set_criteria([("Стиль", 5)])
    with pytest.raises(ConcurrencyError):
        await repo.save(second)


async def test_list_filters_by_status_and_author_with_pagination(repo):
    other = uuid.uuid4()
    for i in range(3):
        await repo.add(new_problem(title=f"Задача {i}"))
    published = new_problem(other, "Опубликованная")
    published.set_criteria([("Корректность", 5)])
    published.publish()
    await repo.add(published)

    items, total = await repo.list(None, AUTHOR, 0, 2)
    assert (len(items), total) == (2, 3)
    items, total = await repo.list(ProblemStatus.PUBLISHED, None, 0, 10)
    assert total == 1 and items[0].id == published.id
