"""Strictly typed multilingual UI lexicon registry for Telegram presentation and notifications."""

from antigravity_core.lexicon.registry import SUPPORTED_LANGUAGES, get_lexicon
from antigravity_core.lexicon.schema import UiLexicon

__all__ = ["SUPPORTED_LANGUAGES", "UiLexicon", "get_lexicon"]
