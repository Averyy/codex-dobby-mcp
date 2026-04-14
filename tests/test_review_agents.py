from pathlib import Path
import tomllib

from codex_dobby_mcp.models import DEFAULT_REVIEW_AGENTS, ReviewAgent
from codex_dobby_mcp.review_agents import REVIEW_AGENT_DEFINITIONS, review_agent_assets_root, selected_review_agents


def test_shipped_review_agent_files_exist_and_match_registered_names() -> None:
    assets_root = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets"
    agents_root = review_agent_assets_root(assets_root)

    for definition in REVIEW_AGENT_DEFINITIONS.values():
        config_path = agents_root / definition.filename
        assert config_path.exists()

        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert payload["name"] == definition.codex_name
        assert payload["description"] == definition.description
        assert payload["sandbox_mode"] == "read-only"


def test_review_agents_absorb_domain_specific_guidance() -> None:
    assets_root = Path(__file__).resolve().parents[1] / "src" / "codex_dobby_mcp" / "assets"
    agents_root = review_agent_assets_root(assets_root)

    security = (agents_root / "review-security.toml").read_text(encoding="utf-8")
    correctness = (agents_root / "review-correctness.toml").read_text(encoding="utf-8")
    architecture = (agents_root / "review-architecture.toml").read_text(encoding="utf-8")
    regression = (agents_root / "review-regression.toml").read_text(encoding="utf-8")

    assert "frontend: XSS, CSRF, open redirects" in security
    assert "backend and data: transaction boundaries" in correctness
    assert "systems: hardware abstraction boundaries" in architecture
    assert "backend and data: migration safety" in regression
    assert "accessible labels" not in regression


def test_default_review_agents_is_single_generalist() -> None:
    assert DEFAULT_REVIEW_AGENTS == (ReviewAgent.GENERALIST,)
    assert selected_review_agents([]) == [ReviewAgent.GENERALIST]
