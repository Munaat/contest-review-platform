"""Синхронные клиенты других сервисов (HTTP/REST).

  * problem-service — критерии оценивания и максимальный балл задачи (ПР4: review → problem);
  * contest-service — состав жюри и арбитр соревнования.
"""
import uuid
from dataclasses import dataclass

import httpx

from app.domain import NotFound


class DependencyUnavailable(Exception):
    """Сервис-зависимость не ответил (HTTP 503)."""


@dataclass
class ProblemInfo:
    id: uuid.UUID
    max_score: int
    criteria: dict[uuid.UUID, int]  # id критерия -> максимальный балл


@dataclass
class JuryInfo:
    judge_ids: list[uuid.UUID]
    arbiter_id: uuid.UUID


class _HttpClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as c:
                r = await c.get(path)
        except httpx.HTTPError as exc:
            raise DependencyUnavailable(f"{self.base_url} is unavailable: {exc}") from exc
        if r.status_code == 404:
            raise NotFound(f"{path} not found")
        if r.status_code >= 400:
            raise DependencyUnavailable(f"{self.base_url}{path} answered {r.status_code}")
        return r.json()


class ProblemClient(_HttpClient):
    async def get_problem(self, problem_id: uuid.UUID) -> ProblemInfo:
        data = await self._get(f"/api/problems/{problem_id}")
        return ProblemInfo(id=uuid.UUID(data["id"]), max_score=data["max_score"],
                           criteria={uuid.UUID(c["id"]): c["max_points"] for c in data["criteria"]})


class ContestClient(_HttpClient):
    async def get_jury(self, contest_id: uuid.UUID) -> JuryInfo:
        data = await self._get(f"/api/contests/{contest_id}")
        return JuryInfo(judge_ids=[uuid.UUID(j) for j in data["judge_ids"]],
                        arbiter_id=uuid.UUID(data["arbiter_id"]))
