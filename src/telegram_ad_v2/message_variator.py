from __future__ import annotations

import random
import re

ZW_CHARS = [
    "\u200b",
    "\u200c",
    "\u200d",
    "\u200e",
    "\u200f",
    "\u2060",
    "\u2061",
    "\u2062",
    "\u2063",
    "\u2064",
    "\ufeff",
]

SPIN_PATTERN = re.compile(r"\{([^{}]+)\}")


def spin_text(template: str) -> str:
    def _pick(m: re.Match) -> str:
        options = m.group(1).split("|")
        return random.choice(options)

    return SPIN_PATTERN.sub(_pick, template)


def add_zero_width_noise(text: str, max_chars: int = 3) -> str:
    if not text:
        return text
    result = list(text)
    for _ in range(random.randint(0, max_chars)):
        pos = random.randint(0, len(result))
        result.insert(pos, random.choice(ZW_CHARS))
    return "".join(result)


def vary_unicode(text: str) -> str:
    homoglyphs = {
        "a": ["\u0430"],
        "e": ["\u0435"],
        "o": ["\u043e", "\u03bf"],
        "p": ["\u0440"],
        "c": ["\u0441"],
        "x": ["\u0445"],
        "y": ["\u0443"],
        "A": ["\u0410"],
        "B": ["\u0412"],
        "E": ["\u0415"],
        "H": ["\u041d"],
        "K": ["\u041a"],
        "M": ["\u041c"],
        "O": ["\u041e"],
        "P": ["\u0420"],
        "T": ["\u0422"],
        "X": ["\u0425"],
    }

    result = list(text)
    num_swaps = max(1, len(result) // 8)
    for _ in range(num_swaps):
        idx = random.randint(0, len(result) - 1)
        ch = result[idx]
        if ch in homoglyphs:
            result[idx] = random.choice(homoglyphs[ch])
    return "".join(result)


def generate_variant(template: str, noise: bool = True, unicode_vary: bool = True) -> str:
    text = spin_text(template)
    if unicode_vary:
        text = vary_unicode(text)
    if noise:
        text = add_zero_width_noise(text)
    return text


class MessageVariator:
    def __init__(
        self,
        variants: list[str] | None = None,
        enable_spin: bool = True,
        enable_noise: bool = True,
        enable_unicode: bool = True,
    ) -> None:
        self.variants = variants or []
        self.enable_spin = enable_spin
        self.enable_noise = enable_noise
        self.enable_unicode = enable_unicode
        self._idx = 0

    def next(self) -> str | None:
        if not self.variants:
            return None
        template = self.variants[self._idx % len(self.variants)]
        self._idx += 1
        return generate_variant(
            template,
            noise=self.enable_noise,
            unicode_vary=self.enable_unicode,
        )

    def reset(self) -> None:
        self._idx = 0
