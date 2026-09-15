import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ENVIRONMENT: str = "demo"
    DB_NAME: str = "rag_demo"
    DB_USER: str = "rag"
    DB_PASSWORD: str = ""
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3307
    DB_URL: str = ""
    RAG_CSRF_SECRET: str = "default-csrf-secret-change-in-prod"
    RAG_COOKIE_SECURE: bool = False
    PORT: int = 8080

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore"
    )

    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.DB_URL and self.DB_URL.startswith("mysql+pymysql://"):
            return self.DB_URL
        if self.DB_URL and self.DB_URL.startswith("jdbc:mysql://"):
            # Convert jdbc:mysql://localhost:3307/rag_demo?... to mysql+pymysql://...
            # e.g. jdbc:mysql://localhost:3307/rag_demo?connectionTimeZone=UTC...
            import re
            m = re.match(r"jdbc:mysql://([^:/]+):(\d+)/([^?]+)", self.DB_URL)
            if m:
                host, port, db = m.groups()
                return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{host}:{port}/{db}?charset=utf8mb4"
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"


def load_settings() -> Settings:
    # Check for .runtime/<ENVIRONMENT>.env first
    rag_root = Path(__file__).resolve().parent.parent.parent
    env_name = os.getenv("RAG_ENVIRONMENT", "demo")
    runtime_env = rag_root / f".runtime/{env_name}.env"
    env_file = runtime_env if runtime_env.exists() else rag_root / ".env"
    
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    return Settings(
        ENVIRONMENT=os.getenv("RAG_ENVIRONMENT", "demo"),
        DB_NAME=os.getenv("DB_NAME", "rag_demo"),
        DB_USER=os.getenv("DB_USER", "rag"),
        DB_PASSWORD=os.getenv("DB_PASSWORD", ""),
        DB_HOST=os.getenv("DB_HOST", "127.0.0.1"),
        DB_PORT=int(os.getenv("DB_PORT", "3307")),
        DB_URL=os.getenv("DB_URL", ""),
        RAG_CSRF_SECRET=os.getenv("RAG_CSRF_SECRET", "default-csrf-secret-change-in-prod"),
        RAG_COOKIE_SECURE=os.getenv("RAG_COOKIE_SECURE", "false").lower() in ("true", "1", "yes"),
        PORT=int(os.getenv("PORT", "8080")),
    )


settings = load_settings()
