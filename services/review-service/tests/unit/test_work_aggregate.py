"""Модульные тесты агрегатов «Работа на проверку» и «Апелляция» (без БД и брокера)."""
import uuid
from datetime import timedelta

import pytest

from app.domain import (Appeal, AppealStatus, Approved, Assigned, AssignmentStatus,
                        DiscrepancyFound, DomainError, Forbidden, InvalidInput, Role, Work,
                        WorkStatus, utcnow)

DEADLINE = timedelta(minutes=30)
MAX = 10


@pytest.fixture()
def jury():
    return [uuid.uuid4() for _ in range(4)]


@pytest.fixture()
def arbiter():
    return uuid.uuid4()


def start(jury, arbiter, load=None, participant=None):
    return Work.start(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), participant or uuid.uuid4(),
                      jury, arbiter, utcnow(), DEADLINE, load if load is not None else {})


def active_judges(work):
    return [a.reviewer_id for a in work.assignments
            if a.status is AssignmentStatus.ACTIVE and a.role is Role.JUDGE]


def test_start_assigns_two_different_judges(jury, arbiter):
    work, events = start(jury, arbiter)
    judges = active_judges(work)
    assert len(judges) == 2 and len(set(judges)) == 2
    assert {a.slot for a in work.assignments} == {1, 2}
    assert all(isinstance(e, Assigned) for e in events) and len(events) == 2
    assert all(a.deadline_at is not None for a in work.assignments)


def test_start_prefers_least_loaded_judges(jury, arbiter):
    load = {jury[0]: 5, jury[1]: 3, jury[2]: 0, jury[3]: 1}
    work, _ = start(jury, arbiter, load)
    assert set(active_judges(work)) == {jury[2], jury[3]}


def test_participant_is_never_assigned_to_own_work(jury, arbiter):
    work, _ = start(jury, arbiter, participant=jury[0])
    assert jury[0] not in active_judges(work)


def test_start_with_empty_jury_is_rejected(arbiter):
    with pytest.raises(DomainError):
        start([], arbiter)


def test_grade_by_not_assigned_reviewer_is_forbidden(jury, arbiter):
    work, _ = start(jury, arbiter)
    with pytest.raises(Forbidden):
        work.grade(uuid.uuid4(), 5, MAX, None, utcnow())


def test_grade_above_max_score_is_rejected(jury, arbiter):
    work, _ = start(jury, arbiter)
    with pytest.raises(InvalidInput):
        work.grade(active_judges(work)[0], MAX + 1, MAX, None, utcnow())


def test_first_grade_keeps_work_in_review(jury, arbiter):
    work, _ = start(jury, arbiter)
    assert work.grade(active_judges(work)[0], 7, MAX, None, utcnow()) == []
    assert work.status is WorkStatus.IN_REVIEW


def test_close_grades_are_approved_with_average(jury, arbiter):
    work, _ = start(jury, arbiter)
    j1, j2 = active_judges(work)
    work.grade(j1, 9, MAX, "хорошо", utcnow())
    events = work.grade(j2, 8, MAX, "неплохо", utcnow())
    assert any(isinstance(e, Approved) for e in events)
    assert work.status is WorkStatus.APPROVED
    assert work.final_score == 9          # (9 + 8) / 2 = 8.5 -> округление вверх
    assert work.approved_by is Role.JUDGE


def test_discrepant_grades_go_to_arbiter(jury, arbiter):
    work, _ = start(jury, arbiter)
    j1, j2 = active_judges(work)
    work.grade(j1, 10, MAX, None, utcnow())
    events = work.grade(j2, 3, MAX, None, utcnow())
    assert isinstance(events[0], DiscrepancyFound)
    assert work.status is WorkStatus.ARBITRATION
    arb = work.active_assignment_of(arbiter)
    assert arb is not None and arb.role is Role.ARBITER


def test_arbiter_grade_approves_work(jury, arbiter):
    work, _ = start(jury, arbiter)
    j1, j2 = active_judges(work)
    work.grade(j1, 10, MAX, None, utcnow())
    work.grade(j2, 2, MAX, None, utcnow())
    events = work.grade(arbiter, 7, MAX, "итог арбитра", utcnow())
    assert any(isinstance(e, Approved) for e in events)
    assert (work.final_score, work.approved_by) == (7, Role.ARBITER)


def test_grade_after_approval_is_rejected(jury, arbiter):
    work, _ = start(jury, arbiter)
    j1, j2 = active_judges(work)
    work.grade(j1, 8, MAX, None, utcnow())
    work.grade(j2, 8, MAX, None, utcnow())
    with pytest.raises(DomainError):
        work.grade(j1, 9, MAX, None, utcnow())


def test_decline_reassigns_slot_to_new_judge(jury, arbiter):
    work, _ = start(jury, arbiter)
    j1, j2 = active_judges(work)
    events = work.decline(j1, utcnow(), DEADLINE, {})
    new = events[0].assignment
    assert new.slot == work.assignments[0].slot and new.attempt == 2
    assert new.reviewer_id not in (j1, j2)


def test_expire_ignores_already_graded_assignment(jury, arbiter):
    work, _ = start(jury, arbiter)
    first = work.assignments[0]
    work.grade(first.reviewer_id, 5, MAX, None, utcnow())
    changed, events = work.expire(first.id, utcnow() + DEADLINE, DEADLINE, {})
    assert (changed, events) == (False, [])


def test_expire_before_deadline_does_nothing(jury, arbiter):
    work, _ = start(jury, arbiter)
    changed, _ = work.expire(work.assignments[0].id, utcnow(), DEADLINE, {})
    assert changed is False


def test_third_expiration_in_slot_escalates_to_arbiter(arbiter):
    jury = [uuid.uuid4() for _ in range(6)]
    work, _ = start(jury, arbiter)
    late = utcnow()
    slot_one = work.assignments[0]
    for _ in range(2):                      # исходное назначение и одно переназначение
        late += DEADLINE * 2                # каждый раз срок нового назначения тоже истекает
        _, events = work.expire(slot_one.id, late, DEADLINE, {})
        slot_one = events[0].assignment
    assert slot_one.attempt == 3
    late += DEADLINE * 2
    _, events = work.expire(slot_one.id, late, DEADLINE, {})
    assert work.status is WorkStatus.ARBITRATION
    assert events[0].assignment.role is Role.ARBITER
    assert not active_judges(work)          # оставшееся назначение судьи снято


def test_no_free_judge_escalates_to_arbiter(arbiter):
    jury = [uuid.uuid4(), uuid.uuid4()]
    work, _ = start(jury, arbiter)
    events = work.decline(jury[0], utcnow(), DEADLINE, {})
    assert work.status is WorkStatus.ARBITRATION
    assert events[0].assignment.reviewer_id == arbiter


# ---------------------------------------------------------------- апелляции
@pytest.fixture()
def approved_work(jury, arbiter):
    participant = uuid.uuid4()
    work, _ = start(jury, arbiter, participant=participant)
    j1, j2 = active_judges(work)
    work.grade(j1, 6, MAX, None, utcnow())
    work.grade(j2, 6, MAX, None, utcnow())
    return work


def test_only_author_can_appeal(approved_work):
    with pytest.raises(Forbidden):
        Appeal.file(approved_work, uuid.uuid4(), "не согласен", utcnow())


def test_appeal_requires_approved_grade(jury, arbiter):
    participant = uuid.uuid4()
    work, _ = start(jury, arbiter, participant=participant)
    with pytest.raises(DomainError):
        Appeal.file(work, participant, "не согласен", utcnow())


def test_arbiter_resolves_appeal_and_revises_score(approved_work, arbiter):
    appeal = Appeal.file(approved_work, approved_work.participant_id, "учтите оформление", utcnow())
    appeal.resolve(approved_work, arbiter, 8, "согласен", utcnow())
    assert appeal.status is AppealStatus.RESOLVED
    assert (appeal.old_score, appeal.new_score, approved_work.final_score) == (6, 8, 8)


def test_judge_cannot_resolve_appeal(approved_work, jury):
    appeal = Appeal.file(approved_work, approved_work.participant_id, "учтите оформление", utcnow())
    with pytest.raises(Forbidden):
        appeal.resolve(approved_work, jury[0], 8, None, utcnow())


def test_appeal_cannot_be_resolved_twice(approved_work, arbiter):
    appeal = Appeal.file(approved_work, approved_work.participant_id, "учтите оформление", utcnow())
    appeal.resolve(approved_work, arbiter, 8, None, utcnow())
    with pytest.raises(DomainError):
        appeal.resolve(approved_work, arbiter, 9, None, utcnow())


def test_revised_score_cannot_exceed_max(approved_work, arbiter):
    appeal = Appeal.file(approved_work, approved_work.participant_id, "учтите оформление", utcnow())
    with pytest.raises(InvalidInput):
        appeal.resolve(approved_work, arbiter, MAX + 1, None, utcnow())
