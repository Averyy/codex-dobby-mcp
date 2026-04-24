from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class ToolName(str, Enum):
    PLAN = "plan"
    RESEARCH = "research"
    BRAINSTORM = "brainstorm"
    BUILD = "build"
    VALIDATE = "validate"
    REVIEW = "review"
    REVERSE_ENGINEER = "reverse_engineer"


class ReasoningEffort(str, Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class RunStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class AsyncRunState(str, Enum):
    RUNNING = "running"
    FINISHED = "finished"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


class ResultArtifactState(str, Enum):
    PLACEHOLDER = "placeholder"
    FINAL = "final"


class Completeness(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class ReviewAgent(str, Enum):
    GENERALIST = "generalist"
    SECURITY = "security"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    CORRECTNESS = "correctness"
    UX = "ux"
    REGRESSION = "regression"


SUPPORTED_REVIEW_AGENT_VALUES = tuple(agent.value for agent in ReviewAgent)
SUPPORTED_REVIEW_AGENTS_TEXT = ", ".join(SUPPORTED_REVIEW_AGENT_VALUES)


def parse_review_agents_input(value: object) -> list[ReviewAgent] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(
            f"Review agents must be a list. Supported agents: {SUPPORTED_REVIEW_AGENTS_TEXT}."
        )

    parsed: list[ReviewAgent] = []
    invalid: list[str] = []
    for raw_agent in value:
        if isinstance(raw_agent, ReviewAgent):
            parsed.append(raw_agent)
            continue
        if not isinstance(raw_agent, str):
            invalid.append(repr(raw_agent))
            continue

        cleaned = raw_agent.strip()
        if not cleaned:
            invalid.append("<empty>")
            continue
        try:
            parsed.append(ReviewAgent(cleaned))
        except ValueError:
            invalid.append(cleaned)

    if invalid:
        message = (
            f"Unsupported review agents: {', '.join(invalid)}. "
            f"Supported agents: {SUPPORTED_REVIEW_AGENTS_TEXT}."
        )
        raise ValueError(message)

    return parsed


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_TOOL_TIMEOUT_SECONDS: dict["ToolName", int] = {}  # populated after ToolName definition
CODEX_DOBBY_DIRNAME = ".codex-dobby"
CODEX_DOBBY_GITIGNORE_ENTRY = ".codex-dobby/"
RECURSION_GUARD_ENV = "CODEX_DOBBY_ACTIVE"
READ_ONLY_TOOLS = frozenset({ToolName.PLAN, ToolName.RESEARCH, ToolName.BRAINSTORM, ToolName.REVIEW})
MUTATING_TOOLS = frozenset({ToolName.BUILD, ToolName.VALIDATE, ToolName.REVERSE_ENGINEER})
DEFAULT_REVIEW_AGENTS = (ReviewAgent.GENERALIST,)
DEFAULT_REASONING_EFFORTS: dict[ToolName, ReasoningEffort] = {
    ToolName.BUILD: ReasoningEffort.HIGH,
    ToolName.RESEARCH: ReasoningEffort.MEDIUM,
    ToolName.BRAINSTORM: ReasoningEffort.HIGH,
    ToolName.PLAN: ReasoningEffort.HIGH,
    ToolName.VALIDATE: ReasoningEffort.MEDIUM,
    ToolName.REVIEW: ReasoningEffort.HIGH,
    ToolName.REVERSE_ENGINEER: ReasoningEffort.HIGH,
}
DEFAULT_TOOL_TIMEOUT_SECONDS.update({
    ToolName.PLAN: 600,
    ToolName.RESEARCH: 1200,
    ToolName.BRAINSTORM: 600,
    ToolName.BUILD: 1200,
    ToolName.VALIDATE: 600,
    ToolName.REVIEW: 600,
    ToolName.REVERSE_ENGINEER: 1800,
})


class InvocationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    repo_root: str | None = None
    files: list[str] = Field(default_factory=list)
    important_context: str | None = None
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=300)
    extra_roots: list[str] = Field(default_factory=list)
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    agents: list[ReviewAgent] = Field(default_factory=list)
    danger: bool = False

    @field_validator("files", "extra_roots")
    @classmethod
    def strip_empty_items(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned

    @field_validator("agents", mode="before")
    @classmethod
    def parse_agents(cls, value: object) -> list[ReviewAgent]:
        parsed = parse_review_agents_input(value)
        return [] if parsed is None else parsed

    @field_validator("prompt")
    @classmethod
    def strip_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Prompt must not be empty")
        return cleaned


class WorkerResult(BaseModel):
    summary: str = Field(..., min_length=1)
    completeness: Completeness
    important_facts: list[str]
    next_steps: list[str]
    files_changed: list[str]
    warnings: list[str]


class ReviewDetails(BaseModel):
    requested_review_agents: list[ReviewAgent] = Field(default_factory=list)
    effective_review_agents: list[ReviewAgent] = Field(default_factory=list)


class GhidraUsageMode(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PRELAUNCH_FAILURE = "prelaunch_failure"
    NO_ACTIVITY = "no_activity"
    STARTUP_ONLY = "startup_only"
    DIRECT_MCP = "direct_mcp"
    HELPER_FALLBACK = "helper_fallback"


class GhidraDetails(BaseModel):
    configured: bool
    mode: GhidraUsageMode
    summary: str
    mcp_calls: list[str] = Field(default_factory=list)
    helper_calls: list[str] = Field(default_factory=list)


class ReverseEngineerDetails(BaseModel):
    ghidra: GhidraDetails | None = None


class ToolResponse(BaseModel):
    task_id: str
    tool: ToolName
    status: RunStatus
    summary: str
    completeness: Completeness
    important_facts: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    artifact_paths: dict[str, str]
    sandbox_violations: list[str] = Field(default_factory=list)
    repo_root: str
    exit_code: int | None = None
    duration_ms: int | None = None
    warnings: list[str] = Field(default_factory=list)
    raw_output_available: bool = True
    model: str
    reasoning_effort: ReasoningEffort
    result_state: ResultArtifactState = ResultArtifactState.FINAL
    review_details: ReviewDetails | None = None
    reverse_engineer_details: ReverseEngineerDetails | None = None


class AsyncRunHandle(BaseModel):
    task_id: str
    tool: ToolName
    state: AsyncRunState
    summary: str
    repo_root: str
    artifact_paths: dict[str, str]
    model: str
    reasoning_effort: ReasoningEffort


class RunLookupResponse(BaseModel):
    task_id: str
    state: AsyncRunState
    summary: str
    repo_root: str
    tool: ToolName | None = None
    status: RunStatus | None = None
    result_state: ResultArtifactState | None = None
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    result: ToolResponse | None = None
    warnings: list[str] = Field(default_factory=list)
    pending_task_ids: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    task_id: str
    state: AsyncRunState
    summary: str
    repo_root: str
    tool: ToolName | None = None
    status: RunStatus | None = None
    result_state: ResultArtifactState | None = None


class RunListResponse(BaseModel):
    repo_root: str
    runs: list[RunSummary] = Field(default_factory=list)


class RunArtifacts(BaseModel):
    run_dir: Path
    request_json: Path
    prompt_txt: Path
    stdout_log: Path
    stderr_log: Path
    last_message_txt: Path
    result_json: Path
    output_schema_json: Path

    def as_public_dict(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "request_json": str(self.request_json),
            "prompt_txt": str(self.prompt_txt),
            "stdout_log": str(self.stdout_log),
            "stderr_log": str(self.stderr_log),
            "last_message_txt": str(self.last_message_txt),
            "result_json": str(self.result_json),
            "output_schema_json": str(self.output_schema_json),
        }


class ResolvedInvocation(BaseModel):
    tool: ToolName
    request: InvocationRequest
    requested_timeout_seconds: int
    requested_review_agents: list[ReviewAgent] = Field(default_factory=list)
    repo_root: Path
    model: str
    reasoning_effort: ReasoningEffort
    sandbox_roots: list[Path] = Field(default_factory=list)
    writable_roots: list[Path] = Field(default_factory=list)
    advisory_read_only_roots: list[Path] = Field(default_factory=list)
    fetchaller_available: bool = False
    ghidra_available: bool = False
    artifacts: RunArtifacts
    gitignore_updated: bool = False


class RepoSnapshot(BaseModel):
    head_commit: str | None = None
    status_entries: list[str] = Field(default_factory=list)
    dirty_files: list[str] = Field(default_factory=list)
    path_fingerprints: dict[str, str | None] = Field(default_factory=dict)
