"""Модульные тесты сервисного слоя: in-memory репозитории, заглушки клиентов
problem-service и contest-service, брокер-заглушка вместо RabbitMQ."""
import uuid

import pytest

from app.clients import DependencyUnavailable, JuryInfo, ProblemInfo
from app.domain import InvalidInput, Role, WorkStatus
from app.messaging import NullBroker
from app.repositories import InMemoryAppealRepository, InMemoryWorkRepository
from app.service import DEADLINE_WAIT_QUEUE, ReviewService

C1, C2 = uuid.uuid4(), uuid.uuid4()


class FakeProblems:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def get_problem(self, problem_id):
        self.calls += 1
        if self.fail:
            raise DependencyUnavailable("problem-service is down")
        return ProblemInfo(id=problem_id, max_score=10, criteria={C1: 6, C2: 4})


class FakeContests:
    def __init__(self, judges, arbiter):
        self.jury = JuryInfo(judges, arbiter)

    async def get_jury(self, contest_id):
        return self.jury


@pytest.fixture()
def env():
    judges, arbiter = [uuid.uuid4() for _ in range(3)], uuid.uuid4()
    broker = NullBroker("review")
    problems = FakeProblems()
    service = ReviewService(InMemoryWorkRepository(), InMemoryAppealRepository(), problems,
                            FakeContests(judges, arbiter), broker, deadline_seconds=60)
    return service, broker, problems, judges, arbiter


def accepted_event(participant=None):
    return {"event_id": str(uuid.uuid4()), "event_type": "solution.accepted", "payload": {
        "submission_id": str(uuid.uuid4()), "contest_id": str(uuid.uuid4()),
        "problem_id": str(uuid.uuid4()), "participant_id": str(participant or uuid.uuid4())}}


def scores(a, b):
    return [(C1, a), (C2, b)]


def judges_of(work):
    return [x.reviewer_id for x in work.assignments if x.status.value == "active"]


async def test_solution_accepted_creates_work_and_schedules_timers(env):
    service, broker, *_ = env
    work = await service.on_solution_accepted(accepted_event())
    assert work.status is WorkStatus.IN_REVIEW
    assert [e["event_type"] for e in broker.published] == ["review.assigned"] * 2
    assert [(q, ms) for q, _, ms in broker.queued] == [(DEADLINE_WAIT_QUEUE, 60_000)] * 2


async def test_duplicate_solution_event_is_ignored(env):
    service, broker, *_ = env
    event = accepted_event()
    await service.on_solution_accepted(event)
    assert await service.on_solution_accepted(event) is None
    assert len(broker.published) == 2


async def test_grade_calls_problem_service_and_sums_criteria(env):
    service, _, problems, *_ = env
    work = await service.on_solution_accepted(accepted_event())
    j1 = judges_of(work)[0]
    work = await service.grade(work.id, j1, scores(5, 3), "ok")
    assert problems.calls == 1
    assert next(a for a in work.assignments if a.reviewer_id == j1).score == 8


@pytest.mark.parametrize("given", [
    [(C1, 5)],                              # не все критерии
    [(C1, 5), (C2, 3), (uuid.uuid4(), 1)],  # лишний критерий
    [(C1, 7), (C2, 3)],                     # больше максимума критерия
    [(C1, 5), (C1, 1)],                     # критерий оценён дважды
])
async def test_grade_with_invalid_scores_is_rejected(env, given):
    service, *_ = env
    work = await service.on_solution_accepted(accepted_event())
    with pytest.raises(InvalidInput):
        await service.grade(work.id, judges_of(work)[0], given, None)


async def test_two_close_grades_publish_grade_approved(env):
    service, broker, *_ = env
    participant = uuid.uuid4()
    work = await service.on_solution_accepted(accepted_event(participant))
    j1, j2 = judges_of(work)
    await service.grade(work.id, j1, scores(6, 4), "отлично")
    work = await service.grade(work.id, j2, scores(5, 4), "хорошо")
    approved = broker.published[-1]
    assert approved["event_type"] == "grade.approved"
    assert approved["payload"]["score"] == 10 and approved["payload"]["max_score"] == 10
    assert approved["payload"]["participant_id"] == participant
    assert approved["payload"]["approved_by"] == Role.JUDGE.value


async def test_resolved_appeal_publishes_grade_revised(env):
    service, broker, _, _, arbiter = env
    participant = uuid.uuid4()
    work = await service.on_solution_accepted(accepted_event(participant))
    j1, j2 = judges_of(work)
    await service.grade(work.id, j1, scores(4, 2), None)
    await service.grade(work.id, j2, scores(4, 2), None)
    appeal = await service.file_appeal(work.id, participant, "учтите стиль")
    await service.resolve_appeal(appeal.id, arbiter, 8, "учтено")
    revised = broker.published[-1]
    assert revised["event_type"] == "grade.revised"
    assert (revised["payload"]["old_score"], revised["payload"]["new_score"]) == (6, 8)


async def test_expired_deadline_reassigns_and_schedules_new_timer(env):
    service, broker, *_ = env
    service.deadline = service.deadline * 0      # срок уже истёк
    work = await service.on_solution_accepted(accepted_event())
    first = work.assignments[0]
    await service.on_deadline({"work_id": str(work.id), "assignment_id": str(first.id)})
    work = await service.works.get(work.id)
    slot = [a for a in work.assignments if a.slot == first.slot]
    assert [a.status.value for a in slot] == ["expired", "active"]
    assert len(broker.queued) == 3                # два исходных таймера + новый


async def test_problem_service_unavailable_is_reported(env):
    service, _, problems, *_ = env
    work = await service.on_solution_accepted(accepted_event())
    problems.fail = True
    with pytest.raises(DependencyUnavailable):
        await service.grade(work.id, judges_of(work)[0], scores(1, 1), None)
