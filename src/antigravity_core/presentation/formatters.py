from bisect import bisect_left
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any
import unicodedata
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
_AGY_TRUNCATION_NOTICE_RE = re.compile(
    r"The tool's output was truncated due to being too long\.[^\n]*\n*"
    r"(?:Here is a preview with the middle of the output truncated:\n*)?",
    re.IGNORECASE,
)
_AGY_CMD_EXIT_ZERO_RE = re.compile(
    r"^Command exited with code 0\.\s*(?:Command output:\s*)?",
    re.IGNORECASE,
)
_AGY_CMD_EXIT_NONZERO_RE = re.compile(
    r"^Command exited with code (\d+)\.\s*(?:Command output:\s*)?",
    re.IGNORECASE,
)
_MAX_SINGLE_BLOCK_CHARS: int = 1800
_MAX_TOOL_OUTPUT_CHARS: int = 480
_MAX_TOOL_OUTPUT_HEAD_LINES: int = 6
_MAX_TOOL_OUTPUT_TAIL_LINES: int = 4
_MAX_PAGE_UTF16_LEN: int = 3750
_MAX_BLOCKS_PER_PAGE: int = 25
_WIDE_BUBBLE_TARGET_COLS: int = 92

_HEADING_RE = re.compile(r"^(\s*)(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[*-]\s+(.+)$")
_HRULE_RE = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$")
_QUOTE_LINE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
_CELL_MD_STRIP_RE = re.compile(r"(\*\*|__|~~|`|\*(?=\S)|(?<=\S)\*)")
_SNAKE_CASE_UNDERSCORE_RE = re.compile(r"(?<=[a-zA-Z0-9а-яА-ЯёЁ])_(?=[a-zA-Z0-9а-яА-ЯёЁ])")
_MAX_BOX_TABLE_WIDTH: int = 98

_MD_QUOTE_START: str = "\x02"
_MD_QUOTE_END: str = "\x03"
_UNDERSCORE_GUARD: str = "\x04"


def _clean_binary_noise(raw: str) -> str:
    return _BINARY_CONTROL_RE.sub(" ", raw).strip()


def _utf16_len(text: str) -> int:
    return len(add_surrogate(text))


def _pad_wide_bar(text: str) -> str:
    clean = text.replace("**", "").replace("`", "").strip()
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if not lines:
        lines = ["─" * _WIDE_BUBBLE_TARGET_COLS]

    first = lines[0]
    if len(first) < _WIDE_BUBBLE_TARGET_COLS:
        pad_len = max(2, _WIDE_BUBBLE_TARGET_COLS - len(first) - 1)
        lines[0] = f"{first} " + ("─" * pad_len)

    # Telegram Desktop downgrades single-line MessageEntityPre(language="") to inline code;
    # having a second line forces block-level <pre> rendering and full-width bubble expansion.
    if len(lines) == 1:
        lines.append("─" * _WIDE_BUBBLE_TARGET_COLS)
    return "\n".join(lines)


def format_wide_header_block(title: str) -> str:
    """Wraps a header line in a 92-column preformatted block so Telegram Desktop expands the bubble."""
    return f"```{_pad_wide_bar(title)}```"


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

    raw_card = lex.topic_card_template.format(
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
    card_lines = raw_card.splitlines()
    if len(card_lines) >= 2 and card_lines[1].startswith("━"):
        header_bar = format_wide_header_block(card_lines[0])
        return "\n".join([header_bar, *card_lines[2:]])
    return raw_card


def render_new_session_divider(lex: UiLexicon, topic: ForumTopic) -> str:
    mode_label = (
        lex.new_session_mode_full_access
        if topic.execution_mode == ExecutionMode.FULL_ACCESS
        else lex.new_session_mode_read_only
    )
    raw_div = lex.new_session_divider_template.format(
        workspace_path=topic.workspace_path,
        model_slug=topic.model_slug,
        mode_label=mode_label,
    )
    div_lines = [ln for ln in raw_div.splitlines() if not ln.startswith("═")]
    if div_lines:
        return "\n".join([format_wide_header_block(div_lines[0]), *div_lines[1:]])
    return raw_div


def _compact_tool_output_head_tail(raw_output: str) -> str:
    cleaned = _clean_binary_noise(raw_output)
    cleaned = _AGY_TRUNCATION_NOTICE_RE.sub("", cleaned).strip()
    cleaned = _AGY_CMD_EXIT_ZERO_RE.sub("", cleaned).strip()
    cleaned = _AGY_CMD_EXIT_NONZERO_RE.sub(r"[exit \1] ", cleaned).strip()

    if cleaned in ("", "No output from the command."):
        return ""

    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    max_lines = _MAX_TOOL_OUTPUT_HEAD_LINES + _MAX_TOOL_OUTPUT_TAIL_LINES + 1

    if len(lines) > max_lines:
        head = "\n".join(lines[:_MAX_TOOL_OUTPUT_HEAD_LINES])
        tail = "\n".join(lines[-_MAX_TOOL_OUTPUT_TAIL_LINES:])
        hidden_count = len(lines) - _MAX_TOOL_OUTPUT_HEAD_LINES - _MAX_TOOL_OUTPUT_TAIL_LINES
        cleaned = f"{head}\n... [+{hidden_count} стр.] ...\n{tail}"
    else:
        cleaned = "\n".join(lines)

    if len(cleaned) > _MAX_TOOL_OUTPUT_CHARS:
        half = (_MAX_TOOL_OUTPUT_CHARS - 30) // 2
        hidden_chars = len(cleaned) - (half * 2)
        cleaned = f"{cleaned[:half].rstrip()}\n... [+{hidden_chars} симв.] ...\n{cleaned[-half:].lstrip()}"

    return cleaned


def _format_tool_blocks_untruncated(
    lex: UiLexicon, tool: ToolCallSummary
) -> list[str]:
    """Formats each tool call into a compact single collapsible quote block without boilerplate."""
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
    clean_h = (
        _clean_binary_noise(tool.target_hint.replace("\n", " "))[:95]
        if tool.target_hint
        else ""
    )
    hint_str = f" → {clean_h}" if clean_h else ""

    base_header = f"{icon} #{tool.step_index} {tool.tool_name}{dur}{hint_str}"
    body_sections: list[str] = []

    if tool.parameters_text:
        p_clean = _clean_binary_noise(tool.parameters_text)[:600]
        # Avoid repeating single-line CommandLine/Path if it already fits completely in base_header
        is_redundant_single_param = (
            "\n" not in p_clean
            and clean_h
            and p_clean.endswith(clean_h)
            and len(p_clean) <= len(clean_h) + 18
        )
        if p_clean and not is_redundant_single_param:
            body_sections.append(f"{lex.trace_tool_params_label}\n{p_clean}")

    if tool.output_text:
        o_clean = _compact_tool_output_head_tail(tool.output_text)
        if o_clean:
            body_sections.append(f"{lex.trace_tool_output_label}\n{o_clean}")

    full_body = "\n".join(body_sections)
    if not full_body:
        return [base_header]

    return [f"{base_header}\n{full_body}"]


def _assemble_collapsible_trace_message(
    header_line: str,
    quote_blocks: list[str],
) -> tuple[str, list[TypeMessageEntity]]:
    """Builds plain text with a full-width MessageEntityPre header bar and collapsible quote entities."""
    clean_header = _pad_wide_bar(header_line.replace("**", "").replace("`", ""))
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

    raw_pages: list[list[str]] = []
    current_blocks: list[str] = []
    current_utf16 = _utf16_len(first_page_header.replace("**", "").replace("`", ""))

    for block in all_blocks:
        b_utf16 = _utf16_len(block) + 2
        if current_blocks and (
            current_utf16 + b_utf16 > _MAX_PAGE_UTF16_LEN
            or len(current_blocks) >= _MAX_BLOCKS_PER_PAGE
        ):
            raw_pages.append(current_blocks)
            current_blocks = [block]
            current_utf16 = 80 + b_utf16
        else:
            current_blocks.append(block)
            current_utf16 += b_utf16

    if current_blocks:
        raw_pages.append(current_blocks)

    total_pages = len(raw_pages)
    assembled_pages: list[tuple[str, list[TypeMessageEntity]]] = []
    for idx, blocks in enumerate(raw_pages, start=1):
        if idx == 1 and total_pages == 1:
            hdr = first_page_header
        elif idx < total_pages:
            # Sealed earlier pages always get a clean static page title
            hdr = lex.trace_page_continuation_header.format(page_num=idx)
        else:
            hdr = f"{first_page_header} [стр. {idx}]"
        assembled_pages.append(_assemble_collapsible_trace_message(hdr, blocks))

    return assembled_pages


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

    stats_line_1 = (
        f"⏱ {result.turn_delta_duration_sec:.1f}с │ "
        f"📊 +{tok_k:.1f}k ток (сессия: {cum_k:.1f}k, кэш: {cache_k:.1f}k)"
    )
    stats_line_2 = f"🧠 {model_slug} │ 👤 {account_slug}"
    lines = [_pad_wide_bar(f"{stats_line_1}\n{stats_line_2}")]

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

    if (
        result.status != TurnStatus.SUCCESS
        and result.error_message
        and not result.response_text.strip()
    ):
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


def _char_display_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if ch in ("\ufe0f", "\u200d"):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    cp = ord(ch)
    if 0x2600 <= cp <= 0x27BF or 0x1F300 <= cp <= 0x1FAFF:
        return 2
    return 1


def _str_display_width(text: str) -> int:
    return sum(_char_display_width(ch) for ch in text)


def _pad_display_width(text: str, target_width: int) -> str:
    pad = max(0, target_width - _str_display_width(text))
    return text + (" " * pad)


def _clean_table_cell(raw_cell: str) -> str:
    return _CELL_MD_STRIP_RE.sub("", raw_cell.strip()).strip()


def _format_markdown_table_block(table_lines: list[str]) -> list[str]:
    parsed_rows: list[list[str]] = []
    for raw_line in table_lines:
        inner = raw_line.strip().strip("|")
        cells = [c.strip() for c in inner.split("|")]
        if cells and all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c):
            continue
        cleaned_cells = [_clean_table_cell(c) for c in cells]
        if any(cleaned_cells):
            parsed_rows.append(cleaned_cells)

    if not parsed_rows:
        return table_lines

    num_cols = max(len(r) for r in parsed_rows)
    normalized_rows: list[list[str]] = [
        r + ([""] * (num_cols - len(r))) for r in parsed_rows
    ]
    col_widths = [
        max(1, max(_str_display_width(row[col_idx]) for row in normalized_rows))
        for col_idx in range(num_cols)
    ]
    total_box_width = sum(col_widths) + (3 * num_cols) + 1

    if total_box_width <= _MAX_BOX_TABLE_WIDTH:
        top_border = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
        mid_border = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
        bot_border = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"

        box_lines: list[str] = ["```", top_border]
        for row_idx, row in enumerate(normalized_rows):
            padded_cells = [
                _pad_display_width(row[c_i], col_widths[c_i])
                for c_i in range(num_cols)
            ]
            box_lines.append("│ " + " │ ".join(padded_cells) + " │")
            if row_idx == 0 and len(normalized_rows) > 1:
                box_lines.append(mid_border)
        box_lines.append(bot_border)
        box_lines.append("```")
        return box_lines

    # Fallback for extra-wide tables: render each row as a compact card without exhausting Telegram's 100-entity limit
    headers = normalized_rows[0]
    data_rows = normalized_rows[1:] if len(normalized_rows) > 1 else normalized_rows
    card_lines: list[str] = []
    for row in data_rows:
        title_cell = row[0] if row else "—"
        second_hdr = f" — {row[1]}" if num_cols > 1 and row[1] else ""
        card_lines.append(f"▸ **{title_cell}{second_hdr}**")
        start_col = 2 if num_cols > 1 and row[1] else 1
        for c_i in range(start_col, num_cols):
            col_name = headers[c_i] if c_i < len(headers) and headers[c_i] else f"#{c_i + 1}"
            val = row[c_i] if row[c_i] else "—"
            card_lines.append(f"  • {col_name}: {val}")
    return card_lines


def _preprocess_gfm_markdown(raw_md: str) -> str:
    """Converts LLM CommonMark/GFM (headings, bullets, tables, horizontal rules, blockquotes) into Telethon Markdown."""
    lines = raw_md.splitlines()
    out_lines: list[str] = []
    in_code_fence = False
    quote_buffer: list[str] = []
    table_buffer: list[str] = []

    def _flush_quote_buffer() -> None:
        if not quote_buffer:
            return
        joined_q = "\n".join(quote_buffer)
        out_lines.append(f"{_MD_QUOTE_START}{joined_q}{_MD_QUOTE_END}")
        quote_buffer.clear()

    def _flush_table_buffer() -> None:
        if not table_buffer:
            return
        out_lines.extend(_format_markdown_table_block(table_buffer))
        table_buffer.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            _flush_quote_buffer()
            _flush_table_buffer()
            if stripped != "```" and stripped.endswith("```") and len(stripped) > 6:
                out_lines.append(line)
                continue
            in_code_fence = not in_code_fence
            # Always emit clean '```' without language tags so Telethon markdown never prints 'bash'/'table' inside the block
            out_lines.append("```")
            continue

        if in_code_fence:
            out_lines.append(line)
            continue

        if _TABLE_ROW_RE.match(line):
            _flush_quote_buffer()
            table_buffer.append(line)
            continue
        _flush_table_buffer()

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
    _flush_table_buffer()
    return "\n".join(out_lines)


def split_markdown_preserving_blocks(text: str, max_chars: int) -> list[str]:
    """Splits long Markdown text across chunks by UTF-16 length without breaking fenced code blocks."""
    if _utf16_len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    in_fence = False
    fence_header = "```"
    safe_line_max = max(200, max_chars - 50)

    raw_lines: list[str] = []
    for orig_line in text.splitlines():
        if _utf16_len(orig_line) <= safe_line_max:
            raw_lines.append(orig_line)
        else:
            for pos in range(0, len(orig_line), safe_line_max):
                raw_lines.append(orig_line[pos : pos + safe_line_max])

    for line in raw_lines:
        stripped = line.strip()
        line_len = _utf16_len(line) + 1

        if current_len + line_len > max_chars and current_lines:
            if in_fence:
                current_lines.append("```")
                chunks.append("\n".join(current_lines).strip())
                current_lines = [fence_header, line]
                current_len = _utf16_len(fence_header) + 1 + line_len
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
                fence_header = "```"
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
    """Preprocesses GFM tables/headings first, then splits into safe chunks <= max_chars with full-width footer bars."""
    preprocessed_full = _preprocess_gfm_markdown(body_markdown)
    # Strip leading/trailing newlines right inside ``` fences so code blocks and tables have no empty top/bottom lines
    preprocessed_full = re.sub(r"```\n+", "```", preprocessed_full)
    preprocessed_full = re.sub(r"\n+```", "```", preprocessed_full)

    # Re-expand ``` lines for safe chunk splitting, then strip inner newlines per chunk
    chunk_ready = preprocessed_full.replace("```", "\n```\n")
    raw_chunks = split_markdown_preserving_blocks(chunk_ready, max_chars - 260)
    total_chunks = len(raw_chunks)
    rendered_pages: list[tuple[str, list[TypeMessageEntity]]] = []

    for idx, raw_chunk in enumerate(raw_chunks, start=1):
        tight_chunk = re.sub(r"```\n+", "```", raw_chunk)
        tight_chunk = re.sub(r"\n+```", "```", tight_chunk)
        parsed_text, raw_entities = markdown.parse(tight_chunk)
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

        # Telegram Server silently drops all entities beyond index 99 (100-entity hard limit per message).
        # Reserve the last slot so the bottom wide MessageEntityPre bar is guaranteed never to be dropped.
        if len(entities) > 94:
            entities = entities[:94]

        if idx < total_chunks:
            bar_text = _pad_wide_bar(f"📄 Часть {idx}/{total_chunks}")
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

    raw_hdr_lines = lex.dashboard_header.format(
        warm_count=warm_count, running_count=running_count
    ).splitlines()
    if len(raw_hdr_lines) >= 2 and raw_hdr_lines[1].startswith("━"):
        lines: list[str] = [
            format_wide_header_block(raw_hdr_lines[0]),
            *raw_hdr_lines[2:],
        ]
    else:
        lines = [
            format_wide_header_block(raw_hdr_lines[0]),
            *raw_hdr_lines[1:],
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
        backup_badge = (
            lex.dashboard_account_backup_ok
            if acc.encrypted_token_backup
            else lex.dashboard_account_backup_missing
        )
        quota_lines = format_account_quota_lines(acc.last_quota_json, lex)
        if not quota_lines:
            lines.append(
                lex.dashboard_account_item.format(
                    status_badge=status_badge,
                    slug=acc.slug,
                    email_label=acc.email_label,
                    default_badge=default_badge,
                    quota_str=lex.dashboard_account_quota_unprobed,
                    backup_badge=backup_badge,
                )
            )
        else:
            min_pct = acc.last_quota_json.get("min_remaining_pct", "?") if acc.last_quota_json else "?"
            short_q = lex.dashboard_account_quota_probed.format(min_pct=min_pct)
            lines.append(
                lex.dashboard_account_item.format(
                    status_badge=status_badge,
                    slug=acc.slug,
                    email_label=acc.email_label,
                    default_badge=default_badge,
                    quota_str=short_q,
                    backup_badge=backup_badge,
                )
            )
            lines.extend(quota_lines)

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


def _render_mini_bar(pct: int, width: int = 8) -> str:
    filled = max(0, min(width, round((pct / 100.0) * width)))
    return ("█" * filled) + ("░" * (width - filled))


def _format_reset_countdown(iso_reset_time: str) -> str:
    if not iso_reset_time:
        return ""
    try:
        dt = datetime.fromisoformat(iso_reset_time.replace("Z", "+00:00"))
        now = datetime.now(tz=UTC)
        diff_sec = int((dt - now).total_seconds())
        if diff_sec <= 0:
            return ""
        days, rem = divmod(diff_sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days > 0:
            return f" (↻ {days}д {hours}ч)"
        if hours > 0:
            return f" (↻ {hours}ч {mins}м)"
        return f" (↻ {mins}м)"
    except ValueError:
        return ""


def format_account_quota_lines(
    quota_json: dict[str, Any] | None, lex: UiLexicon
) -> list[str]:
    if not quota_json:
        return []
    raw_buckets = quota_json.get("buckets")
    if not isinstance(raw_buckets, dict) or not raw_buckets:
        return []

    def _fmt_bucket(b_key: str) -> str | None:
        b = raw_buckets.get(b_key)
        if not isinstance(b, dict):
            return None
        pct = int(b.get("remaining_pct", 100))
        rst = _format_reset_countdown(str(b.get("reset_time", "")))
        return f"`{_render_mini_bar(pct)} {pct}%`{rst}"

    gem_5h = _fmt_bucket("gemini-5h")
    gem_wk = _fmt_bucket("gemini-weekly")
    c_5h = _fmt_bucket("3p-5h")
    c_wk = _fmt_bucket("3p-weekly")

    out: list[str] = []
    if gem_5h or gem_wk:
        parts_g = []
        if gem_5h:
            parts_g.append(f"**5ч:** {gem_5h}")
        if gem_wk:
            parts_g.append(f"**Нед:** {gem_wk}")
        out.append("    ├─ 🧠 **Gemini:** " + " │ ".join(parts_g))

    if c_5h or c_wk:
        parts_c = []
        if c_5h:
            parts_c.append(f"**5ч:** {c_5h}")
        if c_wk:
            parts_c.append(f"**Нед:** {c_wk}")
        out.append("    └─ 🤖 **Claude/3P:** " + " │ ".join(parts_c))

    if not out:
        for b_key, b_val in raw_buckets.items():
            if isinstance(b_val, dict):
                pct = int(b_val.get("remaining_pct", 100))
                rst = _format_reset_countdown(str(b_val.get("reset_time", "")))
                win = str(b_val.get("window") or b_key)
                out.append(f"    • `{win}`: `{_render_mini_bar(pct)} {pct}%`{rst}")

    return out


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
