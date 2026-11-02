from pathlib import Path
import re

from telethon.helpers import add_surrogate
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    TypeMessageEntity,
)

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

_BINARY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]+")


def _sanitize_trace_text(raw: str, max_len: int) -> str:
    cleaned = _BINARY_CONTROL_RE.sub(" ", raw).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 18].rstrip() + "\n... [сокращено]"


def _utf16_len(text: str) -> int:
    return len(add_surrogate(text))


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


def _format_tool_plain_block(
    lex: UiLexicon, tool: ToolCallSummary, detail_budget: int
) -> str:
    icon = (
        lex.live_progress_tool_done_icon
        if tool.state == StepState.DONE
        else lex.live_progress_tool_active_icon
    )
    dur = (
        f" ({tool.duration_seconds:.2f}с)"
        if tool.duration_seconds is not None
        else ""
    )
    hint_str = ""
    if tool.target_hint:
        clean_h = _sanitize_trace_text(tool.target_hint.replace("\n", " "), 80)
        hint_str = f" → {clean_h}"

    header_line = f"{icon} #{tool.step_index} {tool.tool_name}{dur}{hint_str}"
    lines: list[str] = [header_line]

    half_budget = max(40, detail_budget // 2)
    if tool.parameters_text:
        p_clean = _sanitize_trace_text(tool.parameters_text, half_budget)
        lines.append(f"{lex.trace_tool_params_label}\n{p_clean}")
    if tool.output_text:
        o_clean = _sanitize_trace_text(tool.output_text, half_budget)
        lines.append(f"{lex.trace_tool_output_label}\n{o_clean}")

    return "\n".join(lines)


def _assemble_collapsible_trace_message(
    header_line: str,
    quote_blocks: list[str],
) -> tuple[str, list[TypeMessageEntity]]:
    """Builds plain text and exact UTF-16 MessageEntityBlockquote(collapsed=True) entities without markdown parsing."""
    clean_header = header_line.replace("**", "").replace("`", "")
    entities: list[TypeMessageEntity] = [
        MessageEntityBold(offset=0, length=_utf16_len(clean_header))
    ]
    parts: list[str] = [clean_header]
    current_utf16_offset = _utf16_len(clean_header)

    for block in quote_blocks:
        sep = "\n\n"
        parts.append(sep)
        current_utf16_offset += _utf16_len(sep)

        b_len = _utf16_len(block)
        if b_len > 0:
            entities.append(
                MessageEntityBlockquote(
                    offset=current_utf16_offset,
                    length=b_len,
                    collapsed=True,
                )
            )
            parts.append(block)
            current_utf16_offset += b_len

    return "".join(parts), entities


def _build_budgeted_quote_blocks(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    thinking_text: str | None,
    subagents: tuple[SubagentState, ...],
    tools: tuple[ToolCallSummary, ...],
    draft_text: str | None,
    total_char_budget: int = 3600,
) -> list[str]:
    quote_blocks: list[str] = []
    remaining_budget = total_char_budget

    if thinking_text and thinking_text.strip():
        t_budget = min(600, max(150, remaining_budget // 4))
        t_header = lex.trace_thinking_header.replace("**", "")
        t_body = _sanitize_trace_text(thinking_text, t_budget)
        block_str = f"{t_header}\n{t_body}"
        quote_blocks.append(block_str)
        remaining_budget -= len(block_str) + 4

    if subagents:
        short_len = limits.uuid_short_chars
        sub_lines = [lex.live_progress_subagents_header.replace("**", "")]
        for sub in subagents:
            sub_lines.append(
                f"• {sub.role} ({sub.type_name} — {sub.conversation_id[:short_len]})"
            )
        sub_block = "\n".join(sub_lines)
        quote_blocks.append(sub_block)
        remaining_budget -= len(sub_block) + 4

    if draft_text and draft_text.strip():
        d_budget = min(350, max(100, remaining_budget // 5))
        d_body = _sanitize_trace_text(draft_text.strip()[-d_budget:], d_budget)
        d_block = lex.live_progress_draft.replace("**", "").format(preview=d_body)
        remaining_budget -= len(d_block) + 4
    else:
        d_block = None

    if tools:
        # Telegram allows max ~90 entities per message; if >35 tools, show the most recent 35 in full detail
        visible_tools = tools[-35:] if len(tools) > 35 else tools
        per_tool_budget = max(80, (remaining_budget // len(visible_tools)) - 70)
        for tool in visible_tools:
            quote_blocks.append(_format_tool_plain_block(lex, tool, per_tool_budget))

    if d_block is not None:
        quote_blocks.append(d_block)

    return quote_blocks


def build_live_progress_message(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    event: TurnProgressEvent,
    elapsed_sec: float,
    model_slug: str,
    account_slug: str,
) -> tuple[str, list[TypeMessageEntity]]:
    header = lex.live_progress_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=elapsed_sec,
    )
    blocks = _build_budgeted_quote_blocks(
        lex=lex,
        limits=limits,
        thinking_text=event.thinking_text,
        subagents=event.active_subagents,
        tools=event.active_tools,
        draft_text=event.text_accumulated,
        total_char_budget=3500,
    )
    return _assemble_collapsible_trace_message(header, blocks)


def build_final_execution_trace_message(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    result: TurnResult,
    model_slug: str,
    account_slug: str,
) -> tuple[str, list[TypeMessageEntity]] | None:
    if not result.thinking_text and not result.tool_calls and not result.subagents:
        return None

    header = lex.trace_completed_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=result.turn_delta_duration_sec,
        steps_count=len(result.tool_calls),
    )
    blocks = _build_budgeted_quote_blocks(
        lex=lex,
        limits=limits,
        thinking_text=result.thinking_text,
        subagents=result.subagents,
        tools=result.tool_calls,
        draft_text=None,
        total_char_budget=3650,
    )
    return _assemble_collapsible_trace_message(header, blocks)


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
