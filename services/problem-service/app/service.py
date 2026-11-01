"""Сервисный слой контекста задач: команды «Создать задачу», «Задать критерии
оценивания», «Опубликовать задачу» и запросы чтения."""
import uuid

from app.domain import Problem, ProblemStatus
from app.repositories import ProblemRepository


class ProblemService:
    def __init__(self, repo: ProblemRepository) -> None:
        self.repo = repo

    async def create(self, title: str, statement: str, author_id: uuid.UUID) -> Problem:
        problem = Problem.create(title, statement, author_id)
        await self.repo.add(problem)
        return problem

    async def set_criteria(self, problem_id: uuid.UUID, items: list[tuple[str, int]]) -> Problem:
        problem = await self.repo.get(problem_id)
        problem.set_criteria(items)
        await self.repo.save(problem)
        return problem

    async def publish(self, problem_id: uuid.UUID) -> Problem:
        problem = await self.repo.get(problem_id)
        problem.publish()
        await self.repo.save(problem)
        return problem

    async def get(self, problem_id: uuid.UUID) -> Problem:
        return await self.repo.get(problem_id)

    async def list(self, status: ProblemStatus | None = None, author_id: uuid.UUID | None = None,
                   offset: int = 0, count: int = 20) -> tuple[list[Problem], int]:
        return await self.repo.list(status, author_id, offset, count)
