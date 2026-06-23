"""Tests for Codex runner error classification and local helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.runner import (
    AgentSetupError,
    QuotaExhaustedError,
    TransientAgentError,
    _classify_api_error,
    _config_overrides_for_add_dirs,
    _extract_payload_for_schema,
    _load_codex_output_schema,
    _load_codex_sdk,
    _log_progress,
    _sandbox_name_for_tools,
    run_agent,
)
from audit.json_utils import validate_schema


@pytest.mark.parametrize("text", [
    "You're out of extra usage · resets 2am (Europe/Rome)",
    "Usage limit reached for the day.",
    "You've hit your ChatGPT usage limit. Try again later.",
    "Your plan has no remaining quota.",
    "YOU'RE OUT OF EXTRA USAGE.",
    "You've hit your session limit · resets 5:10am (UTC)",
    "You've hit your session limit · resets 11pm",
])
def test_quota_classified(text: str) -> None:
    label, exc = _classify_api_error(text)
    assert label == "quota_exhausted"
    assert exc is QuotaExhaustedError


@pytest.mark.parametrize("text", [
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary",
    "Server overloaded — please try again",
    "API Error: 503",
    "API Error: 502 Bad Gateway",
    "API Error: 500 Internal Server Error",
    "rate_limit hit",
    "Rate limit reached for gpt-5.4.",
    "Service temporarily unavailable",
])
def test_transient_classified(text: str) -> None:
    label, exc = _classify_api_error(text)
    assert label == "transient"
    assert exc is TransientAgentError


def test_unknown_defaults_to_transient() -> None:
    label, exc = _classify_api_error("some weird new error string")
    assert label == "unknown_api_error"
    assert exc is TransientAgentError


def test_empty_defaults_to_transient() -> None:
    label, exc = _classify_api_error("")
    assert label == "unknown_api_error"
    assert exc is TransientAgentError


def test_missing_codex_sdk_has_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("audit.runner.importlib.import_module", fake_import)

    with pytest.raises(AgentSetupError, match="pip install -e"):
        _load_codex_sdk()


def test_sandbox_mapping_uses_workspace_write_only_for_bash() -> None:
    assert _sandbox_name_for_tools(["Read", "Grep", "Glob"]) == "read_only"
    assert _sandbox_name_for_tools(["Read", "Grep", "Glob", "Bash"]) == "workspace_write"


def test_config_overrides_include_additional_writable_roots(tmp_path) -> None:
    extra = tmp_path / "repo"
    overrides = _config_overrides_for_add_dirs([extra])

    assert any("sandbox_workspace_write.writable_roots" in item for item in overrides)
    assert any(str(extra) in item for item in overrides)


def test_progress_log_includes_stage_ref_model_and_elapsed(caplog) -> None:
    caplog.set_level("INFO", logger="audit.runner")

    _log_progress(
        event="heartbeat",
        stage="hunt",
        artifact_name="task_1",
        model="gpt-5.4",
        elapsed_s=65.2,
    )

    text = caplog.text
    assert "[hunt/task_1]" in text
    assert "heartbeat" in text
    assert "model=gpt-5.4" in text
    assert "elapsed=65s" in text


def test_codex_output_schema_inlines_refs_and_requires_nullable_optionals() -> None:
    schema = _load_codex_output_schema(Path("schemas/recon_output.schema.json"))

    def assert_structured_output_subset(node):
        if isinstance(node, dict):
            assert "$schema" not in node
            assert "$ref" not in node
            if node.get("type") == "object":
                properties = node.get("properties", {})
                assert node.get("additionalProperties") is False
                assert node.get("required") == list(properties)
            for value in node.values():
                assert_structured_output_subset(value)
        elif isinstance(node, list):
            for value in node:
                assert_structured_output_subset(value)

    assert_structured_output_subset(schema)
    subsystem = schema["properties"]["subsystems"]["items"]
    assert subsystem["properties"]["external_dependencies"]["type"] == ["array", "null"]

    hunt_task = schema["properties"]["initial_tasks"]["items"]
    assert hunt_task["title"] == "HuntTask"
    assert hunt_task["properties"]["source"]["type"] == ["string", "null"]
    assert hunt_task["properties"]["source"]["enum"] == ["recon", "gapfill", "feedback", None]


def test_extract_payload_for_schema_strips_null_optional_fields() -> None:
    payload = {
        "subsystems": [
            {
                "name": "web",
                "path": "app.py",
                "language": "python",
                "purpose": "HTTP handlers",
                "external_dependencies": None,
            }
        ],
        "architecture": {
            "build_commands": ["pip install -r requirements.txt"],
            "test_commands": None,
            "entry_points": [
                {
                    "kind": "http_route",
                    "location": "app.py:lookup",
                    "auth_required": None,
                    "notes": None,
                }
            ],
            "trust_boundaries": [
                {
                    "name": "http_to_db",
                    "description": "HTTP query string to SQL",
                    "source_zone": None,
                    "sink_zone": None,
                }
            ],
            "external_inputs": None,
        },
        "initial_tasks": [
            {
                "task_id": "t_routes_sqli_1",
                "attack_class": "sql_injection",
                "scope_hint": "HTTP handler reads name and passes it into SQL",
                "target_files": ["app.py"],
                "rationale": "Direct interpolation of untrusted input.",
                "priority": 1,
                "source": None,
            }
        ],
    }

    schema_file = Path("schemas/recon_output.schema.json")
    normalized = _extract_payload_for_schema(json.dumps(payload), schema_file)

    assert normalized["subsystems"][0] == {
        "name": "web",
        "path": "app.py",
        "language": "python",
        "purpose": "HTTP handlers",
    }
    assert "test_commands" not in normalized["architecture"]
    assert "source" not in normalized["initial_tasks"][0]
    assert validate_schema(normalized, schema_file) == []


async def test_run_agent_repairs_schema_errors_in_same_codex_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return hunt output.")
    schema = Path("schemas/finding.schema.json")
    artifact_dir = tmp_path / "artifacts"
    cwd = tmp_path / "target"
    cwd.mkdir()
    prompts_seen: list[str] = []
    thread_ids: list[str] = []

    class FakeSandbox:
        read_only = "read_only"
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        auto_review = "auto_review"

    class FakeCodexConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeThread:
        id = "thread_same"

        async def run(self, prompt_text, **kwargs):
            prompts_seen.append(prompt_text)
            thread_ids.append(self.id)
            assert kwargs["sandbox"] == "read_only"
            if len(prompts_seen) == 1:
                return SimpleNamespace(
                    final_response='{"task_id": "t_1"}',
                    usage=SimpleNamespace(total=SimpleNamespace(
                        input_tokens=10,
                        output_tokens=5,
                        cached_input_tokens=0,
                        reasoning_output_tokens=0,
                        total_tokens=15,
                    )),
                    duration_ms=10,
                    status="completed",
                    items=[],
                )
            return SimpleNamespace(
                final_response=(
                    '{"task_id":"t_1","findings":[],"gaps_observed":[]}'
                ),
                usage=SimpleNamespace(total=SimpleNamespace(
                    input_tokens=20,
                    output_tokens=8,
                    cached_input_tokens=0,
                    reasoning_output_tokens=0,
                    total_tokens=28,
                )),
                duration_ms=20,
                status="completed",
                items=[],
            )

    class FakeAsyncCodex:
        def __init__(self, config=None):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def thread_start(self, **kwargs):
            assert kwargs["developer_instructions"].startswith("Return hunt output.")
            assert kwargs["model"] == "gpt-5.4"
            assert kwargs["cwd"] == str(cwd)
            assert kwargs["sandbox"] == "read_only"
            return FakeThread()

    fake_sdk = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr("audit.runner._load_codex_sdk", lambda: fake_sdk)

    result = await run_agent(
        stage="hunt",
        prompt_file=prompt,
        user_input={"task_id": "t_1"},
        schema_file=schema,
        allowed_tools=["Read", "Grep", "Glob"],
        model="gpt-5.4",
        cwd=cwd,
        artifact_dir=artifact_dir,
        artifact_name="t_1",
        repair_attempts=1,
    )

    assert result.payload == {"task_id": "t_1", "findings": [], "gaps_observed": []}
    assert result.repair_used is True
    assert len(prompts_seen) == 2
    assert "schema validation" in prompts_seen[1]
    assert thread_ids == ["thread_same", "thread_same"]
