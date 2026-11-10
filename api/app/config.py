from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://bike:bike@localhost:5432/bike"
    redis_url: str = "redis://localhost:6379/0"
    app_url: str = "http://localhost:5173"
    company_name: str = "Saal Bikes"

    jwt_secret: str = "dev-only-secret-change-me-0123456789"
    access_ttl_s: int = 15 * 60
    refresh_ttl_s: int = 14 * 24 * 3600
    cookie_secure: bool = False
    bcrypt_rounds: int = 12

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "Saal Bikes <no-reply@saalbikes.test>"

    # Razorpay test mode. With no key id, payments run in fake mode (dev and tests only).
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = "dev-webhook-secret"

    # LLM providers (OpenAI-compatible APIs)
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_small: str = "openai/gpt-oss-20b"
    llm_big: str = "openai/gpt-oss-120b"
    llm_fallback: str = "openai/gpt-oss-20b:free"

    # Retrieval models, run on CPU through ONNX. fake_models swaps in tiny stand-ins for tests and CI.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "BAAI/bge-reranker-base"
    fake_models: bool = False
    model_cache_dir: str | None = None

    # Object storage (Garage, S3 API)
    s3_endpoint: str = "http://localhost:3900"
    s3_region: str = "garage"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "bikerental"

    @property
    def payments_fake(self) -> bool:
        return not self.razorpay_key_id


settings = Settings()

if settings.cookie_secure and ("change-me" in settings.jwt_secret or "dev-only" in settings.jwt_secret):
    raise RuntimeError("Set JWT_SECRET to a long random value before running with COOKIE_SECURE=true.")
