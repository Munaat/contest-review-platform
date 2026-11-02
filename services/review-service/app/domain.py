"""Доменная модель контекста проверки.

Агрегат «Работа на проверку» (Work) удерживает инварианты экспертной оценки:
  * каждую работу независимо оценивают два судьи (два «слота» проверки);
  * в каждом слоте не более одного активного назначения;
  * один судья не может занимать оба слота одной работы;
  * оценку выставляет только судья (или арбитр) с активным назначением;
  * оценка не превышает максимальный балл задачи;
  * после трёх назначений в слоте (исходное + два переназначения) работа
    уходит арбитру;
  * если оценки судей расходятся больше чем на 20 % максимального балла,
    итоговую оценку выставляет арбитр, иначе итог — среднее двух оценок.

Агрегат «Апелляция» (Appeal): одна апелляция на работу, подаёт её только
участник-автор решения, рассматривает арбитр соревнования.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

REVIEWS_PER_WORK = 2
MAX_ATTEMPTS = 3
DISCREPANCY_RATIO = 0.2


# ---------------------------------------------------------------- ошибки
class DomainError(Exception):
    """Нарушение бизнес-правила (HTTP 409)."""


class InvalidInput(DomainError):
    """Некорректные данные (HTTP 422)."""


class Forbidden(DomainError):
    """Действие не разрешено этому участнику (HTTP 403)."""


class NotFound(Exception):
    """Объект не найден (HTTP 404)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- перечисления
class WorkStatus(str, enum.Enum):
    IN_REVIEW = "in_review"
    ARBITRATION = "arbitration"
    APPROVED = "approved"


class Role(str, enum.Enum):
    JUDGE = "judge"
    ARBITER = "arbiter"


class AssignmentStatus(str, enum.Enum):
    ACTIVE = "active"
    GRADED = "graded"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AppealStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


# ---------------------------------------------------------------- события агрегата
@dataclass
class Assigned:
    """Работа назначена судье или передана арбитру."""
    assignment: "Assignment"


@dataclass
class Approved:
    """Итоговая оценка утверждена."""


@dataclass
class DiscrepancyFound:
    """Расхождение оценок судей обнаружено."""
    scores: tuple[int, int]


# ---------------------------------------------------------------- сущности
@dataclass
class Assignment:
    id: uuid.UUID
    work_id: uuid.UUID
    reviewer_id: uuid.UUID
    role: Role
    slot: int            # 1, 2 — слоты судей; 0 — арбитр
    attempt: int         # номер назначения в слоте: 1..MAX_ATTEMPTS
    status: AssignmentStatus
    created_at: datetime
    deadline_at: datetime | None = None
    score: int | None = None
    comment: str | None = None
    graded_at: datetime | None = None


@dataclass
class Work:
    id: uuid.UUID
    submission_id: uuid.UUID
    contest_id: uuid.UUID
    problem_id: uuid.UUID
    participant_id: uuid.UUID
    jury: list[uuid.UUID]
    arbiter_id: uuid.UUID
    status: WorkStatus = WorkStatus.IN_REVIEW
    created_at: datetime = field(default_factory=utcnow)
    final_score: int | None = None
    max_score: int | None = None
    approved_by: Role | None = None
    approved_at: datetime | None = None
    comment: str | None = None
    assignments: list[Assignment] = field(default_factory=list)
    version: int = 0

    # ------------------------------------------------------------ создание
    @classmethod
    def start(cls, submission_id, contest_id, problem_id, participant_id, jury, arbiter_id,
              now: datetime, deadline: timedelta, load: dict[uuid.UUID, int]):
        """Команды «Поставить работу в очередь» и «Назначить работу судье»."""
        if not jury:
            raise DomainError("contest jury is empty")
        work = cls(id=uuid.uuid4(), submission_id=submission_id, contest_id=contest_id,
                   problem_id=problem_id, participant_id=participant_id,
                   jury=list(dict.fromkeys(jury)), arbiter_id=arbiter_id, created_at=now)
        events: list = []
        for slot in range(1, REVIEWS_PER_WORK + 1):
            events += work._assign_slot(slot, 1, now, deadline, load)
        return work, events

    # ------------------------------------------------------------ запросы
    def active_assignment_of(self, reviewer_id: uuid.UUID) -> Assignment | None:
        return next((a for a in self.assignments
                     if a.reviewer_id == reviewer_id and a.status is AssignmentStatus.ACTIVE), None)

    def assignment(self, assignment_id: uuid.UUID) -> Assignment | None:
        return next((a for a in self.assignments if a.id == assignment_id), None)

    def judge_scores(self) -> list[int]:
        return [a.score for a in self.assignments
                if a.role is Role.JUDGE and a.status is AssignmentStatus.GRADED]

    # ------------------------------------------------------------ назначения
    def _pick_judge(self, load: dict[uuid.UUID, int]) -> uuid.UUID | None:
        """Свободный судья с наименьшей загрузкой, ещё не работавший с этой работой."""
        used = {a.reviewer_id for a in self.assignments if a.role is Role.JUDGE}
        candidates = [j for j in self.jury if j not in used and j != self.participant_id]
        if not candidates:
            return None
        return min(candidates, key=lambda j: (load.get(j, 0), self.jury.index(j)))

    def _assign_slot(self, slot, attempt, now, deadline, load) -> list:
        judge = self._pick_judge(load)
        if judge is None:
            return self._escalate(now)
        a = Assignment(id=uuid.uuid4(), work_id=self.id, reviewer_id=judge, role=Role.JUDGE,
                       slot=slot, attempt=attempt, status=AssignmentStatus.ACTIVE,
                       created_at=now, deadline_at=now + deadline)
        self.assignments.append(a)
        load[judge] = load.get(judge, 0) + 1
        return [Assigned(a)]

    def _escalate(self, now: datetime) -> list:
        """Команда «Передать работу арбитру»."""
        if self.status is not WorkStatus.IN_REVIEW:
            return []
        self.status = WorkStatus.ARBITRATION
        for a in self.assignments:
            if a.status is AssignmentStatus.ACTIVE:
                a.status = AssignmentStatus.CANCELLED
        arb = Assignment(id=uuid.uuid4(), work_id=self.id, reviewer_id=self.arbiter_id,
                         role=Role.ARBITER, slot=0, attempt=1,
                         status=AssignmentStatus.ACTIVE, created_at=now)
        self.assignments.append(arb)
        return [Assigned(arb)]

    def _reassign(self, a: Assignment, now, deadline, load) -> list:
        """Команда «Переназначить работу» или эскалация после лимита."""
        if a.attempt >= MAX_ATTEMPTS:
            return self._escalate(now)
        return self._assign_slot(a.slot, a.attempt + 1, now, deadline, load)

    def decline(self, judge_id, now, deadline, load) -> list:
        """Команда «Отказаться от проверки»."""
        a = self.active_assignment_of(judge_id)
        if a is None or a.role is not Role.JUDGE:
            raise Forbidden("judge has no active assignment for this work")
        a.status = AssignmentStatus.DECLINED
        return self._reassign(a, now, deadline, load)

    def expire(self, assignment_id, now, deadline, load) -> tuple[bool, list]:
        """Команда «Проверить сроки проверки» для одного назначения.

        Возвращает (изменилось ли состояние, события). Назначение, уже закрытое
        оценкой или отказом, не меняется — таймер просто устарел.
        """
        a = self.assignment(assignment_id)
        if a is None or a.status is not AssignmentStatus.ACTIVE or a.role is not Role.JUDGE:
            return False, []
        if a.deadline_at is not None and a.deadline_at > now:
            return False, []
        a.status = AssignmentStatus.EXPIRED
        return True, self._reassign(a, now, deadline, load)

    # ------------------------------------------------------------ оценки
    def grade(self, reviewer_id, score: int, max_score: int, comment: str | None, now) -> list:
        """Команды «Выставить оценку», «Сверить оценки судей», «Утвердить итоговую оценку»."""
        if self.status is WorkStatus.APPROVED:
            raise DomainError("work is already approved")
        a = self.active_assignment_of(reviewer_id)
        if a is None:
            raise Forbidden("reviewer has no active assignment for this work")
        if not 0 <= score <= max_score:
            raise InvalidInput(f"score must be from 0 to {max_score}")
        a.status, a.score, a.comment, a.graded_at = AssignmentStatus.GRADED, score, comment, now
        self.max_score = max_score

        if a.role is Role.ARBITER:
            return self._approve(score, Role.ARBITER, comment, now)
        scores = self.judge_scores()
        if len(scores) < REVIEWS_PER_WORK:
            return []
        s1, s2 = scores[:2]
        if abs(s1 - s2) > DISCREPANCY_RATIO * max_score:
            return [DiscrepancyFound((s1, s2))] + self._escalate(now)
        comments = [x.comment for x in self.assignments
                    if x.role is Role.JUDGE and x.status is AssignmentStatus.GRADED and x.comment]
        return self._approve((s1 + s2 + 1) // 2, Role.JUDGE, "\n".join(comments) or None, now)

    def _approve(self, score, by: Role, comment, now) -> list:
        self.status = WorkStatus.APPROVED
        self.final_score, self.approved_by, self.approved_at, self.comment = score, by, now, comment
        return [Approved()]

    def revise(self, new_score: int) -> int:
        """Изменение итоговой оценки по апелляции; возвращает прежнюю оценку."""
        if self.status is not WorkStatus.APPROVED:
            raise DomainError("only approved work can be revised")
        if not 0 <= new_score <= (self.max_score or 0):
            raise InvalidInput(f"score must be from 0 to {self.max_score}")
        old, self.final_score = self.final_score, new_score
        return old


@dataclass
class Appeal:
    id: uuid.UUID
    work_id: uuid.UUID
    participant_id: uuid.UUID
    reason: str
    status: AppealStatus
    created_at: datetime
    old_score: int | None = None
    new_score: int | None = None
    comment: str | None = None
    resolved_by: uuid.UUID | None = None
    resolved_at: datetime | None = None

    @classmethod
    def file(cls, work: Work, participant_id: uuid.UUID, reason: str, now: datetime) -> "Appeal":
        """Команда «Подать апелляцию»."""
        if work.participant_id != participant_id:
            raise Forbidden("only the author of the solution can appeal")
        if work.status is not WorkStatus.APPROVED:
            raise DomainError("grade is not approved yet")
        if not reason.strip():
            raise InvalidInput("reason must not be empty")
        return cls(id=uuid.uuid4(), work_id=work.id, participant_id=participant_id,
                   reason=reason.strip(), status=AppealStatus.OPEN, created_at=now,
                   old_score=work.final_score)

    def resolve(self, work: Work, arbiter_id: uuid.UUID, new_score: int, comment: str | None,
                now: datetime) -> None:
        """Команда «Пересмотреть оценку»."""
        if self.status is AppealStatus.RESOLVED:
            raise DomainError("appeal is already resolved")
        if arbiter_id != work.arbiter_id:
            raise Forbidden("only the contest arbiter can resolve appeals")
        self.old_score = work.revise(new_score)
        self.new_score, self.comment = new_score, comment
        self.status, self.resolved_by, self.resolved_at = AppealStatus.RESOLVED, arbiter_id, now
