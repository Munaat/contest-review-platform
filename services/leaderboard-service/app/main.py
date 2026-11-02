"""leaderboard-service — таблицы результатов соревнований (минимальная реализация).

Строит таблицу по событиям: contest.started создаёт таблицу, grade.approved и
grade.revised учитывают баллы, contest.submissions_closed и contest.finished
меняют её статус. Состав участников при старте берётся синхронно из
contest-service (ПР4: leaderboard → contest).
"""
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leaderboard")


class Settings(BaseSettings):
    amqp_url: str = ""
    contest_url: str = "http://localhost:8004"


settings = Settings()


class Table:
    def __init__(self, contest_id: str, title: str, participants: list[str]) -> None:
        self.contest_id = contest_id
        self.title = title
        self.status = "running"            # running -> preliminary -> final -> published
        self.scores: dict[str, dict[str, int]] = {p: {} for p in participants}


tables: dict[str, Table] = {}


class Row(BaseModel):
    place: int
    participant_id: uuid.UUID
    total: int
    problems: dict[uuid.UUID, int]


class Standings(BaseModel):
    contest_id: uuid.UUID
    title: str
    status: str
    rows: list[Row]


def standings(t: Table) -> Standings:
    ordered = sorted(t.scores.items(), key=lambda kv: -sum(kv[1].values()))
    rows, place, prev = [], 0, None
    for i, (pid, problems) in enumerate(ordered, start=1):
        total = sum(problems.values())
        if total != prev:
            place, prev = i, total          # равные суммы делят место
        rows.append(Row(place=place, participant_id=pid, total=total, problems=problems))
    return Standings(contest_id=t.contest_id, title=t.title, status=t.status, rows=rows)


async def fetch_participants(contest_id: str, fallback: list[str]) -> list[str]:
    try:
        async with httpx.AsyncClient(base_url=settings.contest_url, timeout=5) as c:
            r = await c.get(f"/api/contests/{contest_id}")
            r.raise_for_status()
            return r.json()["participant_ids"]
    except httpx.HTTPError as exc:
        log.warning("contest-service unavailable (%s), using participants from event", exc)
        return fallback


async def handle(event: dict) -> None:
    kind, p = event["event_type"], event["payload"]
    cid = p["contest_id"]
    if kind == "contest.started":
        participants = await fetch_participants(cid, p.get("participant_ids", []))
        tables.setdefault(cid, Table(cid, p.get("title", ""), participants))
        return
    t = tables.get(cid)
    if t is None:
        t = tables[cid] = Table(cid, p.get("title", ""), p.get("participant_ids", []))
    if kind == "contest.submissions_closed":
        t.status = "preliminary"
    elif kind == "contest.finished":
        t.status = "final"
    elif kind == "grade.approved":
        row = t.scores.setdefault(p["participant_id"], {})
        row[p["problem_id"]] = max(row.get(p["problem_id"], 0), p["score"])   # лучший балл по задаче
    elif kind == "grade.revised":
        t.scores.setdefault(p["participant_id"], {})[p["problem_id"]] = p["new_score"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = Broker(settings.amqp_url, "leaderboard") if settings.amqp_url else NullBroker("leaderboard")
    await broker.connect()
    await broker.consume("leaderboard.inbox", [
        ("contest.events", "contest.started"), ("contest.events", "contest.submissions_closed"),
        ("contest.events", "contest.finished"), ("review.events", "grade.approved"),
        ("review.events", "grade.revised")], handle)
    yield
    await broker.close()


app = FastAPI(title="Leaderboard Service", version="1.0.0", lifespan=lifespan,
              description="Таблицы результатов соревнований")


@app.get("/api/leaderboards/{contest_id}", response_model=Standings, summary="Таблица результатов")
async def read_table(contest_id: uuid.UUID):
    t = tables.get(str(contest_id))
    if t is None:
        raise HTTPException(404, f"leaderboard for {contest_id} not found")
    return standings(t)


@app.post("/api/leaderboards/{contest_id}:publish", response_model=Standings,
          summary="Опубликовать итоговую таблицу")
async def publish_table(contest_id: uuid.UUID):
    t = tables.get(str(contest_id))
    if t is None:
        raise HTTPException(404, f"leaderboard for {contest_id} not found")
    if t.status != "final":
        raise HTTPException(409, "contest is not finished yet")
    t.status = "published"
    return standings(t)
