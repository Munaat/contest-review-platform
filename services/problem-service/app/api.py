"""HTTP-контроллер problem-service (5 эндпоинтов)."""
import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.domain import ProblemStatus
from app.schemas import CriteriaSet, ProblemCreate, ProblemOut, ProblemPage
from app.service import ProblemService

router = APIRouter(prefix="/api/problems", tags=["Problems"])


def get_service(request: Request) -> ProblemService:
    return request.app.state.service


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProblemOut,
             summary="Создать задачу (черновик)")
async def create_problem(body: ProblemCreate, service: ProblemService = Depends(get_service)):
    return ProblemOut.of(await service.create(body.title, body.statement, body.author_id))


@router.put("/{problem_id}/criteria", response_model=ProblemOut,
            summary="Задать критерии оценивания")
async def set_criteria(problem_id: uuid.UUID, body: CriteriaSet,
                       service: ProblemService = Depends(get_service)):
    items = [(c.name, c.max_points) for c in body.criteria]
    return ProblemOut.of(await service.set_criteria(problem_id, items))


@router.post("/{problem_id}:publish", response_model=ProblemOut, summary="Опубликовать задачу")
async def publish_problem(problem_id: uuid.UUID, service: ProblemService = Depends(get_service)):
    return ProblemOut.of(await service.publish(problem_id))


@router.get("/{problem_id}", response_model=ProblemOut, summary="Задача с критериями")
async def get_problem(problem_id: uuid.UUID, service: ProblemService = Depends(get_service)):
    return ProblemOut.of(await service.get(problem_id))


@router.get("", response_model=ProblemPage, summary="Список задач")
async def list_problems(
    status_: ProblemStatus | None = Query(None, alias="status", description="draft или published"),
    author_id: uuid.UUID | None = Query(None, description="Фильтр по автору"),
    offset: int = Query(0, ge=0, description="Отступ от начала списка"),
    count: int = Query(20, ge=1, le=100, description="Размер страницы"),
    service: ProblemService = Depends(get_service),
):
    items, total = await service.list(status_, author_id, offset, count)
    return ProblemPage(items=[ProblemOut.of(p) for p in items], total=total, offset=offset, count=count)
