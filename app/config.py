from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_db_url: str
    supabase_jwt_secret: str

    # Connection pool — sized for the Supabase pgBouncer transaction-mode
    # pooler (port 6543), which multiplexes these client conns onto a much
    # smaller real-connection set. Treat max_size as a logical limit, not a
    # real-Postgres limit. Direct-connection deployments should drop max_size
    # back to ~20 to stay under Supabase's per-project ceiling.
    db_pool_min_size: int = 5
    db_pool_max_size: int = 50

    model_config = {"env_file": ".env"}


settings = Settings()
