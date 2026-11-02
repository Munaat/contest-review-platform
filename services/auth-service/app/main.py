"""auth-service — учётные записи и вход (минимальная заглушка).

По ПР4 аутентификация относится к неспециализированной подобласти и в
промышленной системе заменяется готовым решением (Keycloak). Здесь сервис
хранит учётные записи в памяти, выдаёт JWT и публикует user.registered.
"""
import hashlib
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from app.messaging import Broker, NullBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Settings(BaseSettings):
    amqp_url: str = ""
    jwt_secret: str = "change-me"


settings = Settings()


class RegisterRequest(BaseModel):
    login: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    login: str
    password: str


class Account(BaseModel):
    id: uuid.UUID
    login: str
    email: str
    display_name: str
    registered_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID


accounts: dict[uuid.UUID, tuple[Account, bytes, bytes]] = {}


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.broker = Broker(settings.amqp_url, "auth") if settings.amqp_url else NullBroker("auth")
    await app.state.broker.connect()
    yield
    await app.state.broker.close()


app = FastAPI(title="Auth Service", version="1.0.0", lifespan=lifespan,
              description="Учётные записи и выдача токенов (заглушка вместо Keycloak)")


@app.post("/api/auth/register", status_code=201, response_model=Account, summary="Зарегистрироваться")
async def register(body: RegisterRequest, request: Request):
    if any(a.login == body.login for a, _, _ in accounts.values()):
        raise HTTPException(409, "login is already taken")
    salt = os.urandom(16)
    acc = Account(id=uuid.uuid4(), login=body.login, email=body.email,
                  display_name=body.display_name, registered_at=datetime.now(timezone.utc))
    accounts[acc.id] = (acc, salt, hash_password(body.password, salt))
    await request.app.state.broker.publish("user.registered", {
        "user_id": acc.id, "login": acc.login, "email": acc.email,
        "display_name": acc.display_name, "registered_at": acc.registered_at})
    return acc


@app.post("/api/auth/login", response_model=Token, summary="Войти в систему")
async def login(body: LoginRequest):
    for acc, salt, pwd in accounts.values():
        if acc.login == body.login and hmac.compare_digest(pwd, hash_password(body.password, salt)):
            token = jwt.encode({"sub": str(acc.id), "login": acc.login,
                                "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                               settings.jwt_secret, algorithm="HS256")
            return Token(access_token=token, user_id=acc.id)
    raise HTTPException(401, "invalid login or password")


@app.get("/api/accounts/{user_id}", response_model=Account, summary="Учётная запись")
async def read_account(user_id: uuid.UUID):
    if user_id not in accounts:
        raise HTTPException(404, f"account {user_id} not found")
    return accounts[user_id][0]
