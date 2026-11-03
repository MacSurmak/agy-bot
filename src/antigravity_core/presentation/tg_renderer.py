"""Unified Telegram Rich-Text, LaTeX-to-Unicode, GFM Table, and Single-Line Wide Banner Engine."""

import re

WIDE_BAR_TARGET_COLS: int = 92
# Zero-width space forces Telegram Desktop to render a 1-line block-level <pre> container
# without printing 'copy'/'stats' in the header and without needing a second line of dashes.
WIDE_BAR_LANG: str = "\u200b"

_LATEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    (r"\longrightarrow", "→"),
    (r"\longleftarrow", "←"),
    (r"\longleftrightarrow", "↔"),
    (r"\Longrightarrow", "⇒"),
    (r"\Longleftarrow", "⇐"),
    (r"\Longleftrightarrow", "⇔"),
    (r"\rightarrow", "→"),
    (r"\leftarrow", "←"),
    (r"\leftrightarrow", "↔"),
    (r"\Rightarrow", "⇒"),
    (r"\Leftarrow", "⇐"),
    (r"\Leftrightarrow", "⇔"),
    (r"\mapsto", "↦"),
    (r"\uparrow", "↑"),
    (r"\downarrow", "↓"),
    (r"\implies", "⟹"),
    (r"\impliedby", "⟸"),
    (r"\to", "→"),
    (r"\gets", "←"),
    (r"\iff", "⇔"),
    (r"\approx", "≈"),
    (r"\simeq", "≃"),
    (r"\equiv", "≡"),
    (r"\neq", "≠"),
    (r"\ne", "≠"),
    (r"\leq", "≤"),
    (r"\le", "≤"),
    (r"\geq", "≥"),
    (r"\ge", "≥"),
    (r"\ll", "≪"),
    (r"\gg", "≫"),
    (r"\pm", "±"),
    (r"\mp", "∓"),
    (r"\times", "×"),
    (r"\cdot", "·"),
    (r"\div", "÷"),
    (r"\infty", "∞"),
    (r"\propto", "∝"),
    (r"\partial", "∂"),
    (r"\nabla", "∇"),
    (r"\forall", "∀"),
    (r"\exists", "∃"),
    (r"\notin", "∉"),
    (r"\in", "∈"),
    (r"\subset", "⊂"),
    (r"\subseteq", "⊆"),
    (r"\cup", "∪"),
    (r"\cap", "∩"),
    (r"\emptyset", "∅"),
    (r"\sum", "∑"),
    (r"\prod", "∏"),
    (r"\int", "∫"),
    (r"\sqrt", "√"),
    (r"^\circ", "°"),
    (r"\circ", "°"),
    (r"\alpha", "α"),
    (r"\beta", "β"),
    (r"\gamma", "γ"),
    (r"\delta", "δ"),
    (r"\varepsilon", "ε"),
    (r"\epsilon", "ε"),
    (r"\zeta", "ζ"),
    (r"\eta", "η"),
    (r"\theta", "θ"),
    (r"\iota", "ι"),
    (r"\kappa", "κ"),
    (r"\lambda", "λ"),
    (r"\mu", "μ"),
    (r"\nu", "ν"),
    (r"\xi", "ξ"),
    (r"\pi", "π"),
    (r"\rho", "ρ"),
    (r"\sigma", "σ"),
    (r"\tau", "τ"),
    (r"\varphi", "φ"),
    (r"\phi", "φ"),
    (r"\chi", "χ"),
    (r"\psi", "ψ"),
    (r"\omega", "ω"),
    (r"\Gamma", "Γ"),
    (r"\Delta", "Δ"),
    (r"\Theta", "Θ"),
    (r"\Lambda", "Λ"),
    (r"\Xi", "Ξ"),
    (r"\Pi", "Π"),
    (r"\Sigma", "Σ"),
    (r"\Phi", "Φ"),
    (r"\Psi", "Ψ"),
    (r"\Omega", "Ω"),
    (r"\sim", "~"),
)

_SUPERSCRIPT_MAP: dict[str, str] = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "n": "ⁿ",
    "i": "ⁱ",
}

_SUBSCRIPT_MAP: dict[str, str] = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
}

_BB_MAP: dict[str, str] = {
    "R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "E": "𝔼"
}
_CAL_MAP: dict[str, str] = {
    "O": "𝒪", "L": "ℒ", "H": "ℋ", "F": "ℱ", "D": "𝒟", "N": "𝒩"
}

_LATEX_BB_RE = re.compile(r"\\mathbb\{([A-Za-z])\}")
_LATEX_CAL_RE = re.compile(r"\\mathcal\{([A-Za-z])\}")
_LATEX_TEXT_MACRO_RE = re.compile(r"\\(?:text|mathrm|mathbf|mathit|mathsf|texttt)\{([^{}]*)\}")
_LATEX_FRAC_RE = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
_LATEX_SUPER_BRACE_RE = re.compile(r"\^\{([0-9+\-=()ni]+)\}")
_LATEX_SUPER_SINGLE_RE = re.compile(r"\^([0-9ni])")
_LATEX_SUB_BRACE_RE = re.compile(r"_\{([0-9+\-=()aehijklmnoprstuvx]+)\}")
_LATEX_SUB_SINGLE_RE = re.compile(r"_([0-9aehijklmnoprstuvx])\b")
_INLINE_MATH_DOLLAR_RE = re.compile(r"\$\$([^$]+)\$\$|\$([^$\n]+)\$")


def _convert_math_fragment(expr: str) -> str:
    res = expr.strip()
    res = _LATEX_BB_RE.sub(lambda m: _BB_MAP.get(m.group(1), m.group(1)), res)
    res = _LATEX_CAL_RE.sub(lambda m: _CAL_MAP.get(m.group(1), m.group(1)), res)
    res = _LATEX_TEXT_MACRO_RE.sub(r"\1", res)
    res = _LATEX_FRAC_RE.sub(r"(\1/\2)", res)

    for latex_cmd, uni_char in _LATEX_SYMBOLS:
        res = res.replace(latex_cmd, uni_char)

    res = _LATEX_SUPER_BRACE_RE.sub(
        lambda m: "".join(_SUPERSCRIPT_MAP.get(c, c) for c in m.group(1)),
        res,
    )
    res = _LATEX_SUPER_SINGLE_RE.sub(
        lambda m: _SUPERSCRIPT_MAP.get(m.group(1), m.group(0)),
        res,
    )
    res = _LATEX_SUB_BRACE_RE.sub(
        lambda m: "".join(_SUBSCRIPT_MAP.get(c, c) for c in m.group(1)),
        res,
    )
    res = _LATEX_SUB_SINGLE_RE.sub(
        lambda m: _SUBSCRIPT_MAP.get(m.group(1), m.group(0)),
        res,
    )
    res = res.replace(r"\{", "{").replace(r"\}", "}").replace(r"\%", "%")
    res = re.sub(r"\\(?:quad|qquad|,|;|!)\b", " ", res)
    return res


def normalize_latex_outside_code(line: str) -> str:
    """Converts inline $...$ LaTeX math and bare LaTeX arrows/Greek symbols outside inline `code`."""
    segments = line.split("`")
    for idx in range(0, len(segments), 2):
        seg = segments[idx]
        seg = _INLINE_MATH_DOLLAR_RE.sub(
            lambda m: _convert_math_fragment(m.group(1) or m.group(2) or ""),
            seg,
        )
        # Also convert bare \rightarrow, \approx, etc. if the LLM omitted $...$
        if "\\" in seg:
            seg = _LATEX_TEXT_MACRO_RE.sub(r"\1", seg)
            for latex_cmd, uni_char in _LATEX_SYMBOLS:
                if latex_cmd in seg:
                    seg = seg.replace(latex_cmd, uni_char)
        segments[idx] = seg
    return "`".join(segments)


def make_wide_single_line_bar(text: str) -> str:
    """Formats a single-line status/header bar padded with invisible non-breaking spaces (zero dashes!)."""
    clean = text.replace("**", "").replace("`", "").strip()
    parts = [ln.strip() for ln in clean.splitlines() if ln.strip() and not ln.strip().startswith("─")]
    single_line = "  │  ".join(parts) if parts else ""
    if len(single_line) >= WIDE_BAR_TARGET_COLS:
        return single_line
    pad_count = max(1, WIDE_BAR_TARGET_COLS - len(single_line))
    return single_line + ("\u00a0" * pad_count)
