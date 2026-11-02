"""Точка входа review-service: HTTP API + обработчики очередей в одном процессе."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.clients import ContestClient, DependencyUnavailable, ProblemClient
from app.consumers import start_consumers
from app.db import create_schema, make_engine, make_sessions
from app.domain import DomainError, Forbidden, InvalidInput, NotFound
from app.messaging import Broker, NullBroker
from app.repositories import (ConcurrencyError, DuplicateError, InMemoryAppealRepository,
                              InMemoryWorkRepository, SqlAppealRepository, SqlWorkRepository)
from app.service import ReviewService
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_service(broker) -> tuple[ReviewService, object]:
    engine = None
    if settings.postgres_url:
        engine = make_engine(settings.postgres_url)
        sessions = make_sessions(engine)
        works, appeals = SqlWorkRepository(sessions), SqlAppealRepository(sessions)
    else:
        works, appeals = InMemoryWorkRepository(), InMemoryAppealRepository()
    service = ReviewService(works, appeals, ProblemClient(settings.problem_url),
                            ContestClient(settings.contest_url), broker,
                            settings.review_deadline_seconds)
    return service, engine


def create_app(service: ReviewService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, broker = None, None
        if service is not None:
            app.state.service = service
        else:
            broker = Broker(settings.amqp_url, "review") if settings.amqp_url else NullBroker("review")
            app.state.service, engine = build_service(broker)
            if engine is not None:
                await create_schema(engine)
            await broker.connect()
            await start_consumers(broker, app.state.service)
        yield
        if broker is not None:
            await broker.close()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Review Service", version="1.0.0",
                  description="Экспертная проверка решений: назначения, оценки, арбитраж, апелляции",
                  lifespan=lifespan)
    app.include_router(router)

    def handler(code: int):
        async def handle(_: Request, exc: Exception):
            return JSONResponse(status_code=code, content={"detail": str(exc)})
        return handle

    # Starlette выбирает обработчик по MRO исключения: подкласс важнее базового класса
    app.add_exception_handler(NotFound, handler(404))
    app.add_exception_handler(Forbidden, handler(403))
    app.add_exception_handler(InvalidInput, handler(422))
    app.add_exception_handler(DomainError, handler(409))
    app.add_exception_handler(ConcurrencyError, handler(409))
    app.add_exception_handler(DuplicateError, handler(409))
    app.add_exception_handler(DependencyUnavailable, handler(503))
    return app


app = create_app()
