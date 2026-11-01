"""Сквозной сценарий платформы: от регистрации до рейтинга.

Запуск (после docker compose up): python scripts/demo.py
Нужен только httpx: pip install httpx
"""
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

HOST = sys.argv[1] if len(sys.argv) > 1 else "http://localhost"
URL = {name: f"{HOST}:{port}" for name, port in {
    "auth": 8001, "user": 8002, "problem": 8003, "contest": 8004,
    "submission": 8005, "review": 8006, "leaderboard": 8007, "notification": 8008}.items()}

http = httpx.Client(timeout=10)
step = 0


def call(method: str, service: str, path: str, expect: int = 200, **kw):
    global step
    step += 1
    r = http.request(method, URL[service] + path, **kw)
    mark = "OK " if r.status_code == expect else "ERR"
    print(f"{step:02d} {mark} {method:5} {service:12} {path} -> {r.status_code}")
    if r.status_code != expect:
        print("    ", r.text)
        sys.exit(1)
    return r.json() if "json" in r.headers.get("content-type", "") else r.text


def wait_for(fn, what: str, timeout: float = 20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.5)
    sys.exit(f"timeout waiting for {what}")


def register(login: str, name: str) -> str:
    acc = call("POST", "auth", "/api/auth/register", 201, json={
        "login": f"{login}{uuid.uuid4().hex[:4]}", "email": f"{login}@example.com",
        "password": "password123", "display_name": name})
    return acc["id"]


print("== 1. Пользователи (auth -> user.registered -> user)")
alice, bob = register("alice", "Алиса"), register("bob", "Борис")
judges = [register(f"judge{i}", f"Судья {i}") for i in (1, 2, 3)]
arbiter, author = register("arbiter", "Арбитр"), register("author", "Автор задачи")

print("== 2. Задача с критериями (problem)")
problem = call("POST", "problem", "/api/problems", 201, json={
    "title": "Сумма двух чисел", "statement": "Даны два целых числа. Выведите их сумму.",
    "author_id": author})
problem = call("PUT", "problem", f"/api/problems/{problem['id']}/criteria", json={"criteria": [
    {"name": "Корректность", "max_points": 6}, {"name": "Эффективность", "max_points": 3},
    {"name": "Оформление кода", "max_points": 1}]})
call("POST", "problem", f"/api/problems/{problem['id']}:publish")
crit = [c["id"] for c in problem["criteria"]]

print("== 3. Соревнование (contest)")
now = datetime.now(timezone.utc)
contest = call("POST", "contest", "/api/contests", 201, json={
    "title": "Осенний кубок", "problem_ids": [problem["id"]], "judge_ids": judges,
    "arbiter_id": arbiter, "starts_at": now.isoformat(),
    "submissions_close_at": (now + timedelta(hours=2)).isoformat()})
cid = contest["id"]
for user in (alice, bob):
    call("POST", "contest", f"/api/contests/{cid}/participants", 201, json={"user_id": user})
call("PATCH", "contest", f"/api/contests/{cid}", json={"status": "started"})

print("== 4. Решения (submission -> contest, solution.accepted -> review)")
for user, code in ((alice, "print(sum(map(int, input().split())))"),
                   (bob, "a, b = input().split()\nprint(int(a) + int(b))")):
    call("POST", "submission", "/api/submissions", 201, json={
        "contest_id": cid, "problem_id": problem["id"], "participant_id": user,
        "language": "python3", "source": code})


def works_ready():
    page = http.get(URL["review"] + "/api/works", params={"contest_id": cid}).json()
    return page["items"] if len(page["items"]) == 2 else None


works = {w["participant_id"]: w for w in wait_for(works_ready, "works in review")}
print("    обе работы назначены двум судьям каждая")


def active(work_id: str, role: str = "judge") -> list[str]:
    w = next(w for w in http.get(URL["review"] + "/api/works", params={"contest_id": cid})
             .json()["items"] if w["id"] == work_id)
    return [a["reviewer_id"] for a in w["assignments"] if a["status"] == "active" and a["role"] == role]


def grade(work_id: str, reviewer: str, points: list[int], comment: str):
    return call("POST", "review", f"/api/works/{work_id}:grade", json={
        "reviewer_id": reviewer, "comment": comment,
        "scores": [{"criterion_id": c, "points": p} for c, p in zip(crit, points)]})


print("== 5. Проверка работы Алисы: оценки близки -> итог = среднее (review -> problem)")
wa = works[alice]["id"]
j1, j2 = active(wa)
grade(wa, j1, [6, 3, 1], "Всё верно")
result = grade(wa, j2, [6, 2, 1], "Можно без map")
print(f"    статус {result['status']}, итог {result['final_score']} из {result['max_score']}")

print("== 6. Работа Бориса: отказ судьи -> переназначение, расхождение -> арбитр")
wb = works[bob]["id"]
first = active(wb)
w = call("POST", "review", f"/api/works/{wb}:decline", json={"judge_id": first[0]})
j1, j2 = active(wb)
grade(wb, j1, [6, 3, 1], "Решение корректно")
result = grade(wb, j2, [2, 1, 0], "Не проходит отрицательные числа")
print(f"    статус {result['status']} (расхождение 10 и 3 больше 20 % от 10)")
result = grade(wb, active(wb, "arbiter")[0], [5, 2, 1], "Отрицательные числа обрабатываются, но нет проверки ввода")
print(f"    арбитр утвердил итог {result['final_score']}")

print("== 7. Таблица результатов (grade.approved -> leaderboard)")
wait_for(lambda: len([r for r in http.get(URL["leaderboard"] + f"/api/leaderboards/{cid}")
                      .json()["rows"] if r["total"] > 0]) == 2, "leaderboard")
for row in call("GET", "leaderboard", f"/api/leaderboards/{cid}")["rows"]:
    print(f"    место {row['place']}: {row['participant_id']} — {row['total']}")

print("== 8. Апелляция Бориса (grade.revised)")
appeal = call("POST", "review", "/api/appeals", 201, json={
    "work_id": wb, "participant_id": bob, "reason": "Оформление соответствует требованиям"})
appeal = call("POST", "review", f"/api/appeals/{appeal['id']}:resolve", json={
    "arbiter_id": arbiter, "new_score": 9, "comment": "Апелляция удовлетворена частично"})
print(f"    оценка {appeal['old_score']} -> {appeal['new_score']}")

print("== 9. Завершение соревнования, итоговая таблица и рейтинг")
call("PATCH", "contest", f"/api/contests/{cid}", json={"status": "closed"})
call("PATCH", "contest", f"/api/contests/{cid}", json={"status": "finished"})
wait_for(lambda: http.get(URL["leaderboard"] + f"/api/leaderboards/{cid}").json()["status"] == "final",
         "final leaderboard")
table = call("POST", "leaderboard", f"/api/leaderboards/{cid}:publish")
for row in table["rows"]:
    print(f"    место {row['place']}: {row['participant_id']} — {row['total']}")
wait_for(lambda: http.get(URL["user"] + f"/api/users/{bob}").json().get("contests") == 1, "rating")
for user in (alice, bob):
    u = call("GET", "user", f"/api/users/{user}")
    print(f"    {u['display_name']} ({u['email']}): рейтинг {u['rating']}")

print("== 10. Уведомления Бориса (notification)")
for n in call("GET", "notification", "/api/notifications", params={"recipient_id": bob}):
    print(f"    [{n['event_type']}] {n['text']}")
print("Сценарий выполнен успешно")
