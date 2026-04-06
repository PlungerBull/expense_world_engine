from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_db_url: str
    supabase_jwt_secret: str

    model_config = {"env_file": ".env"}


settings = Settings()
