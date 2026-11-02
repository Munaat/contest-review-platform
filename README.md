# Платформа онлайн-соревнований с экспертной проверкой

Учебный проект по дисциплине «Микросервисная архитектура» (РТУ МИРЭА, ИКБО-64-23).
Аналог Codeforces, в котором решения оценивает не машина, а судьи: работа
назначается двум судьям, при просрочке переназначается, при расхождении оценок
уходит арбитру, а участник может подать апелляцию.

Модель предметной области построена методом Event Storming (ПР1–ПР4), обмен
сообщениями спроектирован в ПР5, API — в ПР6.

## Сервисы

| Сервис | Порт | Хранилище | Эндпоинты | Назначение |
|---|---|---|---|---|
| auth-service | 8001 | память | 3 | Учётные записи, JWT (заглушка вместо Keycloak) |
| user-service | 8002 | память | 2 | Профили и рейтинг |
| **problem-service** | 8003 | PostgreSQL | 5 | Задачи и критерии оценивания |
| contest-service | 8004 | память | 4 | Соревнования, жюри, регистрация |
| submission-service | 8005 | память | 3 | Приём решений |
| **review-service** | 8006 | PostgreSQL | 5 | Экспертная проверка, арбитраж, апелляции |
| leaderboard-service | 8007 | память | 2 | Таблицы результатов |
| notification-service | 8008 | память | 2 | Уведомления (имитация SMTP/Telegram) |

Выделенные сервисы (смысловое ядро и задачи) реализованы полностью и покрыты
тестами; остальные — в минимальном виде, достаточном для сквозного сценария.

Инфраструктура: PostgreSQL 16 (порт 5433), RabbitMQ 3 с веб-интерфейсом
(http://localhost:15672, логин и пароль `contest`).

## Взаимодействие

Синхронно (HTTP/REST):

| Вызов | Зачем |
|---|---|
| review → problem | Критерии и максимальный балл при выставлении оценки |
| review → contest | Состав жюри и арбитр при постановке работы в очередь |
| submission → contest | Соревнование идёт, участник зарегистрирован |
| leaderboard → contest | Состав участников при старте соревнования |
| user → auth | Данные учётной записи в профиле |
| user → leaderboard | Итоговые места для пересчёта рейтинга |

Асинхронно (RabbitMQ): каждый сервис публикует события в обменник
`<сервис>.events` (direct), каждый потребитель читает очередь `<сервис>.inbox`.
Сообщение, обработка которого упала три раза, уходит в `<сервис>.inbox.dlq`.

| Событие | Издатель | Получатели |
|---|---|---|
| user.registered | auth | user |
| contest.started / submissions_closed / finished | contest | leaderboard, notification (+ user для finished) |
| solution.accepted | submission | review |
| review.assigned | review | notification |
| grade.approved | review | leaderboard, notification |
| grade.revised | review | leaderboard, notification |

Срок проверки реализован таймером на TTL сообщения: `review.deadline.wait` →
(TTL) → `review.timers` → `review.deadline`.

## Запуск

```bash
docker compose up --build
```

Документация API каждого сервиса (Swagger): `http://localhost:800X/docs`.

Сквозной сценарий (регистрация → задача → соревнование → решения → проверка →
арбитраж → апелляция → итоговая таблица → рейтинг → уведомления):

```bash
pip install httpx
python scripts/demo.py
```

## Тесты

Для review-service и problem-service написаны модульные и интеграционные тесты.

```bash
cd services/review-service          # или services/problem-service
pip install -r requirements-dev.txt
pytest tests/unit                   # модульные: без БД и брокера
# интеграционные: нужен PostgreSQL, например из docker compose
TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/postgres pytest tests/integration
```

## Структура сервиса (review-service)

```
app/
  domain.py        агрегаты Work и Appeal с инвариантами
  repositories.py  in-memory и PostgreSQL реализации репозиториев
  db.py            ORM-схема SQLAlchemy
  service.py       сервисный слой: команды, публикация событий, таймеры
  clients.py       HTTP-клиенты problem-service и contest-service
  consumers.py     обработчики очередей RabbitMQ
  messaging.py     обёртка над aio-pika (копия есть в каждом сервисе)
  api.py, schemas.py  HTTP-контроллер и схемы
  main.py          точка входа
tests/unit, tests/integration
```
