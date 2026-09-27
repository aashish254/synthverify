"""Application configuration.

Every knob is overridable through environment variables prefixed with ``SV_``
(e.g. ``SV_DATABASE_URL``, ``SV_DEFAULT_THRESHOLD``) or a ``.env`` file, so the
same image can be promoted from a laptop to a containerised enterprise
deployment without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central, typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="SV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    app_name: str = "SynthVerify"
    version: str = "1.0.0"
    environment: str = "development"  # development | staging | production
    log_level: str = "INFO"

    # ------------------------------------------------------------------ data
    # SQLite by default (zero-config); point at PostgreSQL in production.
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'synthverify.db'}"

    # Uploaded media + generated XAI artifacts live here.
    storage_dir: Path = PROJECT_ROOT / "data" / "media"
    artifacts_dir: Path = PROJECT_ROOT / "data" / "artifacts"

    # REQ-INFRA-4: where the *media* bytes go. "local" is the filesystem above;
    # "s3" is any S3-compatible endpoint - MinIO by default, AWS if pointed there.
    media_store: str = "local"
    s3_endpoint: str = ""  # e.g. http://minio:9000 - FC-2: self-hostable by default
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_path_style: bool = True  # False = virtual-host style (bucket.host/key)
    s3_prefix: str = ""  # optional key prefix inside the bucket

    # Single upload / inline analysis limit.
    max_upload_bytes: int = 64 * 1024 * 1024  # 64 MiB
    max_inline_bytes: int = 8 * 1024 * 1024  # 8 MiB for synchronous analyze

    # ------------------------------------------------------------- pipeline
    # Weight of each media-type detector group in the fused risk score.
    default_threshold: float = 0.65  # risk >= this -> HIGH tier
    critical_threshold: float = 0.85
    worker_count: int = 2
    queue_max_size: int = 512
    job_timeout_seconds: int = 300
    embedded_worker: bool = True  # run workers inside the API process

    # REQ-INFRA-2: who hands job ids to workers. "embedded" is the in-process priority
    # queue (the default, and the only one that needs no service); "postgres" makes the
    # jobs table itself the queue, claimed with FOR UPDATE SKIP LOCKED, so any number of
    # replicas share it and none of them needs Redis or RabbitMQ.
    job_broker: str = "embedded"
    broker_poll_seconds: float = 0.5  # how often a worker asks the queue for work
    worker_lease_seconds: int = 600  # a running job unclaimed for this long is re-queueable

    # --------------------------------------------------------------- webhooks
    webhook_timeout_seconds: float = 8.0
    webhook_max_attempts: int = 5
    webhook_backoff_base_seconds: float = 2.0

    # ------------------------------------------------------------------ auth
    # Master key used to bootstrap the first admin API key; if unset one is
    # generated on first boot and written to data/bootstrap_admin_key.txt.
    bootstrap_admin_key: str | None = None
    admin_email: str = "admin@synthverify.local"
    api_key_prefix: str = "sv"

    # ------------------------------------------------------------ rate limit
    rate_limit_rpm: int = 120  # requests per minute per API key
    rate_limit_burst: int = 40
    # REQ-INFRA-3: where the budget is kept. "in-process" is a token bucket inside each replica
    # (the default, and the only one that needs no service); "valkey" keeps one bucket per subject
    # inside a Valkey server so every replica shares it. The server is Valkey (BSD-3), not
    # Redis >= 7.4, whose RSALv2/SSPL licence FC-1 forbids. An unreachable Valkey degrades to the
    # in-process bucket rather than failing the request.
    rate_limit_backend: str = "in-process"
    rate_limit_valkey_host: str = "127.0.0.1"
    rate_limit_valkey_port: int = 6379
    rate_limit_timeout_seconds: float = 0.5  # how long one shared-bucket call may take
    rate_limit_fallback_cooldown_seconds: float = 5.0  # how long to stay local after a failure

    # -------------------------------------------------------------- policy
    # Enterprise workflow routing rules, overridable per organisation.
    block_score: float = 0.85  # auto-block / reject
    manual_review_score: float = 0.50  # route to human reviewer queue

    # -------------------------------------------------------------- retention
    # REQ-INFRA-5. Deletion is opt-in per organisation through the `retention_policies` table, so
    # `retention_default_days` is the only way to sweep media that has no policy row - and it ships
    # **unset** deliberately. "How long do we keep evidence" is a records-management and legal
    # decision that docs/goal-spec.md §4.3 states as a mechanism but never puts a number on; the
    # default that loses no data is the honest one, and an operator who sets it is signing off on a
    # number this spec does not authorise anyone here to invent.
    retention_default_days: int | None = None
    # The scheduler itself. On by default because it is inert while no policy exists, and because
    # `AC-INFRA-5` asks for the sweep to be *enforced by a scheduler* rather than run by hand.
    retention_sweep_enabled: bool = True
    retention_sweep_interval_seconds: float = 3600.0
    # Bounds one transaction: a first sweep on an old install can name far more rows than are worth
    # holding in a write lock, and a bounded sweep converges over passes instead.
    retention_batch_limit: int = 500

    # ----------------------------------------------------------- audit ledger
    # REQ-IDAM-4: `audit_events` is hash-chained, so verifying it is O(rows) and an install that has
    # sealed a million of them cannot re-hash the whole chain on every admin page view. A checkpoint
    # row seals the chain head every N appends, so verification can prove the tail from the last
    # checkpoint and still verify every hash *between* checkpoints. 0 disables sealing - the chain
    # stays intact and `GET /admin/audit/verify` falls back to verifying every row.
    audit_checkpoint_every: int = 5000

    @field_validator("audit_checkpoint_every", mode="after")
    @classmethod
    def _checkpoint_interval_is_a_count(cls, value: int) -> int:
        # Negative would mean "seal a checkpoint at a negative seq", which the append path cannot
        # answer; 0 is the documented "never seal".
        if value < 0:
            raise ValueError("SV_AUDIT_CHECKPOINT_EVERY must be 0 (never seal) or >= 1")
        return value

    @field_validator("retention_default_days", mode="after")
    @classmethod
    def _ttl_is_a_whole_day(cls, value: int | None) -> int | None:
        # A sub-day TTL is a purge, not a retention policy, and `retention_batch_limit` rows per pass
        # would not make it safe. 0 is rejected for the louder reason: it reads as "keep nothing",
        # which is a claim about the absence of evidence no config value should be able to make
        # implicitly.
        if value is not None and value < 1:
            raise ValueError("SV_RETENTION_DEFAULT_DAYS must be >= 1 (or unset to keep media forever)")
        return value

    @field_validator("storage_dir", "artifacts_dir", mode="after")
    @classmethod
    def _ensure_dirs(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @field_validator("worker_lease_seconds", mode="after")
    @classmethod
    def _lease_outlives_the_pipeline(cls, value: int, info: ValidationInfo) -> int:
        # A lease shorter than the pipeline would let a second worker reclaim a job that is
        # merely slow, so two replicas would run the same forensics concurrently and one of
        # them would have its write fenced away. Wasteful, not corrupt - but wasteful in the
        # exact configuration this setting exists to make fast.
        timeout = info.data.get("job_timeout_seconds")
        if timeout is not None and value <= timeout:
            raise ValueError(
                f"SV_WORKER_LEASE_SECONDS ({value}) must exceed SV_JOB_TIMEOUT_SECONDS ({timeout})"
            )
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (import-safe across modules and workers)."""
    return Settings()
