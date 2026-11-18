"""Unified Telegram Native RichMessage (MTProto Layer 227+) and Markdown Extraction Engine."""

from typing import Final

from telethon.tl.custom import Message

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


def normalize_latex_outside_code(line: str) -> str:
    """Passthrough for native LaTeX / Markdown rendering without hardcoded symbol tables."""
    return line


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
