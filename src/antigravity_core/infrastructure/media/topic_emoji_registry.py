"""Registry for custom Telegram Forum Topic emoji packs and emoji-to-document-ID resolution."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loguru import logger
from telethon import TelegramClient
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import (
    Document,
    DocumentAttributeCustomEmoji,
    DocumentAttributeSticker,
    InputStickerSetShortName,
)
from telethon.tl.types.messages import StickerSet

TOPIC_EMOJI_CONFIG_PATH: Final[Path] = Path(
    "/home/agy-agent/.gemini/topic_emoji_packs.json"
)
USER_EMOJI_SESSION_FILENAME: Final[str] = "user_emoji.session"
_VARIATION_SELECTOR_16: Final[str] = "\ufe0f"


@dataclass(frozen=True, slots=True)
class TopicEmojiSpec:
    emoji: str
    label: str
    document_id: int | None


def normalize_emoji_key(raw_emoji: str) -> str:
    """Normalizes an emoji string by stripping whitespace and variation selector-16."""
    return raw_emoji.strip().replace(_VARIATION_SELECTOR_16, "")


def load_topic_emoji_config_specs(config_path: Path) -> tuple[TopicEmojiSpec, ...]:
    """Reads configured topic emoji specs from JSON without requiring a Telegram client."""
    if not config_path.is_file():
        return ()
    raw_text = config_path.read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict) or "icons" not in parsed:
        return ()
    raw_icons = parsed["icons"]
    if not isinstance(raw_icons, dict):
        return ()
    specs: list[TopicEmojiSpec] = []
    for emoji_key, entry in raw_icons.items():
        if not isinstance(emoji_key, str) or not emoji_key.strip():
            continue
        if isinstance(entry, str):
            specs.append(
                TopicEmojiSpec(
                    emoji=emoji_key.strip(),
                    label=entry.strip(),
                    document_id=None,
                )
            )
        elif isinstance(entry, dict):
            label_val = str(entry["label"]).strip() if "label" in entry else ""
            doc_id_val: int | None = None
            if "document_id" in entry and entry["document_id"] is not None:
                doc_id_val = int(entry["document_id"])
            specs.append(
                TopicEmojiSpec(
                    emoji=emoji_key.strip(),
                    label=label_val,
                    document_id=doc_id_val,
                )
            )
    return tuple(specs)


def _load_pack_short_names(config_path: Path) -> tuple[str, ...]:
    if not config_path.is_file():
        return ()
    raw_text = config_path.read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict) or "pack_short_names" not in parsed:
        return ()
    raw_names = parsed["pack_short_names"]
    if not isinstance(raw_names, list):
        return ()
    return tuple(
        str(name).strip() for name in raw_names if isinstance(name, str) and name.strip()
    )


class TopicEmojiRegistry:
    """Loads and resolves custom Telegram emoji document IDs for Forum Topic icons."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._normalized_to_id: dict[str, int] = {}
        self._display_emojis: list[str] = []

    async def load_from_telegram(self, client: TelegramClient) -> int:
        """Hydrates emoji -> document_id mappings from config and Telegram custom emoji packs."""
        self._normalized_to_id.clear()
        self._display_emojis.clear()

        specs = load_topic_emoji_config_specs(self._config_path)
        for spec in specs:
            if spec.document_id is not None:
                norm_key = normalize_emoji_key(spec.emoji)
                if norm_key and norm_key not in self._normalized_to_id:
                    self._normalized_to_id[norm_key] = spec.document_id
                    self._display_emojis.append(spec.emoji)

        pack_names = _load_pack_short_names(self._config_path)
        for pack_name in pack_names:
            sticker_set = await client(
                GetStickerSetRequest(
                    stickerset=InputStickerSetShortName(short_name=pack_name),
                    hash=0,
                )
            )
            if not isinstance(sticker_set, StickerSet):
                continue
            for doc in sticker_set.documents:
                if not isinstance(doc, Document) or doc.id <= 0:
                    continue
                for attr in doc.attributes:
                    if isinstance(
                        attr,
                        (DocumentAttributeCustomEmoji, DocumentAttributeSticker),
                    ):
                        alt = attr.alt.strip()
                        norm_alt = normalize_emoji_key(alt)
                        if norm_alt and norm_alt not in self._normalized_to_id:
                            self._normalized_to_id[norm_alt] = int(doc.id)
                            self._display_emojis.append(alt)

        logger.info(
            "Loaded custom topic emoji registry",
            config_path=str(self._config_path),
            packs_count=len(pack_names),
            icons_count=len(self._normalized_to_id),
        )
        return len(self._normalized_to_id)

    def resolve_icon_id(self, emoji: str) -> int | None:
        """Resolves a Unicode emoji to its Telegram custom_emoji document_id if registered."""
        norm_key = normalize_emoji_key(emoji)
        if not norm_key or norm_key not in self._normalized_to_id:
            return None
        return self._normalized_to_id[norm_key]

    def available_emojis(self) -> tuple[str, ...]:
        """Returns all registered topic emojis in display order."""
        return tuple(self._display_emojis)
