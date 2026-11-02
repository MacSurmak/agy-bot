import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from antigravity_core.domain.enums import StepState, TurnStatus
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import (
    SubagentState,
    ToolCallSummary,
    TurnResult,
    TurnUsage,
)


@dataclass(frozen=True, slots=True)
class ParsedInitEvent:
    conversation_id: UUID
    cwd: str
    model: str
    permission_mode: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedStepUpdateEvent:
    conversation_id: UUID
    step_index: int
    state: StepState
    step_type: str
    tool_summary: ToolCallSummary | None
    subagents: tuple[SubagentState, ...]
    thinking_delta: str | None
    text_delta: str | None


def serialize_user_prompt(prompt_text: str) -> bytes:
    if not prompt_text.strip():
        raise AgyProcessError("Cannot send an empty user prompt to agy stream-json stdin.")
    if prompt_text.lstrip().startswith(("/usage", "/quota", "/model", "/credits", "/changelog")):
        raise AgyProcessError(
            f"CLI-handled slash command '{prompt_text.split()[0]}' is forbidden in stream-json stdin."
        )
    payload = {
        "event": "user",
        "message": {
            "content": prompt_text,
        },
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def parse_stream_json_line(
    raw_line: str,
    prev_cumulative_tokens: int,
    prev_cumulative_duration_sec: float,
) -> ParsedInitEvent | ParsedStepUpdateEvent | TurnResult:
    stripped = raw_line.strip()
    if not stripped:
        raise AgyProcessError("Received empty line from agy stream-json stdout.")

    try:
        data: Any = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AgyProcessError(
            f"Malformed JSON line from agy stdout: {exc} | Line: {stripped[:300]}"
        ) from exc

    if not isinstance(data, dict):
        raise AgyProcessError(f"Expected JSON object from agy stdout, got {type(data).__name__}.")

    event_kind = data.get("event")
    if event_kind == "init":
        return _parse_init_event(data)
    if event_kind == "step_update":
        return _parse_step_update_event(data)
    if event_kind == "result":
        return _parse_result_event(
            data,
            prev_cumulative_tokens=prev_cumulative_tokens,
            prev_cumulative_duration_sec=prev_cumulative_duration_sec,
        )

    raise AgyProcessError(f"Unknown event type in agy stream-json output: '{event_kind}'.")


def _parse_uuid(raw_val: Any, context_label: str) -> UUID:
    if not isinstance(raw_val, str) or not raw_val.strip():
        raise AgyProcessError(f"Missing or invalid conversation_id in {context_label}: {raw_val!r}")
    try:
        return UUID(raw_val.strip())
    except ValueError as exc:
        raise AgyProcessError(
            f"Invalid UUID format for conversation_id in {context_label}: {raw_val!r}"
        ) from exc


def _parse_init_event(data: dict[str, Any]) -> ParsedInitEvent:
    conv_id = _parse_uuid(data.get("conversation_id"), "init event")
    init_body = data.get("init")
    if not isinstance(init_body, dict):
        raise AgyProcessError("Missing 'init' object inside init event.")

    raw_tools = init_body.get("tools")
    tools_tuple: tuple[str, ...] = (
        tuple(str(t) for t in raw_tools) if isinstance(raw_tools, list) else ()
    )
    return ParsedInitEvent(
        conversation_id=conv_id,
        cwd=str(init_body.get("cwd", "")),
        model=str(init_body.get("model", "")),
        permission_mode=str(init_body.get("permission_mode", "")),
        tools=tools_tuple,
    )


def _parse_step_update_event(data: dict[str, Any]) -> ParsedStepUpdateEvent:
    body = data.get("step_update")
    if not isinstance(body, dict):
        raise AgyProcessError("Missing 'step_update' object inside step_update event.")

    conv_id = _parse_uuid(body.get("conversation_id"), "step_update event")
    step_index = int(body.get("step_index", 0))
    raw_state = str(body.get("state", "ACTIVE")).upper()
    state = StepState.DONE if raw_state == "DONE" else StepState.ACTIVE
    step_type = str(body.get("step_type", ""))

    tool_summary: ToolCallSummary | None = None
    if step_type == "tool":
        tool_name = str(body.get("tool_name", "unknown_tool"))
        raw_dur = body.get("duration_seconds")
        duration = float(raw_dur) if isinstance(raw_dur, int | float) else None
        target_hint: str | None = None
        tool_info = body.get("tool_info")
        if isinstance(tool_info, dict):
            params = tool_info.get("parameters")
            if isinstance(params, dict):
                for key in (
                    "TargetFile",
                    "AbsolutePath",
                    "DirectoryPath",
                    "SearchPath",
                    "CommandLine",
                    "file_path",
                    "alias",
                ):
                    if key in params and params[key] is not None:
                        target_hint = str(params[key])[:120]
                        break
        tool_summary = ToolCallSummary(
            step_index=step_index,
            tool_name=tool_name,
            state=state,
            duration_seconds=duration,
            target_hint=target_hint,
        )

    subagents_list: list[SubagentState] = []
    subagent_info = body.get("subagent_info")
    if isinstance(subagent_info, dict):
        raw_subagents = subagent_info.get("subagents")
        if isinstance(raw_subagents, list):
            for item in raw_subagents:
                if isinstance(item, dict):
                    raw_uris = item.get("workspace_uris")
                    uris = tuple(str(u) for u in raw_uris) if isinstance(raw_uris, list) else ()
                    subagents_list.append(
                        SubagentState(
                            type_name=str(item.get("type_name", "subagent")),
                            role=str(item.get("role", "Subagent")),
                            conversation_id=str(item.get("conversation_id", "")),
                            log_uri=str(item.get("log_uri", "")),
                            workspace_uris=uris,
                        )
                    )

    thinking_delta = (
        str(body["thinking_delta"])
        if isinstance(body.get("thinking_delta"), str) and body["thinking_delta"]
        else None
    )
    text_delta = (
        str(body["text_delta"])
        if isinstance(body.get("text_delta"), str) and body["text_delta"]
        else None
    )

    return ParsedStepUpdateEvent(
        conversation_id=conv_id,
        step_index=step_index,
        state=state,
        step_type=step_type,
        tool_summary=tool_summary,
        subagents=tuple(subagents_list),
        thinking_delta=thinking_delta,
        text_delta=text_delta,
    )


def _parse_result_event(
    data: dict[str, Any],
    prev_cumulative_tokens: int,
    prev_cumulative_duration_sec: float,
) -> TurnResult:
    body = data.get("result")
    if not isinstance(body, dict):
        raise AgyProcessError("Missing 'result' object inside result event.")

    conv_id = _parse_uuid(body.get("conversation_id"), "result event")
    raw_status = str(body.get("status", "ERROR")).upper()
    try:
        status = TurnStatus(raw_status)
    except ValueError:
        status = TurnStatus.ERROR

    response_text = str(body.get("response", ""))
    error_val = body.get("error")
    error_message = str(error_val) if error_val is not None else None

    cum_duration = float(body.get("duration_seconds", 0.0))
    cum_turns = int(body.get("num_turns", 1))

    usage_raw = body.get("usage")
    if isinstance(usage_raw, dict):
        usage = TurnUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
            thinking_tokens=int(usage_raw.get("thinking_tokens", 0)),
            cache_read_tokens=int(usage_raw.get("cache_read_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
        )
    else:
        usage = TurnUsage(
            input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            cache_read_tokens=0,
            total_tokens=0,
        )

    # Reference #2 Section 2.3: Subtract previous cumulative process totals to get exact single-turn delta
    delta_tokens = max(0, usage.total_tokens - prev_cumulative_tokens)
    delta_duration = max(0.0, cum_duration - prev_cumulative_duration_sec)

    return TurnResult(
        conversation_id=conv_id,
        status=status,
        response_text=response_text,
        error_message=error_message,
        cumulative_duration_sec=cum_duration,
        cumulative_turns=cum_turns,
        cumulative_usage=usage,
        turn_delta_duration_sec=delta_duration,
        turn_delta_tokens=delta_tokens,
        account_switched_from=None,
        account_switched_to=None,
    )
