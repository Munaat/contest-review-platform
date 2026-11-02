"""submission-service — приём решений (минимальная реализация).

Перед приёмом решения сервис синхронно проверяет в contest-service, что
соревнование идёт, участник зарегистрирован и задача входит в соревнование
(ПР4: submission → contest). Принятое решение публикуется событием
solution.accepted; код решения в сообщение не кладётся, передаётся ссылка.
"""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Settings(BaseSettings):
    amqp_url: str = ""
    contest_url: str = "http://localhost:8004"


settings = Settings()

LANGUAGES = {"python3", "cpp17", "java17", "go", "text"}


class SubmissionCreate(BaseModel):
    contest_id: uuid.UUID
    problem_id: uuid.UUID
    participant_id: uuid.UUID
    language: str = Field(description="python3, cpp17, java17, go или text")
    source: str = Field(min_length=1, max_length=100_000, description="Код или текст решения")


class SubmissionOut(BaseModel):
    id: uuid.UUID
    contest_id: uuid.UUID
    problem_id: uuid.UUID
    participant_id: uuid.UUID
    language: str
    submitted_at: datetime
    source_url: str


submissions: dict[uuid.UUID, tuple[SubmissionOut, str]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.broker = Broker(settings.amqp_url, "submission") if settings.amqp_url else NullBroker("submission")
    await app.state.broker.connect()
    yield
    await app.state.broker.close()


app = FastAPI(title="Submission Service", version="1.0.0", lifespan=lifespan,
              description="Приём решений участников")


async def fetch_contest(contest_id: uuid.UUID) -> dict:
    try:
        async with httpx.AsyncClient(base_url=settings.contest_url, timeout=5) as c:
            r = await c.get(f"/api/contests/{contest_id}")
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"contest-service is unavailable: {exc}")
    if r.status_code == 404:
        raise HTTPException(404, f"contest {contest_id} not found")
    r.raise_for_status()
    return r.json()


@app.post("/api/submissions", status_code=201, response_model=SubmissionOut, summary="Отправить решение")
async def submit(body: SubmissionCreate, request: Request):
    if body.language not in LANGUAGES:
        raise HTTPException(422, f"language must be one of {sorted(LANGUAGES)}")
    contest = await fetch_contest(body.contest_id)          # синхронный вызов contest-service
    # отказ в приёме = событие «Отправка отклонена»
    if contest["status"] != "started":
        raise HTTPException(409, "submission rejected: contest is not running")
    if str(body.participant_id) not in contest["participant_ids"]:
        raise HTTPException(409, "submission rejected: participant is not registered")
    if str(body.problem_id) not in contest["problem_ids"]:
        raise HTTPException(409, "submission rejected: problem is not in the contest")
    sid = uuid.uuid4()
    out = SubmissionOut(id=sid, contest_id=body.contest_id, problem_id=body.problem_id,
                        participant_id=body.participant_id, language=body.language,
                        submitted_at=datetime.now(timezone.utc),
                        source_url=f"/api/submissions/{sid}/source")
    submissions[sid] = (out, body.source)
    payload = out.model_dump(exclude={"id"})
    payload["submission_id"] = sid
    await request.app.state.broker.publish("solution.accepted", payload)
    return out


@app.get("/api/submissions/{submission_id}", response_model=SubmissionOut, summary="Карточка решения")
async def read_submission(submission_id: uuid.UUID):
    if submission_id not in submissions:
        raise HTTPException(404, f"submission {submission_id} not found")
    return submissions[submission_id][0]


@app.get("/api/submissions/{submission_id}/source", response_class=PlainTextResponse,
         summary="Код решения (для судьи)")
async def read_source(submission_id: uuid.UUID):
    if submission_id not in submissions:
        raise HTTPException(404, f"submission {submission_id} not found")
    return submissions[submission_id][1]
