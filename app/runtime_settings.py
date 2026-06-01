from __future__ import annotations

from app.config import settings
from app.schemas import RuntimeSettings


class RuntimeSettingsStore:
    def __init__(self) -> None:
        self.path = settings.runtime_settings_file
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def defaults(self) -> RuntimeSettings:
        return RuntimeSettings(
            defaultAskTimeoutMs=settings.default_ask_timeout_ms,
            maxPromptChars=settings.max_prompt_chars,
            newConversationPerRequest=False,
            autoStartWorker=settings.auto_start_worker,
            workerRecoveryRetries=settings.worker_recovery_retries,
            jobRetryAttempts=1,
            workerHealthcheckSeconds=settings.worker_healthcheck_seconds,
            workerHardTimeoutMs=settings.worker_hard_timeout_ms,
            maxConsecutiveFailuresBeforeRestart=settings.max_consecutive_failures_before_restart,
            maxQueueSize=settings.max_queue_size,
            schedulerPreferIdle=True,
            schedulerWaitForIdleSeconds=120,
            contextAutoResetEnabled=True,
            contextFullRetryOnce=True,
            openaiPromptMode="latest_user",
            historyWindowTurns=6,
            agentToolResultMode="fast_final",
            agentToolResultMaxChars=6000,
        )

    def get(self) -> RuntimeSettings:
        if not self.path.exists():
            current = self.defaults()
            self.save(current)
            return current
        try:
            return RuntimeSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except Exception:
            current = self.defaults()
            self.save(current)
            return current

    def save(self, value: RuntimeSettings) -> RuntimeSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        return value


runtime_store = RuntimeSettingsStore()
