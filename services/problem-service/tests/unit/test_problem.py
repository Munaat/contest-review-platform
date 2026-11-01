"""Модульные тесты агрегата «Задача» и сервисного слоя (in-memory репозиторий)."""
import uuid

import pytest

from app.domain import DomainError, InvalidInput, NotFound, Problem, ProblemStatus
from app.repositories import ConcurrencyError, InMemoryProblemRepository
from app.service import ProblemService

AUTHOR = uuid.uuid4()


@pytest.fixture()
def problem():
    return Problem.create("Сумма", "Сложите два числа", AUTHOR)


@pytest.fixture()
def service():
    return ProblemService(InMemoryProblemRepository())


# ---------------------------------------------------------------- агрегат
def test_new_problem_is_draft_without_criteria(problem):
    assert problem.status is ProblemStatus.DRAFT
    assert problem.criteria == [] and problem.max_score == 0


@pytest.mark.parametrize("title, statement", [("  ", "условие"), ("Название", "")])
def test_empty_title_or_statement_is_rejected(title, statement):
    with pytest.raises(InvalidInput):
        Problem.create(title, statement, AUTHOR)


def test_max_score_is_sum_of_criteria(problem):
    problem.set_criteria([("Корректность", 6), ("Стиль", 4)])
    assert problem.max_score == 10
    assert [c.position for c in problem.criteria] == [0, 1]


@pytest.mark.parametrize("items", [
    [],                                     # нет критериев
    [("a", 1)] * 11,                        # больше десяти
    [("Стиль", 2), ("стиль", 3)],           # повтор без учёта регистра
    [("Корректность", 0)],                  # балл меньше 1
    [("Корректность", 101)],                # балл больше 100
    [("   ", 5)],                           # пустое название
])
def test_invalid_criteria_are_rejected(problem, items):
    with pytest.raises(InvalidInput):
        problem.set_criteria(items)


def test_publish_requires_criteria(problem):
    with pytest.raises(DomainError):
        problem.publish()


def test_published_problem_is_immutable(problem):
    problem.set_criteria([("Корректность", 10)])
    problem.publish()
    assert problem.status is ProblemStatus.PUBLISHED and problem.published_at is not None
    with pytest.raises(DomainError):
        problem.set_criteria([("Стиль", 5)])
    with pytest.raises(DomainError):
        problem.publish()


# ---------------------------------------------------------------- сервис
async def test_create_set_criteria_and_publish(service):
    p = await service.create("Сумма", "Сложите два числа", AUTHOR)
    await service.set_criteria(p.id, [("Корректность", 7), ("Стиль", 3)])
    p = await service.publish(p.id)
    stored = await service.get(p.id)
    assert stored.status is ProblemStatus.PUBLISHED and stored.max_score == 10


async def test_get_unknown_problem_raises_not_found(service):
    with pytest.raises(NotFound):
        await service.get(uuid.uuid4())


async def test_list_filters_and_paginates(service):
    other = uuid.uuid4()
    for i in range(3):
        await service.create(f"Задача {i}", "условие", AUTHOR)
    p = await service.create("Чужая", "условие", other)
    await service.set_criteria(p.id, [("Корректность", 5)])
    await service.publish(p.id)

    items, total = await service.list(author_id=AUTHOR, offset=0, count=2)
    assert (len(items), total) == (2, 3)
    items, total = await service.list(status=ProblemStatus.PUBLISHED)
    assert total == 1 and items[0].author_id == other


async def test_stale_copy_cannot_be_saved(service):
    p = await service.create("Сумма", "условие", AUTHOR)
    first, second = await service.repo.get(p.id), await service.repo.get(p.id)
    first.set_criteria([("Корректность", 5)])
    await service.repo.save(first)
    second.set_criteria([("Стиль", 5)])
    with pytest.raises(ConcurrencyError):
        await service.repo.save(second)
