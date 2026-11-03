from pydantic import BaseModel, Field


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    skip_checkov: bool = False


class ValidationBody(BaseModel):
    ok: bool
    missing_binary: bool
    output: str
    policy_warnings: list[str] = Field(default_factory=list)


class PlanSummary(BaseModel):
    to_add: int = 0
    to_change: int = 0
    to_destroy: int = 0
    resources: list[str] = Field(default_factory=list)
    cost_warnings: list[str] = Field(default_factory=list)
    estimated_monthly_usd: float | None = None
    pricing_status: str = "not_requested"
    pricing_items: list[str] = Field(default_factory=list)


class FileChange(BaseModel):
    name: str
    status: str
    diff: str


class MessageResponse(BaseModel):
    reply: str
    files: list[str]
    validation: ValidationBody
    plan: ValidationBody
    scan: ValidationBody
    registry_docs: bool
    plan_summary: PlanSummary = Field(default_factory=PlanSummary)
    changes: list[FileChange] = Field(default_factory=list)


class AgentJobStatus(BaseModel):
    status: str = "idle"
    progress: list[dict[str, str]] = Field(default_factory=list)
    result: MessageResponse | None = None
    error: str | None = None


class OperationRecord(BaseModel):
    id: int
    kind: str
    status: str
    started_at: str
    finished_at: str | None = None
    summary: str = ""
    command: str = ""
    output: str = ""
    ok: bool | None = None


class OperationHistory(BaseModel):
    operations: list[OperationRecord] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approve: bool


class ErrorAnalysisBody(BaseModel):
    category: str
    title: str
    explanation: str
    recommendation: str
    resource: str | None = None


class CommandBody(BaseModel):
    ok: bool
    command: str
    output: str
    status: str = "completed"
    operation_id: int | None = None
    plan_summary: PlanSummary | None = None
    error_analysis: ErrorAnalysisBody | None = None


class FileBody(BaseModel):
    name: str
    content: str


class FilesResponse(BaseModel):
    files: list[FileBody]


class SessionStatus(BaseModel):
    status: str
    terraform_state: bool
    managed_resources: int = 0


class SessionSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class SessionsResponse(BaseModel):
    sessions: list[SessionSummary]


class ConversationMessage(BaseModel):
    role: str
    content: str


class ConversationResponse(BaseModel):
    messages: list[ConversationMessage] = Field(default_factory=list)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
