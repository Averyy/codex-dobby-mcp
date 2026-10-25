from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from codex_dobby_mcp.models import Completeness, ReviewAgent, ReviewAgentExecutionStatus
from codex_dobby_mcp.runner import _review_rollout_evidence


FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "codex-0.147-v2-review"
)
AGENTS = [ReviewAgent.CORRECTNESS, ReviewAgent.REGRESSION]


def _expected_spawn_messages() -> dict[str, str]:
    return json.loads(
        (FIXTURE_ROOT / "expected_spawn_messages.json").read_text(encoding="utf-8")
    )


def test_captured_codex_0_147_v2_rollouts_remain_compatible(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    shutil.copytree(FIXTURE_ROOT / "sessions", codex_home / "sessions")
    expected_messages = _expected_spawn_messages()

    evidence = _review_rollout_evidence(codex_home, AGENTS, expected_messages)

    assert evidence.collected is True
    assert evidence.validation_issues == ()
    assert [agent.observed_status for agent in evidence.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.COMPLETED,
    ]
    assert [agent.child_result.summary for agent in evidence.agents if agent.child_result] == [
        "Confirmed correctness fixture result.",
        "Confirmed regression fixture result.",
    ]
    assert all(agent.spawn_contract_issues == () for agent in evidence.agents)


def test_codex_0_164_v2_child_rollouts_use_spawn_output_identity(tmp_path: Path) -> None:
    from test_runner_schema import _review_rollout_evidence as synthetic_evidence  # noqa: PLC0415
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
        modern_v2=True,
    )

    evidence = synthetic_evidence(codex_home, AGENTS)

    assert evidence.validation_issues == ()
    assert [agent.observed_status for agent in evidence.agents] == [
        ReviewAgentExecutionStatus.COMPLETED,
        ReviewAgentExecutionStatus.COMPLETED,
    ]
    assert all(agent.started_count == 0 for agent in evidence.agents)
    assert all(agent.child_multi_agent_versions == ("v2",) for agent in evidence.agents)
    assert all(agent.spawn_output_task_names == (f"/root/{agent.task_name}",) for agent in evidence.agents)


def test_codex_0_164_v2_child_cannot_delegate(tmp_path: Path) -> None:
    from test_runner_schema import _review_rollout_evidence as synthetic_evidence  # noqa: PLC0415
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
        modern_v2=True,
        nested_collaboration=True,
    )

    evidence = synthetic_evidence(codex_home, AGENTS)
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    assert evidence.agents[0].nested_collaboration_calls == ("spawn_agent",)
    assert evidence.agents[1].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_codex_0_164_v2_spawn_output_must_match_child_path(tmp_path: Path) -> None:
    from test_runner_schema import _review_rollout_evidence as synthetic_evidence  # noqa: PLC0415
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
        modern_v2=True,
    )
    root_rollout = next((codex_home / "sessions").rglob("root.jsonl"))
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") == "function_call_output" and payload.get("call_id") == "spawn-correctness":
            payload["output"] = json.dumps({"task_name": "/root/unexpected"})
    root_rollout.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")

    evidence = synthetic_evidence(codex_home, AGENTS)

    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    assert evidence.agents[1].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_codex_0_164_non_object_spawn_output_is_ignored(tmp_path: Path) -> None:
    from test_runner_schema import _review_rollout_evidence as synthetic_evidence  # noqa: PLC0415
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    codex_home = tmp_path / "codex-home"
    _write_review_rollouts(
        codex_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
        modern_v2=True,
    )
    root_rollout = next((codex_home / "sessions").rglob("root.jsonl"))
    items = [json.loads(line) for line in root_rollout.read_text(encoding="utf-8").splitlines()]
    for item in items:
        payload = item.get("payload", {})
        if payload.get("type") == "function_call_output" and payload.get("call_id") == "spawn-correctness":
            payload["output"] = "[]"
    root_rollout.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")

    evidence = synthetic_evidence(codex_home, AGENTS)

    assert evidence.agents[0].spawn_output_task_names == (None,)
    assert evidence.agents[0].observed_status == ReviewAgentExecutionStatus.FAILED
    assert evidence.agents[1].observed_status == ReviewAgentExecutionStatus.COMPLETED


def test_fixture_assignment_digests_bind_the_complete_canonical_json() -> None:
    for message in _expected_spawn_messages().values():
        lines = message.splitlines()
        canonical_json = lines[2].removeprefix("Canonical assignment JSON: ")
        expected_digest = lines[3].removeprefix("Canonical assignment SHA-256: ")

        assert hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() == expected_digest


def _record_shapes(rollout: Path) -> list[tuple[str, str, str]]:
    """The structural signature of a rollout file, ignoring all values."""
    shapes: list[tuple[str, str, str]] = []
    for line in rollout.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        payload = item.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        discriminator = payload.get("name") or payload.get("kind") or ""
        shapes.append(
            (
                str(item.get("type")),
                str(payload.get("type")),
                str(discriminator),
            )
        )
    return shapes


def _key_shapes(rollout: Path) -> set[str]:
    """Dotted key paths of the structures the rollout parser reads."""
    keys: set[str] = set()

    def walk(prefix: str, value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                keys.add(path)
                walk(path, nested)

    for line in rollout.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = item.get("type")
        if kind == "session_meta":
            walk("session_meta", payload)
        elif kind == "turn_context":
            walk("turn_context", payload)
        elif kind == "event_msg" and payload.get("type") == "task_complete":
            keys.update(f"task_complete.{key}" for key in payload)
        elif kind == "event_msg" and payload.get("type") == "sub_agent_activity":
            keys.update(f"sub_agent_activity.{key}" for key in payload)
        elif kind == "response_item" and payload.get("type") == "function_call":
            keys.update(f"function_call.{key}" for key in payload)
        elif kind == "response_item" and payload.get("type") == "agent_message":
            keys.update(f"agent_message.{key}" for key in payload)
    return keys


def test_synthetic_rollout_writer_still_models_the_captured_shape(
    tmp_path: Path,
) -> None:
    """Guard the synthetic fixture writer against drifting from real Codex.

    Most rollout tests are driven by ``_write_review_rollouts`` rather than the
    captured corpus, so if that writer stops modelling a record or field real
    Codex emits, the parser and the writer can drift together and every test
    stays green. This asserts the writer still produces every structural shape
    present in the captured 0.147 rollouts.
    """
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    synthetic_home = tmp_path / "synthetic"
    _write_review_rollouts(
        synthetic_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
    )

    captured_root = next((FIXTURE_ROOT / "sessions").rglob("root.jsonl"))
    synthetic_root = next((synthetic_home / "sessions").rglob("root.jsonl"))
    captured_child = next(
        path
        for path in sorted((FIXTURE_ROOT / "sessions").rglob("*.jsonl"))
        if path.name != "root.jsonl"
    )
    synthetic_child = next(
        path
        for path in sorted((synthetic_home / "sessions").rglob("*.jsonl"))
        if path.name != "root.jsonl"
    )

    # The writer may model more than the capture (it also exercises the
    # list_agents delivery channel), but never less.
    missing_root = set(_record_shapes(captured_root)) - set(_record_shapes(synthetic_root))
    assert not missing_root, f"synthetic root no longer models: {sorted(missing_root)}"

    missing_child = set(_record_shapes(captured_child)) - set(
        _record_shapes(synthetic_child)
    )
    assert not missing_child, f"synthetic child no longer models: {sorted(missing_child)}"

    missing_keys = _key_shapes(captured_root) - _key_shapes(synthetic_root)
    assert not missing_keys, f"synthetic root dropped fields: {sorted(missing_keys)}"

    missing_child_keys = _key_shapes(captured_child) - _key_shapes(synthetic_child)
    assert not missing_child_keys, (
        f"synthetic child dropped fields: {sorted(missing_child_keys)}"
    )


def test_captured_and_synthetic_rollouts_agree_on_observed_status(
    tmp_path: Path,
) -> None:
    """Both corpora must reach the same verdict through the real parser."""
    from test_runner_schema import _write_review_rollouts  # noqa: PLC0415

    synthetic_home = tmp_path / "synthetic"
    _write_review_rollouts(
        synthetic_home,
        {ReviewAgent.CORRECTNESS: Completeness.FULL, ReviewAgent.REGRESSION: Completeness.FULL},
    )
    captured_home = tmp_path / "captured"
    shutil.copytree(FIXTURE_ROOT / "sessions", captured_home / "sessions")

    from test_runner_schema import _review_rollout_evidence as synthetic_evidence  # noqa: PLC0415

    synthetic = synthetic_evidence(synthetic_home, AGENTS)
    captured = _review_rollout_evidence(captured_home, AGENTS, _expected_spawn_messages())

    assert [agent.observed_status for agent in synthetic.agents] == [
        agent.observed_status for agent in captured.agents
    ]
    assert synthetic.validation_issues == captured.validation_issues == ()
    for agent in (*synthetic.agents, *captured.agents):
        assert agent.terminal_event_count == 1
        assert agent.delivered_count == 1
        assert agent.spawn_contract_issues == ()
        assert agent.attestation_issues == ()


def test_rollout_parser_discards_records_it_never_reads(tmp_path: Path) -> None:
    """Bulk rollout content must not be retained just to be ignored.

    A multi-lens review with history.persistence=save-all can write hundreds of
    MB of JSONL, almost all of it reasoning traces and streaming deltas that no
    consumption site looks at. Padding a real capture with that noise must not
    change the verdict, and must not grow what the parser holds.
    """
    from codex_dobby_mcp.runner import _is_consumed_rollout_record  # noqa: PLC0415

    baseline_home = tmp_path / "baseline"
    shutil.copytree(FIXTURE_ROOT / "sessions", baseline_home / "sessions")
    padded_home = tmp_path / "padded"
    shutil.copytree(FIXTURE_ROOT / "sessions", padded_home / "sessions")

    noise = [
        {"type": "response_item", "payload": {"type": "reasoning", "text": "x" * 4096}},
        {"type": "event_msg", "payload": {"type": "agent_reasoning_delta", "delta": "y" * 4096}},
        {"type": "event_msg", "payload": {"type": "token_count", "total": 1234}},
        {"type": "turn_diff", "payload": {"unified_diff": "z" * 4096}},
    ]
    for rollout in sorted((padded_home / "sessions").rglob("*.jsonl")):
        lines = rollout.read_text(encoding="utf-8").splitlines()
        padded = []
        for line in lines:
            padded.append(line)
            padded.extend(json.dumps(record) for record in noise)
        rollout.write_text("\n".join(padded) + "\n", encoding="utf-8")

    expected = _expected_spawn_messages()
    baseline = _review_rollout_evidence(baseline_home, AGENTS, expected)
    padded = _review_rollout_evidence(padded_home, AGENTS, expected)

    # Identical verdict despite an order of magnitude more input.
    assert padded.collected is baseline.collected
    assert padded.validation_issues == baseline.validation_issues == ()
    assert [agent.observed_status for agent in padded.agents] == [
        agent.observed_status for agent in baseline.agents
    ]
    assert [
        agent.child_result.summary for agent in padded.agents if agent.child_result
    ] == [agent.child_result.summary for agent in baseline.agents if agent.child_result]

    # And none of the noise is a record the parser would retain.
    assert not any(_is_consumed_rollout_record(record) for record in noise)


def test_every_consumed_record_shape_in_the_capture_is_retained() -> None:
    """The discard allowlist must not drop something the parser reads."""
    from codex_dobby_mcp.runner import _is_consumed_rollout_record  # noqa: PLC0415

    retained = 0
    for rollout in sorted((FIXTURE_ROOT / "sessions").rglob("*.jsonl")):
        for line in rollout.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert _is_consumed_rollout_record(record), record.get("type")
            retained += 1
    assert retained > 0
