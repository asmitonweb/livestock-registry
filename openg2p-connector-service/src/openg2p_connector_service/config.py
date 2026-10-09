from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONNECTOR_",
        env_file=".env",
        extra="allow",
        env_nested_delimiter="__",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8050
    openapi_title: str = "OpenG2P Connector Service"
    openapi_version: str = "0.1.0"

    # --- database (connector-local) ---
    db_driver: str = "postgresql+asyncpg"
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_username: str = "postgres"
    db_password: str = "password"
    db_dbname: str = "connector"

    # --- partner ingest target ---
    partner_ingest_base_url: str = "http://localhost:8000"
    partner_ingest_timeout: int = 30

    # --- celery / redis ---
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_backend_url: str = ""

    @model_validator(mode="after")
    def _sync_celery_backend(self):
        if self.celery_backend_url and self.celery_result_backend == "redis://localhost:6379/1":
            self.celery_result_backend = self.celery_backend_url
        return self

    # --- webhook auth ---
    webhook_default_secret: str = ""

    # --- deprecated ODK globals (migrate to per-connector source_config_json) ---
    odk_central_base_url: str = ""
    odk_central_email: str = ""
    odk_central_password: str = ""
    odk_poll_interval_seconds: int = 300
    # httpx read/connect budget for ODK Central OData and session POST (seconds).
    # Per-connector ``source_config_json.http_timeout_seconds`` overrides for polls only.
    odk_http_timeout_seconds: float = 60.0

    # --- operational ---
    worker_max_attempts: int = 5
    log_level: str = "INFO"
    # Per-submission ingestion events (JSON lines), also written to stdout.
    # Empty turns the file off.
    ingest_log_file: str = "logs/odk-ingest.jsonl"
    ingest_log_max_bytes: int = 20 * 1024 * 1024
    ingest_log_backups: int = 10

    # --- feature flags ---
    otel_enabled: bool = False
    validate_mapped_payload: bool = False

    # --- backpressure ---
    global_max_in_flight: int = 50

    # --- CORS (for standalone UI on a different origin) ---
    cors_origins: str = ""

    # --- webhook rate-limiting (Redis sliding window) ---
    webhook_rate_limit_enabled: bool = False
    webhook_rate_limit_max: int = 100
    webhook_rate_limit_window_seconds: int = 60

    # --- metrics (Prometheus) ---
    metrics_enabled: bool = False
    metrics_path: str = "/metrics"

    # --- Read-only metadata lookups ---
    # Used to populate UI dropdowns for G2P envelope fields. If unset, the
    # endpoints return an empty list and the UI falls back to free-text input
    # with a warning. DSNs should use the async driver (postgresql+asyncpg).
    master_data_db_dsn: str = ""
    registry_db_dsn: str = ""

    # Persist per-run payload snapshots for debugging (disable in prod if sensitive).
    store_run_payloads: bool = True
    run_payload_max_bytes: int = 262_144

    # --- enterprise polling / checkpointing ---
    # When True, transports that cannot do a server-side incremental
    # query MUST raise instead of silently re-downloading every record.
    # Per-connector source_config_json may override with
    # ``"strict_incremental": false`` for low-volume / dev forms.
    strict_incremental: bool = True
    # When True, the silent fallback to a full scan is allowed even
    # under strict_incremental=true. This is the explicit opt-in that
    # documents "I accept periodic full re-fetches for this connector."
    full_scan_on_incremental_unsupported: bool = False

    @property
    def db_datasource(self) -> str:
        return (
            f"{self.db_driver}://{self.db_username}:{self.db_password}"
            f"@{self.db_hostname}:{self.db_port}/{self.db_dbname}"
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
