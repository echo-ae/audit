"""Run one Codex agent and validate its schema-shaped final output.

Each call starts a local Codex SDK thread, sends one JSON user payload, records
SDK result summaries to a JSONL artifact, validates the final JSON object, and
uses repair turns in the same thread when schema validation fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.json_utils import extract_json, validate_schema

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    payload: dict
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    num_turns: int | None
    duration_ms: int | None
    session_id: str | None
    artifact_path: Path
    repair_used: bool
    raw_result_message: dict = field(default_factory=dict)


class AgentRunError(RuntimeError):
    """Schema validation failed after repair attempts."""


class AgentSetupError(RuntimeError):
    """Local Codex SDK/runtime setup is missing or unusable."""


class TransientAgentError(RuntimeError):
    """Codex returned a retryable service, timeout, or rate-limit error."""


class QuotaExhaustedError(RuntimeError):
    """The Codex subscription/session quota is exhausted until reset."""


_QUOTA_MARKERS = (
    "out of extra usage",
    "usage limit reached",
    "chatgpt usage limit",
    "session limit",
    "your plan has no remaining",
    "quota exhausted",
)

_TRANSIENT_MARKERS = (
    "api error: 529",
    "overloaded",
    "api error: 503",
    "api error: 502",
    "api error: 504",
    "api error: 500",
    "rate_limit",
    "rate limit reached",
    "temporarily unavailable",
    "service unavailable",
    "server busy",
    "transport closed",
    "timeout",
)


def _classify_api_error(text: str) -> tuple[str, type[RuntimeError]]:
    t = (text or "").lower()
    if any(m in t for m in _QUOTA_MARKERS):
        return "quota_exhausted", QuotaExhaustedError
    if any(m in t for m in _TRANSIENT_MARKERS):
        return "transient", TransientAgentError
    return "unknown_api_error", TransientAgentError


async def run_agent(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None = None,
    max_turns: int = 25,
    permission_mode: str = "acceptEdits",
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int = 1,
    transient_retries: int = 3,
    transient_base_delay: float = 30.0,
) -> AgentResult:
    """Run one Codex agent, retrying transient errors with backoff."""
    del max_turns, permission_mode  # Codex SDK thread config owns turn policy.

    last_exc: RuntimeError | None = None
    for attempt in range(transient_retries + 1):
        try:
            return await _run_agent_once(
                stage=stage,
                prompt_file=prompt_file,
                user_input=user_input,
                schema_file=schema_file,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
                add_dirs=add_dirs,
                artifact_dir=artifact_dir,
                artifact_name=artifact_name,
                repair_attempts=repair_attempts,
            )
        except QuotaExhaustedError:
            raise
        except TransientAgentError as e:
            last_exc = e
            if attempt >= transient_retries:
                break
            delay = min(transient_base_delay * (2 ** attempt), 240.0)
            log.warning(
                "[%s/%s] transient Codex error (attempt %d/%d): %s; retrying in %.0fs",
                stage,
                artifact_name,
                attempt + 1,
                transient_retries + 1,
                str(e)[:160],
                delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def _run_agent_once(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None,
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int,
) -> AgentResult:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_name}.jsonl"
    cwd.mkdir(parents=True, exist_ok=True)

    system_prompt = _build_system_prompt(prompt_file, schema_file)
    schema = _load_codex_output_schema(schema_file)
    initial_prompt = json.dumps(user_input, ensure_ascii=False)
    sandbox_name = _sandbox_name_for_tools(allowed_tools)
    sdk = _load_codex_sdk()
    started_at = time.monotonic()

    _log_progress(
        event="start",
        stage=stage,
        artifact_name=artifact_name,
        model=model,
        elapsed_s=0,
    )

    last_text = ""
    last_result_msg: dict[str, Any] = {}
    repair_used = False

    with artifact_path.open("w") as art:
        _write_artifact(
            art,
            {
                "kind": "meta",
                "provider": "codex",
                "stage": stage,
                "model": model,
                "sandbox": sandbox_name,
                "started_at": time.time(),
            },
        )
        _write_artifact(art, {"kind": "user", "text": initial_prompt[:50000]})

        try:
            codex_config = sdk.CodexConfig(
                config_overrides=_config_overrides_for_add_dirs(add_dirs or [])
            )
            async with sdk.AsyncCodex(config=codex_config) as codex:
                thread = await codex.thread_start(
                    developer_instructions=system_prompt,
                    model=model,
                    cwd=str(cwd),
                    sandbox=_sdk_sandbox(sdk, sandbox_name),
                    approval_mode=sdk.ApprovalMode.auto_review,
                    ephemeral=True,
                )
                _write_artifact(
                    art,
                    {
                        "kind": "thread",
                        "thread_id": getattr(thread, "id", None),
                        "cwd": str(cwd),
                        "add_dirs": [str(p) for p in (add_dirs or [])],
                    },
                )

                result = await _run_turn_with_heartbeat(
                    thread,
                    initial_prompt,
                    output_schema=schema,
                    sandbox=_sdk_sandbox(sdk, sandbox_name),
                    stage=stage,
                    artifact_name=artifact_name,
                    model=model,
                    started_at=started_at,
                )
                last_text, last_result_msg = _result_to_text_and_dict(result, thread)
                _write_artifact(art, {"kind": "turn_result", **last_result_msg})

                errors = _validate(last_text, schema_file)
                attempts = 0
                while errors and attempts < repair_attempts:
                    attempts += 1
                    repair_used = True
                    _log_progress(
                        event=f"repair attempt {attempts}",
                        stage=stage,
                        artifact_name=artifact_name,
                        model=model,
                        elapsed_s=time.monotonic() - started_at,
                    )
                    repair_prompt = _build_repair_prompt(last_text, errors, schema_file)
                    _write_artifact(
                        art,
                        {"kind": "repair_request", "text": repair_prompt[:50000]},
                    )
                    result = await _run_turn_with_heartbeat(
                        thread,
                        repair_prompt,
                        output_schema=schema,
                        sandbox=_sdk_sandbox(sdk, sandbox_name),
                        stage=stage,
                        artifact_name=artifact_name,
                        model=model,
                        started_at=started_at,
                    )
                    last_text, last_result_msg = _result_to_text_and_dict(result, thread)
                    _write_artifact(art, {"kind": "repair_result", **last_result_msg})
                    errors = _validate(last_text, schema_file)

                if errors:
                    _write_artifact(art, {"kind": "schema_errors", "errors": errors})
                    raise AgentRunError(
                        f"[{stage}/{artifact_name}] schema validation failed after "
                        f"{repair_attempts} repair attempts: {errors[:5]}"
                    )

                payload = _extract_payload_for_schema(last_text, schema_file)
                _write_artifact(art, {"kind": "final_payload", "payload": payload})
        except AgentRunError:
            raise
        except Exception as e:
            label, exc_cls = _classify_api_error(str(e))
            if exc_cls is QuotaExhaustedError:
                raise QuotaExhaustedError(f"[{stage}/{artifact_name}] {label}: {e}") from e
            if exc_cls is TransientAgentError:
                raise TransientAgentError(f"[{stage}/{artifact_name}] {label}: {e}") from e
            raise

    usage = last_result_msg.get("usage") or {}
    _log_progress(
        event="complete",
        stage=stage,
        artifact_name=artifact_name,
        model=model,
        elapsed_s=time.monotonic() - started_at,
    )
    return AgentResult(
        payload=payload,
        cost_usd=None,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=None,
        num_turns=last_result_msg.get("num_turns"),
        duration_ms=last_result_msg.get("duration_ms"),
        session_id=last_result_msg.get("session_id"),
        artifact_path=artifact_path,
        repair_used=repair_used,
        raw_result_message=last_result_msg,
    )


def _load_codex_sdk() -> Any:
    try:
        return importlib.import_module("openai_codex")
    except ModuleNotFoundError as e:
        raise AgentSetupError(
            "Missing Python package `openai-codex`. Install project "
            "dependencies with `pip install -e .`."
        ) from e


def _load_codex_output_schema(schema_file: Path) -> dict:
    schema = _load_schema_with_inlined_refs(schema_file)
    return _to_codex_output_schema(schema)


def _load_schema_with_inlined_refs(schema_file: Path) -> dict:
    return _inline_external_refs(
        json.loads(schema_file.read_text()),
        schemas_dir=schema_file.parent,
        ref_stack=(schema_file.name,),
    )


def _inline_external_refs(value: Any, *, schemas_dir: Path, ref_stack: tuple[str, ...]) -> Any:
    if isinstance(value, list):
        return [
            _inline_external_refs(item, schemas_dir=schemas_dir, ref_stack=ref_stack)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    ref = value.get("$ref")
    if isinstance(ref, str) and not ref.startswith("#"):
        if ref in ref_stack:
            chain = " -> ".join([*ref_stack, ref])
            raise AgentSetupError(f"Cyclic schema reference: {chain}")
        target = schemas_dir / ref
        if not target.exists():
            raise AgentSetupError(f"Missing schema reference `{ref}` from {schemas_dir}")
        target_schema = json.loads(target.read_text())
        return _inline_external_refs(
            target_schema,
            schemas_dir=schemas_dir,
            ref_stack=(*ref_stack, ref),
        )

    return {
        key: _inline_external_refs(child, schemas_dir=schemas_dir, ref_stack=ref_stack)
        for key, child in value.items()
    }


def _to_codex_output_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_to_codex_output_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    required = set(value.get("required") or [])
    converted = {
        key: _to_codex_output_schema(child)
        for key, child in value.items()
        if key != "$schema"
    }

    if _is_object_schema(converted):
        properties = converted.get("properties") or {}
        for name, property_schema in list(properties.items()):
            if name not in required:
                properties[name] = _make_nullable_schema(property_schema)
        converted["additionalProperties"] = False
        converted["required"] = list(properties)

    return converted


def _make_nullable_schema(schema: Any) -> Any:
    if not isinstance(schema, dict) or _schema_allows_null(schema):
        return schema

    nullable = dict(schema)
    schema_type = nullable.get("type")
    if isinstance(schema_type, str):
        nullable["type"] = [schema_type, "null"]
    elif isinstance(schema_type, list):
        nullable["type"] = [*schema_type, "null"]
    else:
        nullable["anyOf"] = [schema, {"type": "null"}]

    enum = nullable.get("enum")
    if isinstance(enum, list) and None not in enum:
        nullable["enum"] = [*enum, None]

    return nullable


def _schema_allows_null(schema: dict) -> bool:
    schema_type = schema.get("type")
    return schema_type == "null" or (
        isinstance(schema_type, list) and "null" in schema_type
    )


def _is_object_schema(schema: dict) -> bool:
    schema_type = schema.get("type")
    return (
        schema_type == "object"
        or (isinstance(schema_type, list) and "object" in schema_type)
        or "properties" in schema
    )


def _extract_payload_for_schema(text: str, schema_file: Path) -> Any:
    payload = extract_json(text)
    schema = _load_schema_with_inlined_refs(schema_file)
    return _strip_null_optional_fields(payload, schema)


def _strip_null_optional_fields(payload: Any, schema: Any) -> Any:
    if isinstance(payload, list):
        item_schema = schema.get("items") if isinstance(schema, dict) else None
        return [_strip_null_optional_fields(item, item_schema) for item in payload]
    if not isinstance(payload, dict) or not isinstance(schema, dict):
        return payload
    if not _is_object_schema(schema):
        return payload

    required = set(schema.get("required") or [])
    properties = schema.get("properties") or {}
    normalized = {}
    for key, value in payload.items():
        property_schema = properties.get(key)
        if value is None and key in properties and key not in required:
            continue
        normalized[key] = _strip_null_optional_fields(value, property_schema)
    return normalized


def _build_system_prompt(prompt_file: Path, schema_file: Path) -> str:
    system_prompt = prompt_file.read_text()
    schema_text = schema_file.read_text()
    return (
        system_prompt
        + "\n\n# Output schema\n\n"
        + "Your output MUST validate against this JSON Schema. "
        + "Pay attention to nested objects, required fields, and "
        + "`additionalProperties: false`.\n\n"
        + f"```json\n{schema_text}\n```\n"
    )


def _sandbox_name_for_tools(allowed_tools: list[str]) -> str:
    return "workspace_write" if "Bash" in allowed_tools else "read_only"


def _sdk_sandbox(sdk: Any, sandbox_name: str) -> Any:
    return getattr(sdk.Sandbox, sandbox_name)


def _config_overrides_for_add_dirs(add_dirs: list[Path]) -> tuple[str, ...]:
    roots = [str(p.resolve()) for p in add_dirs]
    if not roots:
        return ()
    encoded = json.dumps(roots)
    return (f"sandbox_workspace_write.writable_roots={encoded}",)


async def _run_turn_with_heartbeat(
    thread: Any,
    prompt: str,
    *,
    output_schema: dict,
    sandbox: Any,
    stage: str,
    artifact_name: str,
    model: str,
    started_at: float,
) -> Any:
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            stop,
            stage=stage,
            artifact_name=artifact_name,
            model=model,
            started_at=started_at,
        )
    )
    try:
        return await thread.run(
            prompt,
            output_schema=output_schema,
            sandbox=sandbox,
        )
    finally:
        stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _heartbeat_loop(
    stop: asyncio.Event,
    *,
    stage: str,
    artifact_name: str,
    model: str,
    started_at: float,
    interval_s: float = 30.0,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            _log_progress(
                event="heartbeat",
                stage=stage,
                artifact_name=artifact_name,
                model=model,
                elapsed_s=time.monotonic() - started_at,
            )


def _log_progress(
    *,
    event: str,
    stage: str,
    artifact_name: str,
    model: str,
    elapsed_s: float,
) -> None:
    log.info(
        "[%s/%s] %s model=%s elapsed=%ds",
        stage,
        artifact_name,
        event,
        model,
        int(elapsed_s),
    )


def _result_to_text_and_dict(result: Any, thread: Any) -> tuple[str, dict[str, Any]]:
    text = result.final_response or ""
    usage = _usage_to_dict(getattr(result, "usage", None))
    msg = {
        "subtype": "success",
        "is_error": False,
        "duration_ms": getattr(result, "duration_ms", None),
        "duration_api_ms": None,
        "num_turns": 1,
        "session_id": getattr(thread, "id", None),
        "stop_reason": getattr(result, "status", None),
        "total_cost_usd": None,
        "usage": usage,
        "result": text,
        "model_usage": None,
        "items": [_serialize_any(item) for item in getattr(result, "items", [])],
    }
    return text, msg


def _usage_to_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    total = getattr(usage, "total", None) or usage
    return {
        "input_tokens": getattr(total, "input_tokens", None),
        "output_tokens": getattr(total, "output_tokens", None),
        "cache_read_input_tokens": getattr(total, "cached_input_tokens", None),
        "reasoning_output_tokens": getattr(total, "reasoning_output_tokens", None),
        "total_tokens": getattr(total, "total_tokens", None),
    }


def _validate(text: str, schema_file: Path) -> list[str]:
    try:
        payload = _extract_payload_for_schema(text, schema_file)
    except ValueError as e:
        return [f"json_extract: {e}"]
    return validate_schema(payload, schema_file)


def _build_repair_prompt(prev_output: str, errors: list[str], schema_file: Path) -> str:
    err_block = "\n".join(f"- {e}" for e in errors[:20])
    return (
        "Your previous output failed schema validation against "
        f"`{schema_file.name}`. Errors:\n"
        f"{err_block}\n\n"
        "Re-emit the same response, fixing ONLY these errors. Output a "
        "single JSON object — no prose, no markdown fence."
    )


def _write_artifact(fp, obj: Any) -> None:
    fp.write(json.dumps(obj, default=_json_fallback, ensure_ascii=False) + "\n")
    fp.flush()


def _serialize_any(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_serialize_any(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize_any(v) for k, v in value.items()}
    return repr(value)


def _json_fallback(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    return _serialize_any(o)
