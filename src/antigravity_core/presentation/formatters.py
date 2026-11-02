from pathlib import Path
import re

from bisect import bisect_left
from telethon.extensions import markdown
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityPre,
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
_MAX_SINGLE_BLOCK_CHARS: int = 3100
_MAX_PAGE_UTF16_LEN: int = 3750
_MAX_BLOCKS_PER_PAGE: int = 25
_WIDE_BUBBLE_TARGET_COLS: int = 92

_HEADING_RE = re.compile(r"^(\s*)(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[*-]\s+(.+)$")
_HRULE_RE = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$")
_QUOTE_LINE_RE = re.compile(r"^\s*>\s?(.*)$")
_SNAKE_CASE_UNDERSCORE_RE = re.compile(r"(?<=[a-zA-Z0-9а-яА-ЯёЁ])_(?=[a-zA-Z0-9а-яА-ЯёЁ])")

_MD_QUOTE_START: str = "\x02"
_MD_QUOTE_END: str = "\x03"
_UNDERSCORE_GUARD: str = "\x04"


def _clean_binary_noise(raw: str) -> str:
    return _BINARY_CONTROL_RE.sub(" ", raw).strip()


def _utf16_len(text: str) -> int:
    return len(add_surrogate(text))


def _split_text_preserving_lines(text: str, chunk_size: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        idx = remaining.rfind("\n", 0, chunk_size)
        if idx <= chunk_size // 3:
            idx = chunk_size
        chunk = remaining[:idx].rstrip()
        if chunk:
            parts.append(chunk)
        remaining = remaining[idx:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


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


def _format_tool_blocks_untruncated(
    lex: UiLexicon, tool: ToolCallSummary
) -> list[str]:
    """Formats a tool call into one or more self-contained collapsible quote blocks without truncation."""
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
        clean_h = _clean_binary_noise(tool.target_hint.replace("\n", " "))[:90]
        hint_str = f" → {clean_h}"

    base_header = f"{icon} #{tool.step_index} {tool.tool_name}{dur}{hint_str}"
    body_sections: list[str] = []

    if tool.parameters_text:
        p_clean = _clean_binary_noise(tool.parameters_text)
        if p_clean:
            body_sections.append(f"{lex.trace_tool_params_label}\n{p_clean}")
    if tool.output_text:
        o_clean = _clean_binary_noise(tool.output_text)
        if o_clean:
            body_sections.append(f"{lex.trace_tool_output_label}\n{o_clean}")

    full_body = "\n".join(body_sections)
    if not full_body:
        return [base_header]

    combined = f"{base_header}\n{full_body}"
    if len(combined) <= _MAX_SINGLE_BLOCK_CHARS:
        return [combined]

    body_chunks = _split_text_preserving_lines(
        full_body, _MAX_SINGLE_BLOCK_CHARS - len(base_header) - 30
    )
    total_parts = len(body_chunks)
    blocks: list[str] = []
    for idx, chunk in enumerate(body_chunks, start=1):
        part_suffix = lex.trace_tool_part_suffix.format(part=idx, total=total_parts)
        blocks.append(f"{base_header}{part_suffix}\n{chunk}")
    return blocks


def _assemble_collapsible_trace_message(
    header_line: str,
    quote_blocks: list[str],
) -> tuple[str, list[TypeMessageEntity]]:
    """Builds plain text with a full-width MessageEntityPre header bar and collapsible quote entities."""
    clean_header = (
        header_line.replace("**", "").replace("`", "").ljust(_WIDE_BUBBLE_TARGET_COLS)
    )
    header_u16 = _utf16_len(clean_header)
    entities: list[TypeMessageEntity] = [
        MessageEntityPre(offset=0, length=header_u16, language="")
    ]
    parts: list[str] = [clean_header]
    current_utf16_offset = header_u16

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


def _build_all_untruncated_blocks(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    thinking_text: str | None,
    subagents: tuple[SubagentState, ...],
    tools: tuple[ToolCallSummary, ...],
    draft_text: str | None,
) -> list[str]:
    quote_blocks: list[str] = []

    if thinking_text and thinking_text.strip():
        t_header = lex.trace_thinking_header.replace("**", "")
        t_clean = _clean_binary_noise(thinking_text)
        t_chunks = _split_text_preserving_lines(
            t_clean, _MAX_SINGLE_BLOCK_CHARS - len(t_header) - 20
        )
        for idx, chunk in enumerate(t_chunks, start=1):
            suffix = (
                lex.trace_tool_part_suffix.format(part=idx, total=len(t_chunks))
                if len(t_chunks) > 1
                else ""
            )
            quote_blocks.append(f"{t_header}{suffix}\n{chunk}")

    if subagents:
        short_len = limits.uuid_short_chars
        sub_lines = [lex.live_progress_subagents_header.replace("**", "")]
        for sub in subagents:
            sub_lines.append(
                f"• {sub.role} ({sub.type_name} — {sub.conversation_id[:short_len]})"
            )
        quote_blocks.append("\n".join(sub_lines))

    for tool in tools:
        quote_blocks.extend(_format_tool_blocks_untruncated(lex, tool))

    if draft_text and draft_text.strip():
        d_lim = limits.draft_preview_max_chars
        d_body = _clean_binary_noise(draft_text.strip()[-d_lim:])
        quote_blocks.append(
            lex.live_progress_draft.replace("**", "").format(preview=d_body)
        )

    return quote_blocks


def _paginate_quote_blocks(
    lex: UiLexicon,
    first_page_header: str,
    all_blocks: list[str],
) -> list[tuple[str, list[TypeMessageEntity]]]:
    if not all_blocks:
        return [_assemble_collapsible_trace_message(first_page_header, [])]

    pages: list[tuple[str, list[TypeMessageEntity]]] = []
    current_blocks: list[str] = []
    page_num = 1
    current_header = first_page_header
    current_utf16 = _utf16_len(current_header.replace("**", "").replace("`", ""))

    for block in all_blocks:
        b_utf16 = _utf16_len(block) + 2
        if current_blocks and (
            current_utf16 + b_utf16 > _MAX_PAGE_UTF16_LEN
            or len(current_blocks) >= _MAX_BLOCKS_PER_PAGE
        ):
            pages.append(
                _assemble_collapsible_trace_message(current_header, current_blocks)
            )
            page_num += 1
            current_header = lex.trace_page_continuation_header.format(
                page_num=page_num
            )
            current_blocks = [block]
            current_utf16 = _utf16_len(current_header) + b_utf16
        else:
            current_blocks.append(block)
            current_utf16 += b_utf16

    if current_blocks:
        pages.append(
            _assemble_collapsible_trace_message(current_header, current_blocks)
        )
    return pages


def build_live_progress_pages(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    event: TurnProgressEvent,
    elapsed_sec: float,
    model_slug: str,
    account_slug: str,
) -> list[tuple[str, list[TypeMessageEntity]]]:
    header = lex.live_progress_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=elapsed_sec,
    )
    all_blocks = _build_all_untruncated_blocks(
        lex=lex,
        limits=limits,
        thinking_text=event.thinking_text,
        subagents=event.active_subagents,
        tools=event.active_tools,
        draft_text=event.text_accumulated,
    )
    return _paginate_quote_blocks(lex, header, all_blocks)


def build_final_execution_trace_pages(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    result: TurnResult,
    model_slug: str,
    account_slug: str,
) -> list[tuple[str, list[TypeMessageEntity]]]:
    if not result.thinking_text and not result.tool_calls and not result.subagents:
        return []

    header = lex.trace_completed_header.format(
        model_slug=model_slug,
        account_slug=account_slug,
        elapsed_sec=result.turn_delta_duration_sec,
        steps_count=len(result.tool_calls),
    )
    all_blocks = _build_all_untruncated_blocks(
        lex=lex,
        limits=limits,
        thinking_text=result.thinking_text,
        subagents=result.subagents,
        tools=result.tool_calls,
        draft_text=None,
    )
    return _paginate_quote_blocks(lex, header, all_blocks)


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

    stats_line = (
        f"⏱ {result.turn_delta_duration_sec:.1f}с │ "
        f"📊 +{tok_k:.1f}k ток (сессия: {cum_k:.1f}k, кэш: {cache_k:.1f}k) │ "
        f"🧠 {model_slug} │ 👤 {account_slug}"
    )
    lines = [stats_line.ljust(_WIDE_BUBBLE_TARGET_COLS)]

    if result.account_switched_from and result.account_switched_to:
        failover_clean = (
            lex.turn_footer_failover.format(
                from_slug=result.account_switched_from,
                to_slug=result.account_switched_to,
            )
            .replace("*", "")
            .replace("`", "")
            .strip()
        )
        lines.append(failover_clean)

    if result.status != TurnStatus.SUCCESS and result.error_message:
        err_lim = limits.error_summary_max_chars
        err_clean = (
            lex.turn_footer_error_status.format(
                status=result.status.value,
                error=result.error_message[:err_lim],
            )
            .replace("*", "")
            .replace("`", "")
            .strip()
        )
        lines.append(err_clean)

    return "\n".join(lines)


def _preprocess_gfm_markdown(raw_md: str) -> str:
    """Converts LLM CommonMark/GFM (headings, bullets, horizontal rules, blockquotes) into Telethon-compatible Markdown."""
    lines = raw_md.splitlines()
    out_lines: list[str] = []
    in_code_fence = False
    quote_buffer: list[str] = []

    def _flush_quote_buffer() -> None:
        if not quote_buffer:
            return
        joined_q = "\n".join(quote_buffer)
        out_lines.append(f"{_MD_QUOTE_START}{joined_q}{_MD_QUOTE_END}")
        quote_buffer.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            _flush_quote_buffer()
            in_code_fence = not in_code_fence
            out_lines.append(line)
            continue

        if in_code_fence:
            out_lines.append(line)
            continue

        q_match = _QUOTE_LINE_RE.match(line)
        if q_match is not None:
            quote_buffer.append(q_match.group(1))
            continue
        _flush_quote_buffer()

        if _HRULE_RE.match(line):
            out_lines.append("─" * 38)
            continue

        h_match = _HEADING_RE.match(line)
        if h_match is not None:
            level = len(h_match.group(2))
            title = h_match.group(3).strip().replace("**", "")
            prefix = "📌" if level <= 2 else "▸"
            out_lines.append(f"{prefix} **{title}**")
            continue

        b_match = _BULLET_RE.match(line)
        if b_match is not None:
            indent = b_match.group(1)
            content = b_match.group(2)
            bullet = "◦" if len(indent) >= 2 else "•"
            line = f"{indent}{bullet} {content}"

        # Protect snake_case underscores outside inline code spans
        parts = line.split("`")
        for idx in range(0, len(parts), 2):
            parts[idx] = _SNAKE_CASE_UNDERSCORE_RE.sub(_UNDERSCORE_GUARD, parts[idx])
        out_lines.append("`".join(parts))

    _flush_quote_buffer()
    return "\n".join(out_lines)


def split_markdown_preserving_blocks(text: str, max_chars: int) -> list[str]:
    """Splits long Markdown text across chunks without breaking fenced code blocks."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    in_fence = False
    fence_header = "```"

    for line in text.splitlines():
        stripped = line.strip()
        line_len = len(line) + 1

        if current_len + line_len > max_chars and current_lines:
            if in_fence:
                current_lines.append("```")
                chunks.append("\n".join(current_lines).strip())
                current_lines = [fence_header, line]
                current_len = len(fence_header) + 1 + line_len
            else:
                chunks.append("\n".join(current_lines).strip())
                current_lines = [line]
                current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_header = stripped
            else:
                in_fence = False
                fence_header = "```"

    if current_lines:
        if in_fence:
            current_lines.append("```")
        chunks.append("\n".join(current_lines).strip())

    return [c for c in chunks if c]


def render_Formatted_response_chunks(
    body_markdown: str,
    footer_bar_text: str,
    max_chars: int,
) -> list[tuple[str, list[TypeMessageEntity]]]:
    """Renders LLM Markdown response into formatted Telegram chunks with a full-width preformatted status bar."""
    raw_chunks = split_markdown_preserving_blocks(body_markdown, max_chars - 220)
    total_chunks = len(raw_chunks)
    rendered_pages: list[tuple[str, list[TypeMessageEntity]]] = []

    for idx, raw_chunk in enumerate(raw_chunks, start=1):
        preprocessed = _preprocess_gfm_markdown(raw_chunk)
        parsed_text, raw_entities = markdown.parse(preprocessed)
        s_text = add_surrogate(parsed_text)

        marker_positions: list[int] = [
            i for i, ch in enumerate(s_text) if ch in (_MD_QUOTE_START, _MD_QUOTE_END)
        ]

        def _proj(pos: int, markers: list[int] = marker_positions) -> int:
            return pos - bisect_left(markers, pos)

        quote_spans: list[tuple[int, int]] = []
        open_q: int | None = None
        for i, ch in enumerate(s_text):
            if ch == _MD_QUOTE_START and open_q is None:
                open_q = i + 1
            elif ch == _MD_QUOTE_END and open_q is not None:
                quote_spans.append((open_q, i))
                open_q = None

        entities: list[TypeMessageEntity] = []
        for ent in raw_entities:
            s_orig = ent.offset
            e_orig = ent.offset + ent.length
            s_new = _proj(s_orig)
            e_new = _proj(e_orig)
            if e_new > s_new:
                ent.offset = s_new
                ent.length = e_new - s_new
                entities.append(ent)

        for qs, qe in quote_spans:
            cs = _proj(qs)
            ce = _proj(qe)
            if ce > cs:
                entities.append(
                    MessageEntityBlockquote(
                        offset=cs,
                        length=ce - cs,
                        collapsed=(ce - cs > 260),
                    )
                )

        clean_s = (
            s_text.replace(_MD_QUOTE_START, "")
            .replace(_MD_QUOTE_END, "")
            .replace(_UNDERSCORE_GUARD, "_")
        )
        clean_body = del_surrogate(clean_s)

        # Attach wide preformatted bar at the bottom of every chunk so Telegram Desktop expands bubble to full width
        if idx < total_chunks:
            bar_text = f"📄 Часть {idx}/{total_chunks}".ljust(_WIDE_BUBBLE_TARGET_COLS)
        else:
            bar_text = footer_bar_text

        sep = "\n\n"
        bar_offset = _utf16_len(clean_body) + _utf16_len(sep)
        bar_len = _utf16_len(bar_text)
        entities.append(
            MessageEntityPre(
                offset=bar_offset,
                length=bar_len,
                language="",
            )
        )
        full_chunk_text = f"{clean_body}{sep}{bar_text}"
        rendered_pages.append((full_chunk_text, entities))

    return rendered_pages


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
