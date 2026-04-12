from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_db_url: str
    supabase_jwt_secret: str

    # Connection pool — tune via env vars for different environments.
    # Supabase free tier allows ~60 connections; paid plans allow more.
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    model_config = {"env_file": ".env"}


settings = Settings()
