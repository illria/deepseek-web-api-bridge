from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    state_encryption_key: str | None = None
    allow_plaintext_state: bool = False

    accounts_dir: Path = Path("data/accounts")
    jobs_dir: Path = Path("data/jobs")
    runtime_settings_file: Path = Path("data/settings/runtime.json")

    bridge_api_keys: str = ""

    browser_headless: bool = True
    browser_no_sandbox: bool = True
    browser_timeout_ms: int = 45_000

    default_ask_timeout_ms: int = 120_000
    max_prompt_chars: int = 60_000
    new_conversation_per_request: bool = False
    auto_start_worker: bool = True
    worker_recovery_retries: int = 1
    worker_healthcheck_seconds: int = 20
    worker_hard_timeout_ms: int = 150_000
    max_consecutive_failures_before_restart: int = 2
    context_auto_reset_enabled: bool = True
    context_full_retry_once: bool = True
    max_queue_size: int = 500

    upload_dir: Path = Path("data/uploads")
    dataset_meta_dir: Path = Path("data/datasets")
    max_upload_mb: int = 100
    max_result_rows: int = 200

    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def api_key_set(self) -> set[str]:
        return {x.strip() for x in self.bridge_api_keys.split(",") if x.strip()}


settings = Settings()
