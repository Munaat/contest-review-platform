"""user-service — профили и рейтинг пользователей (минимальная реализация).

  * user.registered -> создать профиль с начальным рейтингом 1500;
  * contest.finished -> пересчитать рейтинг по итоговым местам, которые
    запрашиваются синхронно у leaderboard-service;
  * карточка пользователя дополняется данными учётной записи из auth-service
    (ПР4: user → auth).
"""
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("user")

INITIAL_RATING = 1500
K = 40


class Settings(BaseSettings):
    amqp_url: str = ""
    auth_url: str = "http://localhost:8001"
    leaderboard_url: str = "http://localhost:8007"


settings = Settings()


class Profile(BaseModel):
    id: uuid.UUID
    login: str
    display_name: str
    rating: int = INITIAL_RATING
    contests: int = 0


class ProfileOut(Profile):
    email: str | None = None


profiles: dict[str, Profile] = {}


async def handle(event: dict) -> None:
    kind, p = event["event_type"], event["payload"]
    if kind == "user.registered":
        profiles.setdefault(p["user_id"], Profile(id=p["user_id"], login=p["login"],
                                                  display_name=p["display_name"]))
    elif kind == "contest.finished":
        async with httpx.AsyncClient(base_url=settings.leaderboard_url, timeout=5) as c:
            r = await c.get(f"/api/leaderboards/{p['contest_id']}")
            r.raise_for_status()                   # ошибка -> повтор сообщения брокером
        rows = r.json()["rows"]
        n = len(rows)
        for row in rows:
            prof = profiles.get(row["participant_id"])
            if prof is None:
                continue
            # чем выше место, тем больше прибавка; середина таблицы ~ 0
            prof.rating += round(K * (n - 2 * row["place"] + 1) / max(n - 1, 1)) if n > 1 else 0
            prof.contests += 1
        log.info("ratings recalculated for contest %s", p["contest_id"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = Broker(settings.amqp_url, "user") if settings.amqp_url else NullBroker("user")
    await broker.connect()
    await broker.consume("user.inbox", [("auth.events", "user.registered"),
                                        ("contest.events", "contest.finished")], handle)
    yield
    await broker.close()


app = FastAPI(title="User Service", version="1.0.0", lifespan=lifespan,
              description="Профили и рейтинг пользователей")


@app.get("/api/users/{user_id}", response_model=ProfileOut, summary="Профиль пользователя")
async def read_user(user_id: uuid.UUID):
    prof = profiles.get(str(user_id))
    if prof is None:
        raise HTTPException(404, f"user {user_id} not found")
    out = ProfileOut(**prof.model_dump())
    try:                                                      # синхронный вызов auth-service
        async with httpx.AsyncClient(base_url=settings.auth_url, timeout=3) as c:
            r = await c.get(f"/api/accounts/{user_id}")
        if r.status_code == 200:
            out.email = r.json()["email"]
    except httpx.HTTPError as exc:
        log.warning("auth-service unavailable: %s", exc)
    return out


@app.get("/api/users", response_model=list[Profile], summary="Рейтинг пользователей")
async def list_users(offset: int = Query(0, ge=0), count: int = Query(20, ge=1, le=100)):
    ordered = sorted(profiles.values(), key=lambda p: -p.rating)
    return ordered[offset:offset + count]
