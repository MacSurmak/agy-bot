from bisect import bisect_left
from datetime import UTC, datetime
import html
from pathlib import Path
import re
from typing import Final
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
    EventSensor,
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
from antigravity_core.presentation.tg_renderer import (
    WIDE_BAR_LANG,
    make_wide_single_line_bar,
    normalize_latex_outside_code,
    supports_native_rich_output,
)

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
_AGY_TIMESTAMP_LINES_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:Created|Completed) At:[^\n]*\n?",
    re.MULTILINE,
)
_AGY_CMD_EXIT_NONZERO_RE: Final[re.Pattern[str]] = re.compile(
    r"^Command exited with code (\d+)\.\s*(?:Command output:\s*)?",
    re.IGNORECASE,
)
_BRAIN_PATH_RE: re.Pattern[str] = re.compile(
    r"(?:/home/[^/\s'\"]+/profiles/[^/\s'\"]+/home|/root|/home/[^/\s'\"]+)/\.gemini/antigravity-cli/brain/[0-9a-fA-F\-]{36}/"
)
_PROFILE_HOME_GEMINI_RE: re.Pattern[str] = re.compile(
    r"/home/[^/\s'\"]+/profiles/([^/\s'\"]+)/home/\.gemini/"
)
_REMOTES_PATH_RE: re.Pattern[str] = re.compile(
    r"/home/[^/\s'\"]+/remotes/"
)
_HOME_PATH_RE: re.Pattern[str] = re.compile(r"(?<![A-Za-z0-9_/-])/home/[^/\s'\"]+/")
_SINGLE_PARAM_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:CommandLine|Path|TargetFile|AbsolutePath|DirectoryPath|SearchPath|file_path):\s*"
)
_MAX_SINGLE_BLOCK_CHARS: Final[int] = 1800
_MAX_TOOL_OUTPUT_CHARS: Final[int] = 2000
_MAX_TOOL_OUTPUT_HEAD_LINES: Final[int] = 15
_MAX_TOOL_OUTPUT_TAIL_LINES: Final[int] = 12
_MAX_RICH_HTML_PAGE_CHARS: Final[int] = 6800
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
_SNAKE_CASE_UNDERSCORE_RE = re.compile(
    r"(?<=[a-zA-Z0-9а-яА-ЯёЁ])_(?=[a-zA-Z0-9а-яА-ЯёЁ])"
)
_MAX_BOX_TABLE_WIDTH: int = 98

_MD_QUOTE_START: str = "\x02"
_MD_QUOTE_END: str = "\x03"
_UNDERSCORE_GUARD: str = "\x04"


def _clean_binary_noise(raw: str) -> str:
    return _BINARY_CONTROL_RE.sub(" ", raw).strip()


def _shorten_verbose_paths(text: str) -> str:
    shortened = _BRAIN_PATH_RE.sub("~brain/", text)
    shortened = _PROFILE_HOME_GEMINI_RE.sub(r"~\1/.gemini/", shortened)
    shortened = _REMOTES_PATH_RE.sub("~remotes/", shortened)
    return _HOME_PATH_RE.sub("~/", shortened)


def _utf16_len(text: str) -> int:
    return len(add_surrogate(text))


def _pad_wide_bar(text: str) -> str:
    return make_wide_single_line_bar(text)


def format_wide_header_block(title: str) -> str:
    """Renders a clean native Markdown H3 heading when RichMessage is active, or a 1-line wide pre block."""
    if supports_native_rich_output():
        clean_title = title.replace("**", "").strip()
        return f"### {clean_title}"
    return f"```{make_wide_single_line_bar(title)}\n\u2060```"


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
    sudo_str = (
        lex.topic_card_sudo_cb_on
        if topic.sudo_carte_blanche
        else lex.topic_card_sudo_cb_off
    )

    ttl_min = max(1, worker_ttl_sec // 60)
    worker_str = (
        lex.topic_card_worker_warm.format(ttl_min=ttl_min)
        if is_worker_warm
        else lex.topic_card_worker_cold.format(ttl_min=ttl_min)
    )

    rendered = lex.topic_card_template.format(
        title=topic.title,
        session_str=session_str,
        workspace_path=topic.workspace_path,
        model_slug=topic.model_slug,
        effort_str=effort_str,
        account_slug=account.slug,
        email_label=account.email_label,
        mode_str=mode_str,
        sudo_str=sudo_str,
        worker_str=worker_str,
    )
    if supports_native_rich_output():
        return f"{rendered.rstrip()}\n\n{RICH_SPACER_MENU}"
    return rendered


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


def _compact_tool_output_head_tail(
    raw_output: str, lex: UiLexicon
) -> tuple[str, bool]:
    """Strips agy metadata timestamps, detects explicit exit status, and compacts long output."""
    cleaned = _clean_binary_noise(raw_output)
    cleaned = _AGY_TIMESTAMP_LINES_RE.sub("", cleaned).strip()
    cleaned = _AGY_TRUNCATION_NOTICE_RE.sub("", cleaned).strip()
    exited_zero = _AGY_CMD_EXIT_ZERO_RE.match(cleaned) is not None
    cleaned = _AGY_CMD_EXIT_ZERO_RE.sub("", cleaned).strip()
    cleaned = _AGY_CMD_EXIT_NONZERO_RE.sub(r"[exit \1] ", cleaned).strip()

    if cleaned in ("", "No output from the command."):
        return "", exited_zero

    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    max_lines = _MAX_TOOL_OUTPUT_HEAD_LINES + _MAX_TOOL_OUTPUT_TAIL_LINES + 1

    if len(lines) > max_lines:
        head = "\n".join(lines[:_MAX_TOOL_OUTPUT_HEAD_LINES])
        tail = "\n".join(lines[-_MAX_TOOL_OUTPUT_TAIL_LINES:])
        hidden_count = (
            len(lines) - _MAX_TOOL_OUTPUT_HEAD_LINES - _MAX_TOOL_OUTPUT_TAIL_LINES
        )
        marker = lex.trace_hidden_lines_marker.format(count=hidden_count)
        cleaned = f"{head}\n{marker}\n{tail}"
    else:
        cleaned = "\n".join(lines)

    if len(cleaned) > _MAX_TOOL_OUTPUT_CHARS:
        half = (_MAX_TOOL_OUTPUT_CHARS - 30) // 2
        hidden_chars = len(cleaned) - (half * 2)
        c_marker = lex.trace_hidden_chars_marker.format(count=hidden_chars)
        cleaned = f"{cleaned[:half].rstrip()}\n{c_marker}\n{cleaned[-half:].lstrip()}"

    return cleaned, exited_zero


def _is_tool_output_error(cleaned_output: str, exited_zero: bool) -> bool:
    if exited_zero:
        return False
    if not cleaned_output:
        return False
    if cleaned_output.startswith(("[exit ", "Error executing tool", "Error:")):
        return True
    return "Traceback (most recent call last):" in cleaned_output


def _format_tool_blocks_untruncated(
    lex: UiLexicon,
    tool: ToolCallSummary,
    is_superseded_or_final: bool,
) -> list[str]:
    """Formats each tool call into a compact single collapsible quote block without duplicate commands."""
    o_clean, exited_zero = (
        _compact_tool_output_head_tail(tool.output_text, lex)
        if tool.output_text
        else ("", False)
    )
    has_err = _is_tool_output_error(o_clean, exited_zero) or (
        tool.state == StepState.ACTIVE and is_superseded_or_final
    )

    if has_err:
        icon = "❌"
    elif tool.state == StepState.DONE:
        icon = lex.live_progress_tool_done_icon
    else:
        icon = lex.live_progress_tool_active_icon

    dur = (
        lex.live_progress_tool_duration_suffix.format(duration=tool.duration_seconds)
        if tool.duration_seconds is not None
        else ""
    )
    raw_hint = (
        _shorten_verbose_paths(_clean_binary_noise(tool.target_hint.replace("\n", " ")))
        if tool.target_hint
        else ""
    )
    clean_h = raw_hint[:140]
    hint_str = f" → {clean_h}" if clean_h else ""

    base_header = f"{icon} #{tool.step_index} {tool.tool_name}{dur}{hint_str}"
    body_sections: list[str] = []

    if tool.parameters_text:
        p_clean = _shorten_verbose_paths(_clean_binary_noise(tool.parameters_text))[
            :2000
        ]
        p_stripped = _SINGLE_PARAM_PREFIX_RE.sub("", p_clean).strip()
        # Omit parameters section only when the single-line header already displays the exact full command/path
        is_redundant_param = (
            "\n" not in p_clean and bool(clean_h) and p_stripped == clean_h
        )
        if p_clean and not is_redundant_param:
            body_sections.append(f"{lex.trace_tool_params_label}\n{p_stripped}")

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
    """Builds plain text with a 1-line full-width MessageEntityPre header bar and collapsible quote entities."""
    clean_header = make_wide_single_line_bar(header_line)
    header_u16 = _utf16_len(clean_header)
    entities: list[TypeMessageEntity] = [
        MessageEntityPre(offset=0, length=header_u16, language=WIDE_BAR_LANG)
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
    is_final: bool = False,
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
        quote_blocks.extend(
            _format_tool_blocks_untruncated(
                lex, tool, is_superseded_or_final=is_final
            )
        )

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
        else:
            hdr = lex.trace_active_page_suffix.format(
                header=first_page_header, page_num=idx
            )
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
        is_final=True,
    )
    return _paginate_quote_blocks(lex, header, all_blocks)


_COMPACT_BAR_WIDTH: Final[int] = 8
_DEFAULT_SSH_PORT: Final[int] = 22
_SHORT_ANSWER_THRESHOLD_CHARS: Final[int] = 260

RICH_SPACER_ANSWER_FULL: Final[str] = (
    "<table><tr>"
    + "".join("<td>" + ("\u2800" * 12) + "</td>" for _ in range(10))
    + "</tr></table>"
)
RICH_SPACER_MENU: Final[str] = (
    "<table><tr>"
    + "".join("<td>" + ("\u2800" * 6) + "</td>" for _ in range(8))
    + "</tr></table>"
)


def _render_html_header(header_line: str) -> str:
    raw_clean = header_line.replace("**", "").replace("`", "").strip()
    return f"<p><b>{html.escape(raw_clean)}</b></p>"


def _format_trace_blocks_as_html_pages(
    lex: UiLexicon,
    header_line: str,
    blocks: list[str],
    max_html_chars: int,
) -> list[str]:
    """Paginates collapsible <details> trace blocks into Rich HTML messages below Telegram Show More cutoff."""
    if not blocks:
        return [f"{_render_html_header(header_line)}\n{RICH_SPACER_MENU}"]

    pieces: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        summary_raw = lines[0].strip() if lines else "Step"
        body_raw = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        if body_raw:
            display_body = body_raw
        elif summary_raw.startswith("⏳"):
            display_body = "..."
        else:
            display_body = "—"
        piece = (
            f"<details><summary>{html.escape(summary_raw)}</summary>"
            f"<pre><code>{html.escape(display_body)}</code></pre></details>"
        )
        pieces.append(piece)

    raw_pages: list[list[str]] = []
    current_page: list[str] = []
    header_budget = len(_render_html_header(header_line)) + 32
    curr_len = header_budget

    for piece in pieces:
        piece_len = len(piece) + 1
        if current_page and (curr_len + piece_len) > max_html_chars:
            raw_pages.append(current_page)
            current_page = [piece]
            curr_len = header_budget + piece_len
        else:
            current_page.append(piece)
            curr_len += piece_len

    if current_page:
        raw_pages.append(current_page)

    total_pages = len(raw_pages)
    rendered: list[str] = []
    for idx, page_pieces in enumerate(raw_pages, start=1):
        if total_pages == 1:
            hdr_text = header_line
        else:
            hdr_text = lex.trace_active_page_suffix.format(
                header=header_line, page_num=idx
            )
        rendered.append(
            "\n".join([_render_html_header(hdr_text), *page_pieces, RICH_SPACER_MENU])
        )
    return rendered


def build_live_progress_rich_html_pages(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    event: TurnProgressEvent,
    elapsed_sec: float,
    model_slug: str,
    account_slug: str,
) -> list[str]:
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
        is_final=False,
    )
    return _format_trace_blocks_as_html_pages(
        lex, header, all_blocks, _MAX_RICH_HTML_PAGE_CHARS
    )


def build_final_trace_rich_html_pages(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    result: TurnResult,
    model_slug: str,
    account_slug: str,
) -> list[str]:
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
        is_final=True,
    )
    return _format_trace_blocks_as_html_pages(
        lex, header, all_blocks, _MAX_RICH_HTML_PAGE_CHARS
    )


def _should_use_wide_answer_bubble(body_markdown: str, chunk_count: int) -> bool:
    if chunk_count > 1 or len(body_markdown.strip()) >= _SHORT_ANSWER_THRESHOLD_CHARS:
        return True
    structured_markers = ("\n#", "\n- ", "\n1. ", "| ---", "```")
    return any(marker in body_markdown for marker in structured_markers)


def split_rich_markdown_chunks(
    body_markdown: str,
    footer_bar_text: str,
    max_chars: int,
) -> list[str]:
    """Splits native RichMessage Markdown below Telegram Show More cutoff with uniform full-width bubbles."""
    clean_footer = footer_bar_text.strip()
    footer_block = f"\n\n---\n*{clean_footer}*"
    spacer_overhead = len(RICH_SPACER_ANSWER_FULL) + 4
    budget = max(1000, max_chars - len(footer_block) - spacer_overhead - 120)
    chunks = split_markdown_preserving_blocks(body_markdown, budget)
    if not chunks:
        return [footer_block.lstrip()]

    if not _should_use_wide_answer_bubble(body_markdown, len(chunks)):
        chunks[-1] = f"{chunks[-1]}{footer_block}"
        return chunks

    total = len(chunks)
    for idx in range(total - 1):
        chunks[idx] = f"{chunks[idx]}\n\n{RICH_SPACER_ANSWER_FULL}"
    chunks[-1] = f"{chunks[-1]}\n\n{RICH_SPACER_ANSWER_FULL}{footer_block}"
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

    stats_single_line = lex.turn_footer_stats.format(
        duration=result.turn_delta_duration_sec,
        tok_k=tok_k,
        cum_k=cum_k,
        cache_k=cache_k,
        model_slug=model_slug,
        account_slug=account_slug,
    )
    clean_stats = stats_single_line.replace("**", "").replace("`", "").strip()
    lines = [
        clean_stats
        if supports_native_rich_output()
        else make_wide_single_line_bar(stats_single_line)
    ]

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
                _pad_display_width(row[c_i], col_widths[c_i]) for c_i in range(num_cols)
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
            col_name = (
                headers[c_i] if c_i < len(headers) and headers[c_i] else f"#{c_i + 1}"
            )
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
            # Omit crooked horizontal rules; leave a clean paragraph break
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue

        line = normalize_latex_outside_code(line)

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


def _tighten_fenced_blocks_preserving_outer_newlines(md_text: str) -> str:
    """Removes empty lines strictly INSIDE ```...``` blocks while keeping outer paragraph newlines intact."""
    parts = md_text.split("```")
    if len(parts) < 3:
        return md_text

    rebuilt: list[str] = [parts[0]]
    for idx in range(1, len(parts), 2):
        code_inner = parts[idx].strip("\n")
        if "\n" not in code_inner:
            code_inner = f"{code_inner}\n\u2060"
        after_text = parts[idx + 1] if (idx + 1) < len(parts) else ""
        if rebuilt[-1] and not rebuilt[-1].endswith("\n"):
            rebuilt[-1] += "\n"
        if after_text and not after_text.startswith("\n"):
            after_text = "\n" + after_text
        rebuilt.append(f"```{code_inner}```")
        rebuilt.append(after_text)
    return "".join(rebuilt)


def render_Formatted_response_chunks(
    body_markdown: str,
    footer_bar_text: str,
    max_chars: int,
    lex: UiLexicon | None = None,
) -> list[tuple[str, list[TypeMessageEntity]]]:
    """Preprocesses GFM tables/headings/LaTeX first, then splits into safe chunks with 1-line wide footer bars."""
    effective_max = 30000 if supports_native_rich_output() else max_chars
    preprocessed_full = (
        body_markdown
        if supports_native_rich_output()
        else _preprocess_gfm_markdown(body_markdown)
    )
    raw_chunks = split_markdown_preserving_blocks(preprocessed_full, effective_max - 260)
    total_chunks = len(raw_chunks)
    rendered_pages: list[tuple[str, list[TypeMessageEntity]]] = []

    for idx, raw_chunk in enumerate(raw_chunks, start=1):
        tight_chunk = _tighten_fenced_blocks_preserving_outer_newlines(raw_chunk)
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
            chunk_tpl = (
                lex.response_chunk_bar
                if lex is not None
                else "📄 Часть {idx} из {total}"
            )
            bar_text = make_wide_single_line_bar(
                chunk_tpl.format(idx=idx, total=total_chunks)
            )
        else:
            bar_text = make_wide_single_line_bar(footer_bar_text)

        sep = "\n\n"
        bar_offset = _utf16_len(clean_body) + _utf16_len(sep)
        bar_len = _utf16_len(bar_text)
        entities.append(
            MessageEntityPre(
                offset=bar_offset,
                length=bar_len,
                language=WIDE_BAR_LANG,
            )
        )
        full_chunk_text = f"{clean_body}{sep}{bar_text}"
        rendered_pages.append((full_chunk_text, entities))

    return rendered_pages


def _classify_bucket_window_slot(
    b_key: str, b_val: dict[str, int | float | str]
) -> str:
    raw_win = str(b_val["window"]).lower() if "window" in b_val else ""
    raw_name = str(b_val["name"]).lower() if "name" in b_val else b_key.lower()
    if "5h" in b_key or "5h" in raw_win or "five hour" in raw_name or "5-hour" in raw_name:
        return "5h"
    if "week" in b_key or "week" in raw_win or "weekly" in raw_name:
        return "weekly"
    return "other"


def _format_compact_quota_cell(
    b_val: dict[str, int | float | str], lex: UiLexicon
) -> str:
    raw_pct = b_val["remaining_pct"] if "remaining_pct" in b_val else 100
    pct = int(raw_pct) if isinstance(raw_pct, int | float) else 100
    raw_rst = str(b_val["reset_time"]) if "reset_time" in b_val else ""
    rst = (
        f" (↻ {raw_rst})"
        if raw_rst and not raw_rst.startswith(("19", "20"))
        else _format_reset_countdown(raw_rst, lex)
    )
    return f"`{_render_mini_bar(pct, _COMPACT_BAR_WIDTH)} {pct}%`{rst}"


def _build_account_quota_table_rows(
    quota_json: dict[str, int | float | str | list[int] | dict[str, dict[str, int | float | str]]] | None,
    lex: UiLexicon,
) -> list[tuple[str, str, str]]:
    if not quota_json or "buckets" not in quota_json:
        return []
    raw_buckets = quota_json["buckets"]
    if not isinstance(raw_buckets, dict) or not raw_buckets:
        return []

    rows_by_group: dict[str, dict[str, str]] = {}
    for b_key, b_val in raw_buckets.items():
        if not isinstance(b_val, dict):
            continue
        grp, _ = _format_dynamic_bucket_entry(b_key, b_val, lex)
        if grp not in rows_by_group:
            rows_by_group[grp] = {"5h": "—", "weekly": "—"}
        slot = _classify_bucket_window_slot(b_key, b_val)
        cell_txt = _format_compact_quota_cell(b_val, lex)
        if slot == "5h":
            rows_by_group[grp]["5h"] = cell_txt
        else:
            rows_by_group[grp]["weekly"] = cell_txt

    return [
        (grp_name, slots["5h"], slots["weekly"])
        for grp_name, slots in rows_by_group.items()
    ]


def _build_accounts_dashboard_lines(
    lex: UiLexicon,
    accounts: list[GoogleAccount],
) -> list[str]:
    lines: list[str] = [lex.dashboard_accounts_header]
    if not accounts:
        lines.append(lex.dashboard_accounts_empty)
        return lines

    for acc in accounts:
        default_badge = f" {lex.dashboard_account_default_badge}" if acc.is_default else ""
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
        lines.append(
            f"\n#### {status_badge}{default_badge} `{acc.email_label}` · {backup_badge}\n"
        )
        q_rows = _build_account_quota_table_rows(acc.last_quota_json, lex)
        if not q_rows:
            lines.append(f"- {lex.dashboard_account_quota_unprobed}")
            continue
        lines.append(lex.dashboard_accounts_table_header)
        for grp_name, cell_5h, cell_wk in q_rows:
            lines.append(f"| {grp_name} | {cell_5h} | {cell_wk} |")
    return lines


def _build_hosts_dashboard_lines(
    lex: UiLexicon,
    hosts: list[RemoteHost],
    mount_statuses: dict[str, bool],
) -> list[str]:
    lines: list[str] = [lex.dashboard_hosts_header]
    if not hosts:
        lines.append(lex.dashboard_hosts_empty)
        return lines
    lines.append(lex.dashboard_hosts_table_header)
    for h in hosts:
        mounted = mount_statuses[h.alias] if h.alias in mount_statuses else False
        m_badge = (
            lex.dashboard_host_mounted_badge
            if mounted
            else lex.dashboard_host_unmounted_badge
        )
        port_suffix = f":{h.ssh_port}" if h.ssh_port != _DEFAULT_SSH_PORT else ""
        lines.append(
            f"| `{h.alias}` | `{h.ssh_user}@{h.hostname_or_ip}{port_suffix}` | {m_badge} |"
        )
    return lines


def render_control_panel_dashboard(
    lex: UiLexicon,
    limits: LimitsStaticConfig,
    accounts: list[GoogleAccount],
    hosts: list[RemoteHost],
    mount_statuses: dict[str, bool],
    pool_snapshots: list[WorkerTopicSnapshot],
    tasks: list[ScheduledTask],
    settings: DynamicSettings,
    sensors: list[EventSensor] | None,
) -> str:
    warm_count = sum(1 for s in pool_snapshots if s.is_warm)
    running_count = sum(1 for s in pool_snapshots if s.is_running_turn)

    hdr_lines = lex.dashboard_header.format(
        warm_count=warm_count, running_count=running_count
    ).splitlines()
    lines: list[str] = hdr_lines
    accounts_by_slug = {a.slug: a.display_email for a in accounts}
    for snap in pool_snapshots:
        if snap.is_warm:
            state_badge = (
                lex.dashboard_worker_running_badge
                if snap.is_running_turn
                else lex.dashboard_worker_warm_badge
            )
            acc_label = (
                accounts_by_slug[snap.account_slug]
                if snap.account_slug and snap.account_slug in accounts_by_slug
                else snap.account_slug
            )
            lines.append(
                lex.dashboard_worker_item.format(
                    topic_id=snap.topic_id,
                    state_badge=state_badge,
                    model_slug=snap.model_slug,
                    account_slug=acc_label,
                )
            )

    lines.extend(_build_accounts_dashboard_lines(lex, accounts))
    lines.extend(_build_hosts_dashboard_lines(lex, hosts, mount_statuses))

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

    active_sensors = sensors if sensors is not None else []
    lines.append(lex.dashboard_sensors_header.format(count=len(active_sensors)))
    for s in active_sensors[:tasks_lim]:
        mode_tag = "1x" if s.one_shot else f"↻ {s.poll_interval_sec}s"
        lines.append(
            lex.dashboard_sensor_item.format(
                short_id=str(s.id)[:short_len],
                topic_id=s.topic_id,
                name=s.name,
                sensor_kind=s.sensor_kind.value,
                condition=s.trigger_condition.value,
                mode_tag=mode_tag,
            )
        )

    lines.append(
        lex.dashboard_settings_summary.format(
            ui_language=settings.ui_language,
            default_model=settings.default_model,
            default_execution_mode=settings.default_execution_mode.value,
            worker_idle_ttl_sec=settings.worker_idle_ttl_sec,
            debounce_window_sec=settings.debounce_window_sec,
            quota_auto_switch_threshold_pct=settings.quota_auto_switch_threshold_pct,
        )
    )
    if supports_native_rich_output():
        lines.append(f"\n{RICH_SPACER_MENU}")
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


def _format_reset_countdown(iso_reset_time: str, lex: UiLexicon) -> str:
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
            return lex.quota_reset_days_hours.format(days=days, hours=hours)
        if hours > 0:
            return lex.quota_reset_hours_mins.format(hours=hours, mins=mins)
        return lex.quota_reset_mins.format(mins=mins)
    except ValueError:
        return ""


def _format_dynamic_bucket_entry(
    b_key: str,
    b_val: dict[str, int | float | str],
    lex: UiLexicon,
) -> tuple[str, str]:
    raw_pct = b_val["remaining_pct"] if "remaining_pct" in b_val else 100
    pct = int(raw_pct) if isinstance(raw_pct, int | float) else 100
    raw_rst = str(b_val["reset_time"]) if "reset_time" in b_val else ""
    rst = (
        f" (↻ {raw_rst})"
        if raw_rst and not raw_rst.startswith(("19", "20"))
        else _format_reset_countdown(raw_rst, lex)
    )
    bar_str = f"`{_render_mini_bar(pct, 8)} {pct}%`{rst}"

    raw_win = str(b_val["window"]).lower() if "window" in b_val else ""
    raw_name = str(b_val["name"]) if "name" in b_val else b_key
    name_low = raw_name.lower()
    if "5h" in b_key or "5h" in raw_win or "five hour" in name_low or "5-hour" in name_low:
        short_lbl = lex.quota_5h_label.format(bar=bar_str)
    elif "week" in b_key or "week" in raw_win or "weekly" in name_low:
        short_lbl = lex.quota_weekly_label.format(bar=bar_str)
    else:
        short_lbl = f"**{raw_name}:** {bar_str}"

    grp = str(b_val["group_title"]).strip() if "group_title" in b_val else ""
    if not grp:
        if "argon" in b_key or "argon" in name_low:
            grp = "Gemini 4 Argon"
        elif "gemini" in b_key or "gemini" in name_low:
            grp = "Gemini"
        elif "3p" in b_key or "claude" in name_low or "gpt" in name_low:
            grp = "Claude / 3P"
        else:
            grp = raw_name
    return grp, short_lbl


def format_account_quota_lines(
    quota_json: dict[str, int | float | str | list[int] | dict[str, dict[str, int | float | str]]] | None,
    lex: UiLexicon,
) -> list[str]:
    """Dynamically renders all quota groups and bars returned by `agy /usage` without hardcoded bucket keys."""
    if not quota_json or "buckets" not in quota_json:
        return []
    raw_buckets = quota_json["buckets"]
    if not isinstance(raw_buckets, dict) or not raw_buckets:
        return []

    grouped_bars: dict[str, list[str]] = {}
    for b_key, b_val in raw_buckets.items():
        if not isinstance(b_val, dict):
            continue
        grp, formatted_bar = _format_dynamic_bucket_entry(b_key, b_val, lex)
        if grp not in grouped_bars:
            grouped_bars[grp] = []
        grouped_bars[grp].append(formatted_bar)

    out: list[str] = []
    total_groups = len(grouped_bars)
    for idx, (grp_name, bars) in enumerate(grouped_bars.items(), start=1):
        branch = "└─" if idx == total_groups else "├─"
        icon = "🤖" if ("claude" in grp_name.lower() or "3p" in grp_name.lower()) else "🧠"
        out.append(f"    {branch} {icon} **{grp_name}:** " + " │ ".join(bars))
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
