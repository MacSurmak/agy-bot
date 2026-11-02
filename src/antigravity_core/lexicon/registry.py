from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.lexicon.en import EN_LEXICON
from antigravity_core.lexicon.ru import RU_LEXICON
from antigravity_core.lexicon.schema import UiLexicon

LEXICONS: dict[str, UiLexicon] = {
    "ru": RU_LEXICON,
    "en": EN_LEXICON,
}

SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(LEXICONS.keys())


def get_lexicon(lang_code: str) -> UiLexicon:
    normalized = lang_code.strip().lower()
    lexicon = LEXICONS.get(normalized)
    if lexicon is None:
        raise ConfigurationError(
            f"Unsupported UI language '{lang_code}'. Supported languages: {SUPPORTED_LANGUAGES}"
        )
    return lexicon
