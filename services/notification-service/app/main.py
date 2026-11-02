"""notification-service — уведомления участников и судей (минимальная реализация).

По ПР3–ПР4 доставку выполняют внешние системы (SMTP-шлюз, Telegram Bot API).
В минимальной реализации отправка имитируется записью в журнал, а
уведомления сохраняются в памяти и доступны по HTTP.
"""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notification")


class Settings(BaseSettings):
    amqp_url: str = ""


settings = Settings()


class Notification(BaseModel):
    id: uuid.UUID
    recipient_id: uuid.UUID
    event_type: str
    text: str
    channel: str = "email"
    created_at: datetime


notifications: dict[uuid.UUID, Notification] = {}


def compose(kind: str, p: dict) -> list[tuple[str, str]]:
    """Получатели и текст уведомления для события."""
    if kind == "contest.started":
        return [(u, f"Соревнование «{p['title']}» началось") for u in p["participant_ids"]]
    if kind == "contest.submissions_closed":
        return [(u, f"Приём решений в «{p['title']}» закрыт") for u in p["participant_ids"]]
    if kind == "contest.finished":
        return [(u, f"Соревнование «{p['title']}» завершено") for u in p["participant_ids"]]
    if kind == "review.assigned":
        what = "арбитраж работы" if p.get("role") == "arbiter" else "работа на проверку"
        tail = f", срок до {p['deadline_at']}" if p.get("deadline_at") else ""
        return [(p["judge_id"], f"Вам назначена {what} {p['work_id']}{tail}")]
    if kind == "grade.approved":
        return [(p["participant_id"], f"Решение оценено: {p['score']} из {p['max_score']}")]
    if kind == "grade.revised":
        return [(p["participant_id"],
                 f"Апелляция рассмотрена: оценка {p['old_score']} → {p['new_score']} из {p['max_score']}")]
    return []


async def handle(event: dict) -> None:
    for recipient, text in compose(event["event_type"], event["payload"]):
        n = Notification(id=uuid.uuid4(), recipient_id=recipient, event_type=event["event_type"],
                         text=text, created_at=datetime.now(timezone.utc))
        notifications[n.id] = n
        log.info("SEND %s -> %s: %s", n.channel, recipient, text)   # имитация SMTP/Telegram


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = Broker(settings.amqp_url, "notification") if settings.amqp_url else NullBroker("notification")
    await broker.connect()
    await broker.consume("notification.inbox", [
        ("contest.events", "contest.started"), ("contest.events", "contest.submissions_closed"),
        ("contest.events", "contest.finished"), ("review.events", "review.assigned"),
        ("review.events", "grade.approved"), ("review.events", "grade.revised")], handle)
    yield
    await broker.close()


app = FastAPI(title="Notification Service", version="1.0.0", lifespan=lifespan,
              description="Уведомления участников и судей")


@app.get("/api/notifications", response_model=list[Notification], summary="Уведомления получателя")
async def list_notifications(recipient_id: uuid.UUID | None = Query(None),
                             offset: int = Query(0, ge=0), count: int = Query(50, ge=1, le=200)):
    items = [n for n in notifications.values() if recipient_id is None or n.recipient_id == recipient_id]
    items.sort(key=lambda n: n.created_at)
    return items[offset:offset + count]


@app.get("/api/notifications/{notification_id}", response_model=Notification, summary="Уведомление")
async def read_notification(notification_id: uuid.UUID):
    if notification_id not in notifications:
        raise HTTPException(404, f"notification {notification_id} not found")
    return notifications[notification_id]
