from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # пусто -> данные хранятся в памяти (удобно для локального запуска без БД)
    postgres_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
