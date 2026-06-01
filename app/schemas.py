from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class RuntimeSettings(BaseModel):
    defaultAskTimeoutMs: int = 120_000
    maxPromptChars: int = 60_000
    newConversationPerRequest: bool = False
    autoStartWorker: bool = True
    workerRecoveryRetries: int = 1
    jobRetryAttempts: int = 1
    workerHealthcheckSeconds: int = 20
    workerHardTimeoutMs: int = 150_000
    maxConsecutiveFailuresBeforeRestart: int = 2
    maxQueueSize: int = 500
    schedulerPreferIdle: bool = True
    schedulerWaitForIdleSeconds: int = 120
    contextAutoResetEnabled: bool = True
    contextFullRetryOnce: bool = True
    openaiPromptMode: str = "latest_user"
    historyWindowTurns: int = 6
    agentToolResultMode: str = "fast_final"
    agentToolResultMaxChars: int = 6000


class CookieModel(BaseModel):
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    httpOnly: bool | None = None
    secure: bool | None = None
    sameSite: str | None = None
    session: bool | None = None
    storeId: str | None = None
    partitionKey: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class StorageModel(BaseModel):
    local: dict[str, Any] = Field(default_factory=dict)
    session: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class DeepSeekState(BaseModel):
    schemaVersion: str | None = None
    capturedAt: str | None = None
    pageUrl: str | None = None
    page: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, Any] = Field(default_factory=dict)
    storage: StorageModel = Field(default_factory=StorageModel)
    headers: dict[str, Any] = Field(default_factory=dict)
    cookies: list[CookieModel] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class AccountSummary(BaseModel):
    accountId: str
    displayName: str
    createdAt: str
    updatedAt: str
    capturedAt: str | None = None
    pageUrl: str | None = None
    schemaVersion: str | None = None
    cookieCount: int = 0
    encrypted: bool = True
    notes: str | None = None
    enabled: bool = True
    priority: int = 100
    weight: int = 100


class AccountImportRequest(BaseModel):
    accountId: str = Field(min_length=1)
    displayName: str = Field(min_length=1)
    notes: str | None = None
    enabled: bool = True
    priority: int = 100
    weight: int = 100
    state: DeepSeekState


class AccountUpdateRequest(BaseModel):
    displayName: str | None = None
    notes: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    weight: int | None = None


class AccountImportResponse(BaseModel):
    ok: bool
    account: AccountSummary


class AccountListResponse(BaseModel):
    ok: bool
    accounts: list[AccountSummary]


class CheckStateResponse(BaseModel):
    ok: bool
    loggedInGuess: bool
    finalUrl: str | None = None
    title: str | None = None
    hasTextarea: bool = False
    hasEditable: bool = False
    loginTextDetected: bool = False
    captchaTextDetected: bool = False
    message: str


class WorkerStatusResponse(BaseModel):
    accountId: str
    running: bool
    busy: bool
    reserved: bool = False
    finalUrl: str | None = None
    title: str | None = None
    lastStartedAt: str | None = None
    lastAskAt: str | None = None
    lastError: str | None = None
    restartCount: int = 0
    consecutiveFailures: int = 0
    lastRecoveredAt: str | None = None


class WorkerFleetResponse(BaseModel):
    ok: bool
    workers: list[WorkerStatusResponse]


class AskResponse(BaseModel):
    ok: bool
    answer: str | None = None
    elapsedMs: int
    message: str
    status: WorkerStatusResponse



class DomProbeRequest(BaseModel):
    prompt: str = Field(default="请用两句话介绍你能做什么。", min_length=1)
    newConversation: bool = False
    timeoutMs: int = Field(default=120_000, ge=10_000, le=300_000)

class BridgeChatRequest(BaseModel):
    sessionId: str | None = None
    answerFormat: str | None = None
    message: str = Field(min_length=1)
    system: str | None = None
    accountId: str | None = None
    newConversation: bool | None = None
    timeoutMs: int | None = Field(default=None, ge=10_000, le=300_000)


class BridgeChatResponse(BaseModel):
    ok: bool
    answer: str | None = None
    message: str
    elapsedMs: int
    accountId: str | None = None
    jobId: str | None = None
    status: WorkerStatusResponse | None = None


class JobCreateRequest(BaseModel):
    sessionId: str | None = None
    answerFormat: str | None = None
    message: str = Field(min_length=1)
    system: str | None = None
    accountId: str | None = None
    newConversation: bool | None = None
    timeoutMs: int | None = Field(default=None, ge=10_000, le=300_000)
    retryAttempts: int | None = Field(default=None, ge=0, le=5)


class JobRecord(BaseModel):
    jobId: str
    sessionId: str | None = None
    answerFormat: str | None = None
    type: str = "chat"
    status: Literal["queued", "running", "succeeded", "failed", "timeout", "cancelled"]
    accountId: str | None = None
    createdAt: str
    startedAt: str | None = None
    finishedAt: str | None = None
    message: str
    system: str | None = None
    answer: str | None = None
    error: str | None = None
    elapsedMs: int | None = None
    timeoutMs: int | None = None
    newConversation: bool | None = None
    queuePosition: int | None = None
    retryAttempts: int = 0
    attempt: int = 0


class JobCreateResponse(BaseModel):
    ok: bool
    job: JobRecord


class JobListResponse(BaseModel):
    ok: bool
    jobs: list[JobRecord]
    total: int
    page: int
    pageSize: int
    pages: int


class OpenAIMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool", "developer"] | str
    content: Any = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    model_config = ConfigDict(extra="allow")


class OpenAIChatCompletionRequest(BaseModel):
    model: str = "deepseek-web"
    sessionId: str | None = None
    metadata: dict[str, Any] | None = None
    messages: list[OpenAIMessage]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    parallel_tool_calls: bool | None = None
    response_format: Any | None = None
    reasoning_effort: str | None = None
    web_search_options: Any | None = None
    stop: Any | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    user: str | None = None
    model_config = ConfigDict(extra="allow")


class ColumnStat(BaseModel):
    name: str
    type: str
    nullCount: int = 0
    nullRatio: float = 0.0
    distinctCount: int | None = None
    examples: list[Any] = Field(default_factory=list)
    min: Any | None = None
    max: Any | None = None
    avg: float | None = None


class DatasetMeta(BaseModel):
    datasetId: str
    originalFilename: str
    storedPath: str
    extension: str
    tableName: str = "data_table"
    uploadedAt: str
    rowCount: int
    columns: list[ColumnStat]
    previewRows: list[dict[str, Any]] = Field(default_factory=list)


class DatasetUploadResponse(BaseModel):
    ok: bool
    dataset: DatasetMeta


class DatasetListResponse(BaseModel):
    ok: bool
    datasets: list[DatasetMeta]


class DataQueryRequest(BaseModel):
    datasetId: str
    question: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=200)
    accountId: str | None = None


class DataQueryResponse(BaseModel):
    ok: bool
    sql: str | None = None
    explain: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rowCount: int = 0
    rawModelAnswer: str | None = None
    jobId: str | None = None
    accountId: str | None = None
    message: str


class AnthropicContentBlock(BaseModel):
    type: str
    text: str | None = None
    source: Any | None = None
    name: str | None = None
    id: str | None = None
    input: Any | None = None
    content: Any | None = None
    model_config = ConfigDict(extra="allow")


class AnthropicMessage(BaseModel):
    role: str
    content: Any = ""


class AnthropicTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class AnthropicMessageRequest(BaseModel):
    model: str = "deepseek-web"
    messages: list[AnthropicMessage]
    system: Any | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: Any | None = None
    max_tokens: int | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")
