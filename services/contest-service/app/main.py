"""contest-service — соревнования, жюри, регистрация участников (минимальная реализация).

Данные хранятся в памяти процесса. Сервис публикует события жизненного цикла
соревнования: contest.started, contest.submissions_closed, contest.finished.
"""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Settings(BaseSettings):
    amqp_url: str = ""


settings = Settings()


class Status(str, Enum):
    CREATED = "created"
    STARTED = "started"
    CLOSED = "closed"
    FINISHED = "finished"


# допустимые переходы и событие, которое публикуется при переходе
TRANSITIONS = {
    (Status.CREATED, Status.STARTED): "contest.started",
    (Status.STARTED, Status.CLOSED): "contest.submissions_closed",
    (Status.CLOSED, Status.FINISHED): "contest.finished",
}


class ContestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    problem_ids: list[uuid.UUID] = Field(min_length=1, description="Задачи из problem-service")
    judge_ids: list[uuid.UUID] = Field(min_length=2, description="Жюри: не меньше двух судей")
    arbiter_id: uuid.UUID = Field(description="Арбитр соревнования")
    starts_at: datetime
    submissions_close_at: datetime


class Registration(BaseModel):
    user_id: uuid.UUID


class StatusChange(BaseModel):
    status: Status


class ContestOut(ContestCreate):
    id: uuid.UUID
    status: Status
    participant_ids: list[uuid.UUID]
    created_at: datetime


contests: dict[uuid.UUID, ContestOut] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.broker = Broker(settings.amqp_url, "contest") if settings.amqp_url else NullBroker("contest")
    await app.state.broker.connect()
    yield
    await app.state.broker.close()


app = FastAPI(title="Contest Service", version="1.0.0", lifespan=lifespan,
              description="Соревнования, жюри и регистрация участников")


def get_contest(contest_id: uuid.UUID) -> ContestOut:
    if contest_id not in contests:
        raise HTTPException(404, f"contest {contest_id} not found")
    return contests[contest_id]


@app.post("/api/contests", status_code=201, response_model=ContestOut, summary="Создать соревнование")
async def create_contest(body: ContestCreate):
    if body.arbiter_id in body.judge_ids:
        raise HTTPException(422, "arbiter must not be a regular judge")
    if body.submissions_close_at <= body.starts_at:
        raise HTTPException(422, "submissions_close_at must be later than starts_at")
    c = ContestOut(**body.model_dump(), id=uuid.uuid4(), status=Status.CREATED, participant_ids=[],
                   created_at=datetime.now(timezone.utc))
    contests[c.id] = c
    return c


@app.get("/api/contests/{contest_id}", response_model=ContestOut, summary="Соревнование, жюри и участники")
async def read_contest(contest_id: uuid.UUID):
    return get_contest(contest_id)


@app.post("/api/contests/{contest_id}/participants", status_code=201, response_model=ContestOut,
          summary="Зарегистрироваться на соревнование")
async def register(contest_id: uuid.UUID, body: Registration):
    c = get_contest(contest_id)
    if c.status not in (Status.CREATED, Status.STARTED):
        raise HTTPException(409, "registration is closed")
    if body.user_id in c.participant_ids:
        raise HTTPException(409, "user is already registered")
    if body.user_id in c.judge_ids or body.user_id == c.arbiter_id:
        raise HTTPException(409, "jury member cannot participate")
    c.participant_ids.append(body.user_id)
    return c


@app.patch("/api/contests/{contest_id}", response_model=ContestOut,
           summary="Начать соревнование, закрыть приём решений, завершить")
async def change_status(contest_id: uuid.UUID, body: StatusChange, request: Request):
    c = get_contest(contest_id)
    event = TRANSITIONS.get((c.status, body.status))
    if event is None:
        raise HTTPException(409, f"cannot change status from {c.status.value} to {body.status.value}")
    c.status = body.status
    now = datetime.now(timezone.utc)
    time_field = {"contest.started": "started_at", "contest.submissions_closed": "closed_at",
                  "contest.finished": "finished_at"}[event]
    await request.app.state.broker.publish(event, {
        "contest_id": c.id, "title": c.title, time_field: now,
        "submissions_close_at": c.submissions_close_at, "participant_ids": c.participant_ids,
    })
    return c
