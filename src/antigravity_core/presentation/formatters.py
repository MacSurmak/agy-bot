from bisect import bisect_left
from pathlib import Path

from telethon.extensions import markdown
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityBlockquote, TypeMessageEntity

from antigravity_core.config.dynamic_settings import DynamicSettings
from antigravity_core.config.static_config import LimitsStaticConfig
from antigravity_core.domain.enums import ExecutionMode, StepState, TurnStatus
from antigravity_core.domain.models import (
    ForumTopic,
    GoogleAccount,
    RemoteHost,
    ScheduledTask,
    SubagentState,
    ToolCallSummary,
    TopicSession,
    TurnProgressEvent,
    TurnResult,
    WorkerTopicSnapshot,
)
from antigravity_core.lexicon.schema import UiLexicon

QUOTE_START: str = "\x02"
QUOTE_END: str = "\x03"


def parse_markdown_with_collapsible_quotes(
    raw_text: str,
) -> tuple[str, list[TypeMessageEntity]]:
    """Parses Markdown text and converts QUOTE_START..QUOTE_END blocks into collapsible blockquotes."""
    parsed_text, raw_entities = markdown.parse(raw_text)
    s_text = add_surrogate(parsed_text)

    marker_positions: list[int] = [
        idx for idx, ch in enumerate(s_text) if ch in (QUOTE_START, QUOTE_END)
    ]
    if not marker_positions:
        return parsed_text, list(raw_entities)

    def _project_offset(pos: int) -> int:
        return pos - bisect_left(marker_positions, pos)

    quote_spans: list[tuple[int, int]] = []
    open_start: int | None = None
    for idx, ch in enumerate(s_text):
        if ch == QUOTE_START and open_start is None:
            open_start = idx + 1
        elif ch == QUOTE_END and open_start is not None:
            quote_spans.append((open_start, idx))
            open_start = None

    adjusted_entities: list[TypeMessageEntity] = []
    for ent in raw_entities:
        orig_start = ent.offset
        orig_end = ent.offset + ent.length
        new_start = _project_offset(orig_start)
        new_end = _project_offset(orig_end)
        if new_end > new_start:
            ent.offset = new_start
            ent.length = new_end - new_start
            adjusted_entities.append(ent)

    for q_start, q_end in quote_spans:
        c_start = _project_offset(q_start)
        c_end = _project_offset(q_end)
        if c_end > c_start:
            adjusted_entities.append(
                MessageEntityBlockquote(
                    offset=c_start,
                    length=c_end - c_start,
                    collapsed=True,
                )
            )

    clean_s_text = s_text.replace(QUOTE_START, "").replace(QUOTE_END, "")
    return del_surrogate(clean_s_text), adjusted_entities


def render_topic_header_card(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    topic: ForumTopic,
    account: GoogleAccount,
    active_session: TopicSession | None,
    is_worker_warm: bool,
    worker_ttl_sec: int,
) -> str:
    short_len = limits.uuid_short_chars
    if active_session is not None:
        conv_short = str(active_session.conversation_id)[:short_len]
        tok_k = active_session.cumulative_tokens / 1000.0
        session_str = lex.topic_card_session_with_stats.format(
            conv_short=conv_short,
            turns_count=active_session.turns_count,
            tok_k=tok_k,
        )
    elif topic.active_conversation_id is not None:
        session_str = lex.topic_card_session_uuid_only.format(
            conv_short=str(topic.active_conversation_id)[:short_len]
        )
    else:
        session_str = lex.topic_card_session_clean

    effort_str = ""
    mode_str = (
        lex.topic_card_mode_full_access
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.topic_card_mode_read_only
    )

    ttl_min = max(1, worker_ttl_sec // 60)
    worker_str = (
        lex.topic_card_worker_warm.format(ttl_min=ttl_min)
        if is_worker_warm
        else lex.topic_card_worker_cold.format(ttl_min=ttl_min)
    )

    return lex.topic_card_template.format(
        title=topic.title,
        session_str=session_str,
        workspace_path=topic.workspace_path,
        model_slug=topic.model_slug,
        effort_str=effort_str,
        account_slug=account.slug,
        email_label=account.email_label,
        mode_str=mode_str,
        worker_str=worker_str,
    )


def render_new_session_divider(lex: UiLexicon, topic: ForumTopic) -> str:
    mode_label = (
        lex.new_session_mode_full_access
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.new_session_mode_read_only
    )
    return lex.new_session_divider_template.format(
        workspace_path=topic.workspace_path,
        model_slug=topic.model_slug,
        mode_label=mode_label,
    )


def _format_tool_blockquote(lex: UiLexicon, tool: ToolCallSummary) -> str:
    icon = (
        lex.live_progress_tool_done_icon
        if tool.state == StepState.DONE
        else lex.live_progress_tool_active_icon
    )
    dur = (
        lex.live_progress_tool_duration_suffix.format(duration=tool.duration_seconds)
        if tool.duration_seconds is not None
        else ""
    )
    safe_hint = tool.target_hint.replace("`", "'") if tool.target_hint else None
    hint = (
        lex.live_progress_tool_hint_suffix.format(hint=safe_hint)
        if safe_hint
        else ""
    )
    header = lex.live_progress_tool_item.format(
        icon=icon,
        step_index=tool.step_index,
        tool_name=tool.tool_name,
        dur=dur,
        hint=hint,
    )
    inner_lines: list[str] = [header]
    if tool.parameters_text:
        clean_params = tool.parameters_text.replace("`", "'")
        inner_lines.append(f"{lex.trace_tool_params_label}\n{clean_params}")
    if tool.output_text:
        clean_out = tool.output_text.replace("`", "'")
        inner_lines.append(f"{lex.trace_tool_output_label}\n{clean_out}")

    return f"{QUOTE_START}" + "\n".join(inner_lines) + f"{QUOTE_END}"


def _build_trace_blocks(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    thinking_text: str | None,
    subagents: tuple[SubagentState, ...],
    tools: tuple[ToolCallSummary, ...],
) -> list[str]:
    blocks: list[str] = []
    short_len = limits.uuid_short_chars

    if thinking_text and thinking_text.strip():
        clean_thinking = thinking_text.strip().replace("`", "'")
        blocks.append(
            f"{QUOTE_START}{lex.trace_thinking_header}\n{clean_thinking}{QUOTE_END}"
        )

    if subagents:
        sub_lines = [lex.live_progress_subagents_header]
        for sub in subagents:
            sub_lines.append(
                lex.live_progress_subagent_item.format(
                    role=sub.role,
                    type_name=sub.type_name,
                    conv_short=sub.conversation_id[:short_len],
                )
            )
        blocks.append(f"{QUOTE_START}" + "\n".join(sub_lines) + f"{QUOTE_END}")

    for tool in tools:
        blocks.append(_format_tool_blockquote(lex, tool))

    return blocks


def render_live_progress(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    event: TurnProgressEvent,
    elapsed_sec: float,
    model_slug: str,
    account_slug: str,
) -> str:
    header = lex.live_progress_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=elapsed_sec,
    )
    blocks = _build_trace_blocks(
        lex=lex,
        limits=limits,
        thinking_text=event.thinking_text,
        subagents=event.active_subagents,
        tools=event.active_tools,
    )
    if event.text_accumulated.strip():
        draft_lim = limits.draft_preview_max_chars
        preview = event.text_accumulated.strip()[-draft_lim:].replace("`", "'")
        blocks.append(
            f"{QUOTE_START}{lex.live_progress_draft.format(preview=preview)}{QUOTE_END}"
        )

    return "\n\n".join([header, *blocks])


def render_final_execution_trace_chunks(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    result: TurnResult,
    model_slug: str,
    account_slug: str,
    max_chars: int,
) -> list[str]:
    if not result.thinking_text and not result.tool_calls and not result.subagents:
        return []

    header = lex.trace_completed_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=result.turn_delta_duration_sec,
        steps_count=len(result.tool_calls),
    )
    blocks = _build_trace_blocks(
        lex=lex,
        limits=limits,
        thinking_text=result.thinking_text,
        subagents=result.subagents,
        tools=result.tool_calls,
    )

    chunks: list[str] = []
    current = header

    for block in blocks:
        if len(block) > max_chars - 200:
            # Split an oversized single block safely while keeping QUOTE_START..QUOTE_END balanced
            inner = block.removeprefix(QUOTE_START).removesuffix(QUOTE_END)
            part_limit = max_chars - 250
            for offset in range(0, len(inner), part_limit):
                sub_block = f"{QUOTE_START}{inner[offset : offset + part_limit]}{QUOTE_END}"
                if len(current) + 2 + len(sub_block) > max_chars and current != header:
                    chunks.append(current)
                    current = sub_block
                else:
                    current = f"{current}\n\n{sub_block}"
            continue

        if len(current) + 2 + len(block) > max_chars:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"

    if current:
        chunks.append(current)
    return chunks


def render_turn_footer(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    result: TurnResult,
    model_slug: str,
    account_slug: str,
) -> str:
    tok_k = result.turn_delta_tokens / 1000.0
    cum_k = result.cumulative_usage.total_tokens / 1000.0
    cache_k = result.cumulative_usage.cache_read_tokens / 1000.0

    footer = lex.turn_footer_stats.format(
        duration=result.turn_delta_duration_sec,
        tok_k=tok_k,
        cum_k=cum_k,
        cache_k=cache_k,
        model_slug=model_slug,
        account_slug=account_slug,
    )
    if result.account_switched_from and result.account_switched_to:
        footer += lex.turn_footer_failover.format(
            from_slug=result.account_switched_from,
            to_slug=result.account_switched_to,
        )
    if result.status != TurnStatus.SUCCESS and result.error_message:
        err_lim = limits.error_summary_max_chars
        footer += lex.turn_footer_error_status.format(
            status=result.status.value,
            error=result.error_message[:err_lim],
        )
    return footer


def render_control_panel_dashboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    accounts: list[GoogleAccount],
    hosts: list[RemoteHost],
    mount_statuses: dict[str, bool],
    pool_snapshots: list[WorkerTopicSnapshot],
    tasks: list[ScheduledTask],
    settings: DynamicSettings,
) -> str:
    warm_count = sum(1 for s in pool_snapshots if s.is_warm)
    running_count = sum(1 for s in pool_snapshots if s.is_running_turn)

    lines: list[str] = [
        lex.dashboard_header.format(warm_count=warm_count, running_count=running_count)
    ]
    for snap in pool_snapshots:
        if snap.is_warm:
            state_badge = (
                lex.dashboard_worker_running_badge
                if snap.is_running_turn
                else lex.dashboard_worker_warm_badge
            )
            lines.append(
                lex.dashboard_worker_item.format(
                    topic_id=snap.topic_id,
                    state_badge=state_badge,
                    model_slug=snap.model_slug,
                    account_slug=snap.account_slug,
                )
            )

    lines.append(lex.dashboard_accounts_header)
    if not accounts:
        lines.append(lex.dashboard_accounts_empty)
    for acc in accounts:
        default_badge = lex.dashboard_account_default_badge if acc.is_default else ""
        status_badge = (
            lex.dashboard_account_enabled_icon
            if acc.is_enabled
            else lex.dashboard_account_disabled_icon
        )
        quota_str = lex.dashboard_account_quota_unprobed
        if acc.last_quota_json is not None:
            min_pct = acc.last_quota_json.get("min_remaining_pct", "?")
            quota_str = lex.dashboard_account_quota_probed.format(min_pct=min_pct)
        backup_badge = (
            lex.dashboard_account_backup_ok
            if acc.encrypted_token_backup
            else lex.dashboard_account_backup_missing
        )
        lines.append(
            lex.dashboard_account_item.format(
                status_badge=status_badge,
                slug=acc.slug,
                email_label=acc.email_label,
                default_badge=default_badge,
                quota_str=quota_str,
                backup_badge=backup_badge,
            )
        )

    lines.append(lex.dashboard_hosts_header)
    if not hosts:
        lines.append(lex.dashboard_hosts_empty)
    for h in hosts:
        mounted = mount_statuses.get(h.alias, False)
        m_badge = (
            lex.dashboard_host_mounted_badge
            if mounted
            else lex.dashboard_host_unmounted_badge
        )
        lines.append(
            lex.dashboard_host_item.format(
                alias=h.alias,
                ssh_user=h.ssh_user,
                hostname_or_ip=h.hostname_or_ip,
                ssh_port=h.ssh_port,
                local_mount_path=h.local_mount_path,
                m_badge=m_badge,
            )
        )

    short_len = limits.uuid_short_chars
    tasks_lim = limits.dashboard_tasks_display_limit
    lines.append(lex.dashboard_tasks_header.format(count=len(tasks)))
    for t in tasks[:tasks_lim]:
        lines.append(
            lex.dashboard_task_item.format(
                short_id=str(t.id)[:short_len],
                topic_id=t.topic_id,
                schedule_kind=t.schedule_kind.value,
                trigger_spec=t.trigger_spec,
                notify_policy=t.notify_policy.value,
            )
        )

    lines.append(
        lex.dashboard_settings_summary.format(
            ui_language=settings.ui_language,
            default_model=settings.default_model,
            default_effort=settings.default_effort,
            default_execution_mode=settings.default_execution_mode.value,
            worker_idle_ttl_sec=settings.worker_idle_ttl_sec,
            debounce_window_sec=settings.debounce_window_sec,
            quota_auto_switch_threshold_pct=settings.quota_auto_switch_threshold_pct,
        )
    )
    return "\n".join(lines)


def split_message_chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_idx = remaining.rfind("\n", 0, max_chars)
        if split_idx <= max_chars // 2:
            split_idx = max_chars
        chunk = remaining[:split_idx].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip("\n")

    if remaining:
        chunks.append(remaining)
    return chunks


def list_workspace_directories(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    workspaces_root: Path,
    remotes_root: Path,
) -> list[tuple[str, str]]:
    """Returns pairs of (label, absolute_path) for quick workspace selection."""
    choices: list[tuple[str, str]] = [
        (lex.workspace_dir_root_label, str(workspaces_root.resolve())),
    ]
    if workspaces_root.is_dir():
        for child in sorted(workspaces_root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                choices.append(
                    (
                        lex.workspace_dir_local_label.format(name=child.name),
                        str(child.resolve()),
                    )
                )

    if remotes_root.is_dir():
        for host_dir in sorted(remotes_root.iterdir()):
            if host_dir.is_dir():
                choices.append(
                    (
                        lex.workspace_dir_remote_label.format(name=host_dir.name),
                        str(host_dir.resolve()),
                    )
                )
    return choices[: limits.workspace_dirs_display_limit]
