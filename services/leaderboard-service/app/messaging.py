"""Работа с RabbitMQ: публикация доменных событий и чтение входящей очереди.

Соглашения (ПР5, задание 2):
  * сервис-издатель публикует события в обменник <сервис>.events типа direct,
    ключ маршрутизации = имя события;
  * сервис-потребитель читает очередь <сервис>.inbox, привязанную к нужным ключам;
  * сообщение, обработка которого упала 3 раза, уходит через dead.letters
    в <сервис>.inbox.dlq;
  * сообщения и очереди сохраняются при перезапуске брокера.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

log = logging.getLogger("messaging")

DLX = "dead.letters"
MAX_ATTEMPTS = 3

Handler = Callable[[dict], Awaitable[None]]


def envelope(event_type: str, source: str, payload: dict) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "source": source,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _json_default(value):
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value)}")


def encode(data: dict) -> bytes:
    return json.dumps(data, default=_json_default, ensure_ascii=False).encode("utf-8")


class Broker:
    def __init__(self, url: str, service: str):
        self.url = url
        self.service = service
        self.exchange_name = f"{service}.events"
        self._connection = None
        self._channel = None
        self._exchanges: dict[str, aio_pika.abc.AbstractExchange] = {}

    async def connect(self, attempts: int = 30, delay: float = 2.0) -> None:
        for i in range(attempts):
            try:
                self._connection = await aio_pika.connect_robust(self.url)
                break
            except Exception as exc:  # брокер может стартовать дольше сервиса
                log.warning("RabbitMQ is not ready (%s), retry %s/%s", exc, i + 1, attempts)
                await asyncio.sleep(delay)
        else:
            raise RuntimeError("RabbitMQ is unavailable")
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=10)
        await self._exchange(DLX)
        await self._exchange(self.exchange_name)
        log.info("connected to RabbitMQ as %s", self.service)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def _exchange(self, name: str):
        if name not in self._exchanges:
            self._exchanges[name] = await self._channel.declare_exchange(
                name, aio_pika.ExchangeType.DIRECT, durable=True
            )
        return self._exchanges[name]

    async def publish(self, event_type: str, payload: dict) -> dict:
        """Публикует доменное событие в собственный обменник сервиса."""
        event = envelope(event_type, self.service, payload)
        exchange = await self._exchange(self.exchange_name)
        await exchange.publish(
            aio_pika.Message(
                body=encode(event),
                content_type="application/json",
                message_id=event["event_id"],
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=event_type,
        )
        log.info("published %s %s", event_type, event["event_id"])
        return event

    async def publish_to_queue(self, queue: str, data: dict, expiration_ms: int | None = None) -> None:
        """Сообщение в очередь через обменник по умолчанию (используется для таймеров)."""
        await self._channel.default_exchange.publish(
            aio_pika.Message(
                body=encode(data),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                expiration=expiration_ms / 1000 if expiration_ms is not None else None,
            ),
            routing_key=queue,
        )

    async def declare_queue(self, name: str, arguments: dict | None = None):
        return await self._channel.declare_queue(name, durable=True, arguments=arguments)

    async def consume(self, queue_name: str, bindings: list[tuple[str, str]], handler: Handler,
                      dead_letter: bool = True) -> None:
        """Объявляет очередь, привязывает её к обменникам и запускает обработку."""
        arguments = None
        if dead_letter:
            dlq = await self.declare_queue(f"{queue_name}.dlq")
            await dlq.bind(await self._exchange(DLX), routing_key=queue_name)
            arguments = {"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": queue_name}
        queue = await self.declare_queue(queue_name, arguments)
        for exchange, key in bindings:
            await queue.bind(await self._exchange(exchange), routing_key=key)

        async def on_message(message: AbstractIncomingMessage) -> None:
            attempt = int((message.headers or {}).get("x-attempt", 1))
            try:
                data = json.loads(message.body)
                await handler(data)
            except Exception:
                log.exception("failed to handle message from %s (attempt %s)", queue_name, attempt)
                if attempt < MAX_ATTEMPTS:
                    # повторная попытка: копия сообщения в конец своей очереди
                    await self._channel.default_exchange.publish(
                        aio_pika.Message(
                            body=message.body,
                            content_type=message.content_type,
                            message_id=message.message_id,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            headers={"x-attempt": attempt + 1},
                        ),
                        routing_key=queue_name,
                    )
                    await message.ack()
                else:
                    await message.nack(requeue=False)  # -> dead.letters -> <queue>.dlq
                return
            await message.ack()

        await queue.consume(on_message)
        log.info("consuming %s", queue_name)


class NullBroker:
    """Заглушка брокера для тестов и запуска без RabbitMQ: запоминает события."""

    def __init__(self, service: str = "test"):
        self.service = service
        self.published: list[dict] = []
        self.queued: list[tuple[str, dict, int | None]] = []

    async def connect(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish(self, event_type: str, payload: dict) -> dict:
        event = envelope(event_type, self.service, payload)
        self.published.append(event)
        return event

    async def publish_to_queue(self, queue: str, data: dict, expiration_ms: int | None = None) -> None:
        self.queued.append((queue, data, expiration_ms))

    async def declare_queue(self, *args, **kwargs):
        return None

    async def consume(self, *args, **kwargs) -> None:
        return None
