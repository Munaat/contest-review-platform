"""HTTP-контроллер review-service (5 эндпоинтов)."""
import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.domain import WorkStatus
from app.schemas import (AppealCreate, AppealOut, AppealResolve, DeclineRequest, GradeRequest,
                         WorkOut, WorkPage)
from app.service import ReviewService

router = APIRouter(prefix="/api", tags=["Review"])


def get_service(request: Request) -> ReviewService:
    return request.app.state.service


@router.get("/works", response_model=WorkPage, summary="Работы на проверку (очередь судьи)")
async def list_works(
    judge_id: uuid.UUID | None = Query(None, description="Работы с активным назначением судьи"),
    status_: WorkStatus | None = Query(None, alias="status", description="in_review, arbitration, approved"),
    contest_id: uuid.UUID | None = Query(None, description="Фильтр по соревнованию"),
    offset: int = Query(0, ge=0, description="Отступ от начала списка"),
    count: int = Query(20, ge=1, le=100, description="Размер страницы"),
    service: ReviewService = Depends(get_service),
):
    items, total = await service.list_works(judge_id, status_, contest_id, offset, count)
    return WorkPage(items=[WorkOut.of(w) for w in items], total=total, offset=offset, count=count)


@router.post("/works/{work_id}:grade", response_model=WorkOut,
             summary="Выставить оценку (судья) или утвердить итоговую (арбитр)")
async def grade_work(work_id: uuid.UUID, body: GradeRequest,
                     service: ReviewService = Depends(get_service)):
    scores = [(s.criterion_id, s.points) for s in body.scores]
    return WorkOut.of(await service.grade(work_id, body.reviewer_id, scores, body.comment))


@router.post("/works/{work_id}:decline", response_model=WorkOut, summary="Отказаться от проверки")
async def decline_work(work_id: uuid.UUID, body: DeclineRequest,
                       service: ReviewService = Depends(get_service)):
    return WorkOut.of(await service.decline(work_id, body.judge_id))


@router.post("/appeals", status_code=status.HTTP_201_CREATED, response_model=AppealOut,
             summary="Подать апелляцию")
async def file_appeal(body: AppealCreate, service: ReviewService = Depends(get_service)):
    return AppealOut.of(await service.file_appeal(body.work_id, body.participant_id, body.reason))


@router.post("/appeals/{appeal_id}:resolve", response_model=AppealOut, summary="Пересмотреть оценку")
async def resolve_appeal(appeal_id: uuid.UUID, body: AppealResolve,
                         service: ReviewService = Depends(get_service)):
    return AppealOut.of(await service.resolve_appeal(appeal_id, body.arbiter_id, body.new_score,
                                                     body.comment))
