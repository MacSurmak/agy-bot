"""Unified Telegram Native RichMessage (MTProto Layer 227+) and Markdown Extraction Engine."""

import random
import re
from typing import Final

from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.tl.custom import Button, Message
from telethon.tl.types import TypeMessageEntity

type ButtonLayout = list[list[Button]] | None
type TlTreeVal = (
    str
    | int
    | float
    | bool
    | None
    | list[TlTreeVal]
    | dict[str, TlTreeVal]
)
type TlDict = dict[str, TlTreeVal]

WIDE_BAR_TARGET_COLS: Final[int] = 92
WIDE_BAR_LANG: Final[str] = "\u200b"

_TEXT_WRAP_TAGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("TextBold", "**", "**"),
    ("TextItalic", "*", "*"),
    ("TextFixed", "`", "`"),
    ("TextCode", "`", "`"),
    ("TextStrike", "~~", "~~"),
)


_MIN_SINGLE_LINE_FENCE_CHARS: Final[int] = 6
_BRACKET_DISPLAY_MATH_RE: Final[re.Pattern[str]] = re.compile(
    r"\\\[(.*?)\\\]", re.DOTALL
)
_BRACKET_INLINE_MATH_RE: Final[re.Pattern[str]] = re.compile(r"\\\((.*?)\\\)")
_CURRENCY_DOLLAR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)\$(\d+(?:[.,]\d+)?)\b(?![^$\n]*\$)"
)
_BARE_LATEX_CMD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\\)(\\[A-Za-z]{2,}(?:\{[^{}\n]*\})?(?:\^[0-9A-Za-z]+|_[0-9A-Za-z]+)?)"
)
_INTRAWORD_UNDERSCORE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[0-9A-Za-zА-Яа-яЁё°])_(?=[0-9A-Za-zА-Яа-яЁё])"
)
_MATH_SPLIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(\$\$[^$]+\$\$|\$[^$\n]+\$)"
)


def _normalize_non_math_text(segment: str) -> str:
    """Normalizes currency dollars, bare LaTeX commands, and snake_case underscores outside $...$."""
    escaped_currency = _CURRENCY_DOLLAR_RE.sub(r"\\$\1", segment)
    wrapped_bare_latex = _BARE_LATEX_CMD_RE.sub(r"$\1$", escaped_currency)
    sub_parts = _MATH_SPLIT_RE.split(wrapped_bare_latex)
    for idx in range(0, len(sub_parts), 2):
        sub_parts[idx] = _INTRAWORD_UNDERSCORE_RE.sub(r"\_", sub_parts[idx])
    return "".join(sub_parts)


def normalize_latex_outside_code(line: str) -> str:
    """Normalizes LaTeX delimiters and bare commands outside inline code spans without symbol dictionaries."""
    converted = _BRACKET_DISPLAY_MATH_RE.sub(r"$$\1$$", line)
    converted = _BRACKET_INLINE_MATH_RE.sub(r"$\1$", converted)
    code_parts = converted.split("`")
    for idx in range(0, len(code_parts), 2):
        math_parts = _MATH_SPLIT_RE.split(code_parts[idx])
        for m_idx in range(0, len(math_parts), 2):
            math_parts[m_idx] = _normalize_non_math_text(math_parts[m_idx])
        code_parts[idx] = "".join(math_parts)
    return "`".join(code_parts)


def prepare_rich_markdown(raw_md: str) -> str:
    """Prepares multi-line GFM and LaTeX Markdown for Telegram Native RichMessage rendering."""
    out_lines: list[str] = []
    in_fence = False
    for line in raw_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            is_single_line_fence = (
                stripped != "```"
                and stripped.endswith("```")
                and len(stripped) > _MIN_SINGLE_LINE_FENCE_CHARS
            )
            if not is_single_line_fence:
                in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        out_lines.append(normalize_latex_outside_code(line))
    return "\n".join(out_lines)


def make_wide_single_line_bar(text: str) -> str:
    """Formats a single-line status/header bar padded with non-breaking spaces."""
    clean = text.replace("**", "").replace("`", "").strip()
    parts = [
        ln.strip()
        for ln in clean.splitlines()
        if ln.strip() and not ln.strip().startswith("─")
    ]
    single_line = "  │  ".join(parts) if parts else ""
    if len(single_line) >= WIDE_BAR_TARGET_COLS:
        return single_line + "\u2060"
    pad_count = max(1, WIDE_BAR_TARGET_COLS - len(single_line))
    return single_line + ("\u00a0" * pad_count) + "\u2060"


def _get_dict(mapping: TlDict, key: str) -> TlDict | None:
    if key not in mapping:
        return None
    val = mapping[key]
    return val if isinstance(val, dict) else None


def _get_list(mapping: TlDict, key: str) -> list[TlTreeVal] | None:
    if key not in mapping:
        return None
    val = mapping[key]
    return val if isinstance(val, list) else None


def _get_str(mapping: TlDict, key: str) -> str:
    if key not in mapping:
        return ""
    val = mapping[key]
    return val if isinstance(val, str) else ""


def _render_rich_text_dict(node: TlTreeVal) -> str:
    """Recursively converts a serialized MTProto RichText dictionary into Markdown."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    cls_name = _get_str(node, "_")
    if cls_name == "TextEmpty":
        return ""
    if cls_name == "TextPlain":
        return _get_str(node, "text")
    if cls_name == "TextConcat":
        sub_texts = _get_list(node, "texts")
        if sub_texts is None:
            return ""
        return "".join(_render_rich_text_dict(t) for t in sub_texts)

    inner = _render_rich_text_dict(node["text"] if "text" in node else None)
    for tag_name, prefix, suffix in _TEXT_WRAP_TAGS:
        if cls_name == tag_name:
            return f"{prefix}{inner}{suffix}" if inner else ""

    if cls_name == "TextUrl":
        url_val = _get_str(node, "url")
        return f"[{inner}]({url_val})" if url_val else inner
    return inner


def _render_list_block_dict(block: TlDict, ordered: bool) -> str:
    raw_items = _get_list(block, "items")
    if raw_items is None:
        return ""
    out_items: list[str] = []
    for idx, item in enumerate(raw_items, start=1):
        prefix = f"{idx}. " if ordered else "- "
        if isinstance(item, dict):
            sub_blocks = _get_list(item, "blocks")
            if sub_blocks is not None:
                val = "\n".join(
                    _render_page_block_dict(sb)
                    for sb in sub_blocks
                    if isinstance(sb, dict)
                ).strip()
            else:
                val = _render_rich_text_dict(
                    item["text"] if "text" in item else None
                ).strip()
        else:
            val = _render_rich_text_dict(item).strip()
        out_items.append(f"{prefix}{val}")
    return "\n".join(out_items)


def _render_table_block_dict(block: TlDict) -> str:
    raw_rows = _get_list(block, "rows")
    if raw_rows is None:
        return ""
    md_rows: list[str] = []
    for r_idx, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            continue
        cells = _get_list(row, "cells")
        if cells is None:
            continue
        cell_strs = [
            _render_rich_text_dict(
                c["text"] if isinstance(c, dict) and "text" in c else None
            )
            .replace("\n", " ")
            .strip()
            for c in cells
        ]
        if cell_strs:
            md_rows.append("| " + " | ".join(cell_strs) + " |")
            if r_idx == 0:
                md_rows.append("| " + " | ".join("---" for _ in cell_strs) + " |")
    return "\n".join(md_rows)


def _render_page_block_dict(block: TlDict) -> str:
    """Converts a serialized MTProto PageBlock dictionary into clean GFM Markdown."""
    cls_name = _get_str(block, "_")
    text_node = block["text"] if "text" in block else None

    if cls_name.startswith("PageBlockHeading") or cls_name in (
        "PageBlockTitle",
        "PageBlockSubtitle",
        "PageBlockHeader",
        "PageBlockSubheader",
    ):
        txt = _render_rich_text_dict(text_node).strip()
        return f"### {txt}" if txt else ""

    if cls_name in ("PageBlockParagraph", "PageBlockKicker", "PageBlockFooter"):
        return _render_rich_text_dict(text_node).strip()

    if cls_name == "PageBlockPreformatted":
        code_txt = _render_rich_text_dict(text_node)
        lang = _get_str(block, "language").strip()
        return f"```{lang}\n{code_txt}\n```"

    if cls_name in ("PageBlockBlockquote", "PageBlockPullquote"):
        q_txt = _render_rich_text_dict(text_node).strip()
        return "\n".join(f"> {ln}" for ln in q_txt.splitlines())

    if cls_name in ("PageBlockList", "PageBlockOrderedList"):
        return _render_list_block_dict(block, cls_name == "PageBlockOrderedList")

    if cls_name == "PageBlockTable":
        return _render_table_block_dict(block)

    if cls_name == "PageBlockDivider":
        return "---"

    sub_blocks = _get_list(block, "blocks")
    if sub_blocks is None:
        return ""
    inner_md = "\n\n".join(
        b_str
        for b_str in (
            _render_page_block_dict(sb) for sb in sub_blocks if isinstance(sb, dict)
        )
        if b_str
    )
    if cls_name == "PageBlockDetails" and "title" in block:
        title = _render_rich_text_dict(block["title"]).strip()
        return f"### {title}\n{inner_md}" if title else inner_md
    return inner_md


def _find_rich_dict(msg_dict: TlDict) -> TlDict | None:
    direct = _get_dict(msg_dict, "rich_message")
    if direct is not None:
        return direct
    media_dict = _get_dict(msg_dict, "media")
    if media_dict is None:
        return None
    media_rich = _get_dict(media_dict, "rich_message")
    if media_rich is not None:
        return media_rich
    wp_dict = _get_dict(media_dict, "webpage")
    if wp_dict is not None:
        return _get_dict(wp_dict, "cached_page")
    return None


def extract_incoming_message_markdown(message: Message) -> str:
    """Extracts user text from both standard `message.message` and Telegram 2026 `rich_message`."""
    raw_dict = message.to_dict()
    msg_dict: TlDict = raw_dict if isinstance(raw_dict, dict) else {}
    standard_text = _get_str(msg_dict, "message").strip()

    rich_dict = _find_rich_dict(msg_dict)
    if rich_dict is None:
        return standard_text

    blocks = _get_list(rich_dict, "blocks")
    if blocks is None:
        return standard_text

    rendered_blocks = [
        b_md
        for b_md in (
            _render_page_block_dict(b) for b in blocks if isinstance(b, dict)
        )
        if b_md
    ]
    if not rendered_blocks:
        return standard_text

    rich_md = "\n\n".join(rendered_blocks).strip()
    if standard_text and standard_text not in rich_md:
        return f"{standard_text}\n\n{rich_md}"
    return rich_md


def supports_native_rich_output() -> bool:
    """Returns True if the installed Telethon MTProto schema provides InputRichMessageMarkdown."""
    return hasattr(types, "InputRichMessageMarkdown")


def _extract_sent_message_from_updates(
    client: TelegramClient,
    updates_obj: types.TypeUpdates,
    chat_id: int,
) -> Message | None:
    _ = (client, chat_id)
    raw_updates = (
        updates_obj.updates if hasattr(updates_obj, "updates") else []
    )
    for upd in raw_updates:
        if hasattr(upd, "message"):
            raw_m = upd.message
            if isinstance(raw_m, types.Message):
                return Message(
                    id=raw_m.id,
                    peer_id=raw_m.peer_id,
                    date=raw_m.date,
                    message=raw_m.message,
                    out=True,
                )
    return None


async def send_rich_or_standard_message(
    client: TelegramClient,
    chat_id: int,
    markdown_text: str,
    fallback_text: str,
    fallback_entities: list[TypeMessageEntity] | None,
    reply_to: int | None,
    silent: bool,
    buttons: ButtonLayout,
) -> Message:
    """Sends a native Telegram RichMessage when supported by MTProto layer, or standard formatted message."""
    if supports_native_rich_output():
        rich_cls = getattr(types, "InputRichMessageMarkdown")
        prepared_md = prepare_rich_markdown(markdown_text)
        peer = await client.get_input_entity(chat_id)
        markup = client.build_reply_markup(buttons) if buttons is not None else None
        reply_hdr = (
            types.InputReplyToMessage(reply_to_msg_id=reply_to)
            if reply_to is not None
            else None
        )
        req = functions.messages.SendMessageRequest(
            peer=peer,
            message=fallback_text[:4000],
            random_id=random.randrange(-2**63, 2**63),
            reply_to=reply_hdr,
            silent=silent,
            reply_markup=markup,
            entities=None,
            rich_message=rich_cls(markdown=prepared_md[:32768]),
        )
        updates_res = await client(req)
        extracted = _extract_sent_message_from_updates(client, updates_res, chat_id)
        if extracted is not None:
            return extracted

    return await client.send_message(
        entity=chat_id,
        message=fallback_text,
        formatting_entities=fallback_entities,
        reply_to=reply_to,
        silent=silent,
        buttons=buttons,
    )


async def edit_rich_or_standard_message(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    markdown_text: str,
    fallback_text: str,
    fallback_entities: list[TypeMessageEntity] | None,
    buttons: ButtonLayout,
) -> None:
    """Edits an existing message in-place with native RichMessage when supported by MTProto layer."""
    if supports_native_rich_output():
        rich_cls = getattr(types, "InputRichMessageMarkdown")
        prepared_md = prepare_rich_markdown(markdown_text)
        peer = await client.get_input_entity(chat_id)
        markup = client.build_reply_markup(buttons) if buttons is not None else None
        req = functions.messages.EditMessageRequest(
            peer=peer,
            id=message_id,
            message=fallback_text[:4000],
            reply_markup=markup,
            entities=None,
            rich_message=rich_cls(markdown=prepared_md[:32768]),
        )
        await client(req)
        return

    await client.edit_message(
        entity=chat_id,
        message=message_id,
        text=fallback_text,
        formatting_entities=fallback_entities,
        buttons=buttons,
    )
