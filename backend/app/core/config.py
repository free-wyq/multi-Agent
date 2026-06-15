"""
应用配置（pydantic-settings）
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 应用
    DEBUG: bool = False
    APP_NAME: str = "Multi-Agent Framework"

    # 数据库
    DATABASE_URL: str = "postgresql+asyncpg://agenticx@localhost:5432/multi_agent"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""

    # Docker
    DOCKER_HOST: str = "unix:///var/run/docker.sock"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
