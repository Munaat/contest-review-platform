"""Асинхронный интерфейс review-service: обработчики сообщений RabbitMQ."""
from app.service import DEADLINE_WAIT_QUEUE, ReviewService

INBOX = "review.inbox"
TIMERS_EXCHANGE = "review.timers"
DEADLINE_QUEUE = "review.deadline"


async def start_consumers(broker, service: ReviewService) -> None:
    # событие «Решение принято» от submission-service
    await broker.consume(INBOX, [("submission.events", "solution.accepted")],
                         service.on_solution_accepted)
    # таймер дедлайна: сообщение лежит в review.deadline.wait до истечения TTL,
    # затем через review.timers попадает в review.deadline
    await broker.consume(DEADLINE_QUEUE, [(TIMERS_EXCHANGE, "deadline.expired")],
                         service.on_deadline, dead_letter=False)
    await broker.declare_queue(DEADLINE_WAIT_QUEUE, {
        "x-dead-letter-exchange": TIMERS_EXCHANGE,
        "x-dead-letter-routing-key": "deadline.expired",
    })
