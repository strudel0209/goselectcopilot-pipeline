"""Cross-segment tag repair.

Do not fix OCR homoglyphs with prompting - constrain them with a lexicon. The
lexicon already exists: the same equipment tags appear in the schedule, in clean
tabular text. Harvesting it is the payoff for processing a package as one job
rather than N independent files.

Two mechanisms, in order:

1. **Confusion-class signature.** ``VFD-401`` misread as ``150-401`` is edit
   distance 3, so naive fuzzy matching cannot repair it safely. Mapping each
   character to its OCR confusion class collapses both strings to the same
   signature, giving an exact, explainable match.
2. **Bounded fuzzy fallback**, only for short edit distances.

Both reject on ambiguity. Silently rewriting ``VFD-101`` to ``VFD-401`` because
they score 86%% is worse than leaving the value corrupt, because the corruption
is at least visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Observed on ABB drawings: the box around a tag is read as a radical sign.
LATEX_ARTEFACT = re.compile(r"\$?\\(?:sqrt|surd|text|mathrm)\s*\{([^}]*)\}\$?")
# A leading letter is required so numeric ranges such as "10-20" are not
# harvested as tags. The suffix may lead with a letter: the Howey one-line uses
# VFD-H1 / VFD-J4 alongside M-401, VFD-401, P-101A.
TAG_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{0,4}-[A-Z]{0,2}\d{1,5}[A-Z]?\b")

# Characters an OCR engine interchanges. Each group collapses to its first member.
CONFUSION_GROUPS: tuple[str, ...] = (
    "0ODQ",
    "1ILV|",
    "5SF",
    "8B",
    "2Z",
    "6G",
    "7T",
    "4A",
    "9g",
)
_CLASS_OF: dict[str, str] = {
    char: group[0] for group in CONFUSION_GROUPS for char in group
}


def normalise(raw: str) -> str:
    """Strip LaTeX corruption and layout noise, leaving a bare candidate tag."""
    text = LATEX_ARTEFACT.sub(r"\1", raw or "")
    text = text.replace("$", "").replace("\\", "")
    text = re.sub(r"\s+", "", text)
    return text.strip().upper()


def signature(tag: str) -> str:
    """Collapse a tag into its OCR confusion signature."""
    return "".join(_CLASS_OF.get(c, c) for c in tag.upper())


def harvest(texts: Iterable[str]) -> set[str]:
    return {m for text in texts for m in TAG_PATTERN.findall(text.upper())}


@dataclass(frozen=True)
class Repair:
    raw: str
    value: str
    confidence: float
    method: str
    ambiguous_with: tuple[str, ...] = ()

    @property
    def repaired(self) -> bool:
        return self.method != "unchanged" and self.value != normalise(self.raw)


@dataclass
class TagLexicon:
    """Authoritative tags, harvested from the segments that render them cleanly."""

    tags: set[str] = field(default_factory=set)
    _by_signature: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self._by_signature = {}
        for tag in self.tags:
            self._by_signature.setdefault(signature(tag), []).append(tag)
        for candidates in self._by_signature.values():
            candidates.sort()

    def add(self, tags: Iterable[str]) -> None:
        self.tags |= {t.upper() for t in tags}
        self.reindex()

    def snap(self, raw: str, max_edits: int = 1, fuzzy_floor: int = 92) -> Repair:
        candidate = normalise(raw)
        if not candidate:
            return Repair(raw, candidate, 0.0, "unchanged")

        if candidate in self.tags:
            return Repair(raw, candidate, 1.0, "exact")

        matches = self._by_signature.get(signature(candidate), [])
        if len(matches) == 1:
            return Repair(raw, matches[0], 0.95, "confusion-class")
        if len(matches) > 1:
            return Repair(raw, candidate, 0.0, "ambiguous", tuple(matches))

        return self._fuzzy(raw, candidate, max_edits, fuzzy_floor)

    def _fuzzy(self, raw: str, candidate: str, max_edits: int, floor: int) -> Repair:
        try:
            from rapidfuzz import fuzz, process
        except ImportError:  # pragma: no cover - optional at runtime
            return Repair(raw, candidate, 0.0, "unchanged")

        if not self.tags:
            return Repair(raw, candidate, 0.0, "unchanged")

        scored = process.extract(
            candidate, sorted(self.tags), scorer=fuzz.ratio, limit=2, score_cutoff=floor
        )
        if not scored:
            return Repair(raw, candidate, 0.0, "unchanged")
        if len(scored) > 1 and abs(scored[0][1] - scored[1][1]) < 5:
            return Repair(raw, candidate, 0.0, "ambiguous", (scored[0][0], scored[1][0]))

        best, score, _ = scored[0]
        if _edit_distance(candidate, best) > max_edits:
            return Repair(raw, candidate, 0.0, "unchanged")
        return Repair(raw, best, round(score / 100, 3), "fuzzy")


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def repair_all(lexicon: TagLexicon, raws: Iterable[str]) -> list[Repair]:
    return [lexicon.snap(raw) for raw in raws]
