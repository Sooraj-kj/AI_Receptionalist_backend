from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Clienxo AI Receptionist API"
    database_url: str = "sqlite:///./clienxo_demo.db"
    app_secret: str = "change-me-for-production"
    frontend_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    demo_admin_email: str = "reception@clienxo.demo"
    demo_admin_password: str = "demo12345"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
