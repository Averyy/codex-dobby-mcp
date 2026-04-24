from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codex_dobby_mcp.models import DEFAULT_REVIEW_AGENTS, ReasoningEffort, ReviewAgent


@dataclass(frozen=True)
class ReviewAgentDefinition:
    review_agent: ReviewAgent
    codex_name: str
    label: str
    description: str
    filename: str


REVIEW_SUBAGENT_DEFAULT_MODEL = "gpt-5.5"
REVIEW_SUBAGENT_DEFAULT_REASONING_EFFORT = ReasoningEffort.MEDIUM


REVIEW_AGENT_DEFINITIONS: dict[ReviewAgent, ReviewAgentDefinition] = {
    ReviewAgent.GENERALIST: ReviewAgentDefinition(
        review_agent=ReviewAgent.GENERALIST,
        codex_name="dobby_review_generalist",
        label="generalist quick pass",
        description="Fast read-only reviewer for an initial bug and regression sweep.",
        filename="review-generalist.toml",
    ),
    ReviewAgent.SECURITY: ReviewAgentDefinition(
        review_agent=ReviewAgent.SECURITY,
        codex_name="dobby_review_security",
        label="security",
        description="Read-only reviewer focused on vulnerabilities, trust boundaries, and secrets.",
        filename="review-security.toml",
    ),
    ReviewAgent.PERFORMANCE: ReviewAgentDefinition(
        review_agent=ReviewAgent.PERFORMANCE,
        codex_name="dobby_review_performance",
        label="performance",
        description="Read-only reviewer focused on real performance risks and hot paths.",
        filename="review-performance.toml",
    ),
    ReviewAgent.ARCHITECTURE: ReviewAgentDefinition(
        review_agent=ReviewAgent.ARCHITECTURE,
        codex_name="dobby_review_architecture",
        label="architecture",
        description="Read-only reviewer focused on structure, boundaries, and convention drift.",
        filename="review-architecture.toml",
    ),
    ReviewAgent.CORRECTNESS: ReviewAgentDefinition(
        review_agent=ReviewAgent.CORRECTNESS,
        codex_name="dobby_review_correctness",
        label="correctness",
        description="Read-only reviewer focused on logic bugs, edge cases, and behavioral defects.",
        filename="review-correctness.toml",
    ),
    ReviewAgent.UX: ReviewAgentDefinition(
        review_agent=ReviewAgent.UX,
        codex_name="dobby_review_ux",
        label="ux and accessibility",
        description="Read-only reviewer focused on user-facing defects and accessibility failures.",
        filename="review-ux.toml",
    ),
    ReviewAgent.REGRESSION: ReviewAgentDefinition(
        review_agent=ReviewAgent.REGRESSION,
        codex_name="dobby_review_regression",
        label="regression and patterns",
        description="Read-only reviewer focused on regressions, local rules, and repeated project pitfalls.",
        filename="review-regression.toml",
    ),
}


def selected_review_agents(agents: list[ReviewAgent]) -> list[ReviewAgent]:
    chosen = agents or list(DEFAULT_REVIEW_AGENTS)
    return list(dict.fromkeys(chosen))


def selected_review_agent_definitions(agents: list[ReviewAgent]) -> list[ReviewAgentDefinition]:
    return [REVIEW_AGENT_DEFINITIONS[agent] for agent in selected_review_agents(agents)]


def review_uses_orchestrator(agents: list[ReviewAgent]) -> bool:
    return len(selected_review_agents(agents)) > 1


def review_agent_assets_root(assets_root: Path) -> Path:
    return assets_root / "codex_agents"


def review_agents_root(assets_root: Path) -> Path:
    return review_agent_assets_root(assets_root)
