from typing import Final

from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.lexicon.en import EN_LEXICON
from antigravity_core.lexicon.ru import RU_LEXICON
from antigravity_core.lexicon.schema import UiLexicon

_LEXICON_PAIRS: Final[tuple[tuple[str, UiLexicon], ...]] = (
    ("ru", RU_LEXICON),
    ("en", EN_LEXICON),
)

SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("ru", "en")


def get_lexicon(lang_code: str) -> UiLexicon:
    normalized = lang_code.strip().lower()
    for code, lex in _LEXICON_PAIRS:
        if code == normalized:
            return lex
    raise ConfigurationError(
        f"Unsupported UI language '{lang_code}'. Supported languages: {SUPPORTED_LANGUAGES}"
    )
