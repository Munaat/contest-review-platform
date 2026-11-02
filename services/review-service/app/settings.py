from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    postgres_url: str = ""            # пусто -> хранение в памяти
    amqp_url: str = ""                # пусто -> без брокера (события только в журнал)
    problem_url: str = "http://localhost:8003"
    contest_url: str = "http://localhost:8004"
    review_deadline_seconds: int = 3600

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
