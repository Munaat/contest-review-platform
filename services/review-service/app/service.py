"""Сервисный слой контекста проверки.

Связывает агрегаты с репозиториями, клиентами других сервисов и брокером:
загружает агрегат, вызывает его команду, сохраняет и только после фиксации
публикует события (review.assigned, grade.approved, grade.revised) и ставит
таймеры дедлайна проверки.
"""
import logging
import uuid
from datetime import timedelta

from app.clients import ContestClient, ProblemClient
from app.domain import (Appeal, Approved, Assigned, DiscrepancyFound, InvalidInput, Role, Work,
                        WorkStatus, utcnow)
from app.repositories import AppealRepository, DuplicateError, WorkRepository

log = logging.getLogger("review")

DEADLINE_WAIT_QUEUE = "review.deadline.wait"


class ReviewService:
    def __init__(self, works: WorkRepository, appeals: AppealRepository, problems: ProblemClient,
                 contests: ContestClient, broker, deadline_seconds: int = 3600) -> None:
        self.works = works
        self.appeals = appeals
        self.problems = problems
        self.contests = contests
        self.broker = broker
        self.deadline = timedelta(seconds=deadline_seconds)

    # ------------------------------------------------------------ асинхронные команды
    async def on_solution_accepted(self, event: dict) -> Work | None:
        """Решение принято -> поставить работу в очередь и назначить двум судьям."""
        p = event["payload"]
        submission_id = uuid.UUID(p["submission_id"])
        if await self.works.find_by_submission(submission_id) is not None:
            log.info("submission %s already in review, skip duplicate", submission_id)
            return None
        contest_id = uuid.UUID(p["contest_id"])
        jury = await self.contests.get_jury(contest_id)
        load = await self.works.active_load(jury.judge_ids)
        work, events = Work.start(
            submission_id=submission_id, contest_id=contest_id,
            problem_id=uuid.UUID(p["problem_id"]), participant_id=uuid.UUID(p["participant_id"]),
            jury=jury.judge_ids, arbiter_id=jury.arbiter_id,
            now=utcnow(), deadline=self.deadline, load=load)
        try:
            await self.works.add(work)
        except DuplicateError:
            return None
        await self._dispatch(work, events)
        return work

    async def on_deadline(self, timer: dict) -> None:
        """Таймер истёк -> переназначить работу или передать арбитру."""
        work = await self.works.get(uuid.UUID(timer["work_id"]))
        load = await self.works.active_load(work.jury)
        changed, events = work.expire(uuid.UUID(timer["assignment_id"]), utcnow(), self.deadline, load)
        if not changed:
            return
        await self.works.save(work)
        log.info("assignment %s of work %s expired", timer["assignment_id"], work.id)
        await self._dispatch(work, events)

    # ------------------------------------------------------------ синхронные команды
    async def grade(self, work_id: uuid.UUID, reviewer_id: uuid.UUID,
                    scores: list[tuple[uuid.UUID, int]], comment: str | None) -> Work:
        work = await self.works.get(work_id)
        problem = await self.problems.get_problem(work.problem_id)  # синхронный вызов problem-service
        given = dict(scores)
        if len(given) != len(scores):
            raise InvalidInput("each criterion must be scored once")
        if set(given) != set(problem.criteria):
            raise InvalidInput("scores must be given for every criterion of the problem")
        for criterion_id, points in given.items():
            limit = problem.criteria[criterion_id]
            if not 0 <= points <= limit:
                raise InvalidInput(f"points for criterion {criterion_id} must be from 0 to {limit}")
        events = work.grade(reviewer_id, sum(given.values()), problem.max_score, comment, utcnow())
        await self.works.save(work)
        await self._dispatch(work, events)
        return work

    async def decline(self, work_id: uuid.UUID, judge_id: uuid.UUID) -> Work:
        work = await self.works.get(work_id)
        load = await self.works.active_load(work.jury)
        events = work.decline(judge_id, utcnow(), self.deadline, load)
        await self.works.save(work)
        await self._dispatch(work, events)
        return work

    async def list_works(self, judge_id=None, status: WorkStatus | None = None, contest_id=None,
                         offset: int = 0, count: int = 20):
        return await self.works.list(judge_id, status, contest_id, offset, count)

    async def file_appeal(self, work_id: uuid.UUID, participant_id: uuid.UUID, reason: str) -> Appeal:
        work = await self.works.get(work_id)
        appeal = Appeal.file(work, participant_id, reason, utcnow())
        await self.appeals.add(appeal)
        return appeal

    async def resolve_appeal(self, appeal_id: uuid.UUID, arbiter_id: uuid.UUID, new_score: int,
                             comment: str | None) -> Appeal:
        appeal = await self.appeals.get(appeal_id)
        work = await self.works.get(appeal.work_id)
        appeal.resolve(work, arbiter_id, new_score, comment, utcnow())
        await self.works.save(work)
        await self.appeals.save(appeal)
        await self.broker.publish("grade.revised", {
            "appeal_id": appeal.id, "work_id": work.id, "contest_id": work.contest_id,
            "problem_id": work.problem_id, "participant_id": work.participant_id,
            "old_score": appeal.old_score, "new_score": appeal.new_score,
            "max_score": work.max_score, "revised_at": appeal.resolved_at,
        })
        return appeal

    # ------------------------------------------------------------ публикация
    async def _dispatch(self, work: Work, events: list) -> None:
        for ev in events:
            if isinstance(ev, Assigned):
                a = ev.assignment
                await self.broker.publish("review.assigned", {
                    "work_id": work.id, "assignment_id": a.id, "judge_id": a.reviewer_id,
                    "role": a.role.value, "submission_id": work.submission_id,
                    "contest_id": work.contest_id, "problem_id": work.problem_id,
                    "attempt": a.attempt, "deadline_at": a.deadline_at,
                })
                if a.role is Role.JUDGE:
                    await self.broker.publish_to_queue(
                        DEADLINE_WAIT_QUEUE,
                        {"work_id": work.id, "assignment_id": a.id, "judge_id": a.reviewer_id,
                         "attempt": a.attempt},
                        expiration_ms=int(self.deadline.total_seconds() * 1000))
            elif isinstance(ev, DiscrepancyFound):
                log.info("work %s: discrepancy %s, escalated to arbiter", work.id, ev.scores)
            elif isinstance(ev, Approved):
                await self.broker.publish("grade.approved", {
                    "work_id": work.id, "submission_id": work.submission_id,
                    "contest_id": work.contest_id, "problem_id": work.problem_id,
                    "participant_id": work.participant_id, "score": work.final_score,
                    "max_score": work.max_score, "comment": work.comment,
                    "approved_by": work.approved_by.value, "approved_at": work.approved_at,
                })
