"""Точка входа problem-service."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.db import create_schema, make_engine, make_sessions
from app.domain import DomainError, InvalidInput, NotFound
from app.repositories import ConcurrencyError, InMemoryProblemRepository, SqlProblemRepository
from app.service import ProblemService
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(service: ProblemService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if service is not None:
            app.state.service = service
        elif settings.postgres_url:
            engine = make_engine(settings.postgres_url)
            await create_schema(engine)
            app.state.service = ProblemService(SqlProblemRepository(make_sessions(engine)))
        else:
            app.state.service = ProblemService(InMemoryProblemRepository())
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Problem Service", version="1.0.0",
                  description="Задачи соревнований и критерии оценивания", lifespan=lifespan)
    app.include_router(router)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidInput)
    async def invalid(_: Request, exc: InvalidInput):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def conflict(_: Request, exc: DomainError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ConcurrencyError)
    async def concurrency(_: Request, exc: ConcurrencyError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    return app


app = create_app()
