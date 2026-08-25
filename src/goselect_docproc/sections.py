"""Section index - the traceability fix.

Reference misattribution happens when code falls back to *nearest text above in
reading order*. On a two-column or landscape page that is the wrong section,
because a linearised reading order does not follow the visual layout.

The fix is **span-ordered anchoring**: index every heading by its span offset,
then attribute any element to the last heading whose offset precedes it. Binary
search, exact, no model call.

The walk over the service's ``sections`` tree lives here rather than inside a
producer because the policy is the shared part: nesting depth comes from the
tree instead of from counting dots in a numeral, and the tree's root children
are *separate documents stapled into one file*. Attribution never crosses that
line, which is what stops a drawing inheriting a specification clause.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from .contracts import ContentType, SectionNode

# Content that owns its identity. A sheet carries its own title block and is
# never a clause of whatever prose happens to precede it in the file.
SELF_TITLING = frozenset({ContentType.DRAWING, ContentType.PLAN})
# Leading bullets must never be promoted to headings.
BULLET = re.compile(r"^\s*[-\u2022\u00b7\u25cf\u25aa*\u2013\u2014]\s+")
MAX_HEADING_CHARS = 120


@dataclass(frozen=True)
class SectionIndex:
    nodes: list[SectionNode]
    strategy: str
    role_headings: int
    boundaries: tuple[tuple[int, int], ...] = ()
    """Half-open span ranges, one per independent document in the file."""

    @property
    def offsets(self) -> list[int]:
        return [n.offset for n in self.nodes]

    @property
    def reliable(self) -> bool:
        """Geometry fallback is a guess. Report scanned accuracy separately."""
        return self.strategy in {"di-sections", "roles"} and len(self.nodes) >= 3

    def _floor(self, offset: int) -> int:
        """Start of the document this offset belongs to; headings before it are another document's."""
        for start, end in self.boundaries:
            if start <= offset < end:
                return start
        return 0

    def path_for(self, offset: int) -> str | None:
        """Full breadcrumb for a character offset, expanded by heading level."""
        if not self.nodes:
            return None
        i = bisect.bisect_right(self.offsets, offset) - 1
        if i < 0:
            return None

        floor = self._floor(offset)
        crumbs: list[str] = []
        level: int | None = None
        for node in reversed(self.nodes[: i + 1]):
            if node.offset < floor:
                break
            if level is None or node.level < level:
                crumbs.append(node.heading)
                level = node.level
            if level == 1:
                break
        return " > ".join(reversed(crumbs)) or None

    def node_for(self, offset: int) -> SectionNode | None:
        if not self.nodes:
            return None
        i = bisect.bisect_right(self.offsets, offset) - 1
        if i < 0:
            return None
        node = self.nodes[i]
        return node if node.offset >= self._floor(offset) else None

    def root_for(self, offset: int, *, inherits: bool = True) -> str | None:
        """Breadcrumb for a whole segment.

        ``inherits=False`` for content that owns its identity: a drawing sheet is
        a separate document with its own title block, not a clause of whatever
        specification happens to precede it in the file. DI's own tree gets this
        right for some sheets and wrong for others - on the Howey package it split
        sheet E-07 out but folded E-08 into clause 3.05 - so the rule is enforced
        here rather than trusted upstream.
        """
        if inherits:
            return self.path_for(offset)
        node = self.node_for(offset)
        return node.heading if node and node.offset >= offset else None


def _clean(text: str) -> str:
    return BULLET.sub("", (text or "").strip()).strip()


def _is_substantive(text: str) -> bool:
    """DI tags decorative rules such as an em-dash as ``sectionHeading``."""
    return any(c.isalnum() for c in text)


# What a producer must reduce its native models to. Offsets are absolute into the
# one immutable content string; page is 1-based.
ParagraphRef = tuple[int, int, str]
SectionRef = tuple[tuple[int, int] | None, list[str]]


def tree_index(
    sections: list[SectionRef], paragraphs: list[ParagraphRef | None]
) -> tuple[list[SectionNode], list[tuple[int, int]]]:
    """Walk a service's ``sections`` tree into headings and document boundaries.

    Producer-neutral on purpose: Document Intelligence and Content Understanding
    return the same shape - a span plus JSON-pointer ``elements`` - under
    different attribute names, and the policy below is the part worth sharing.

    A single entry is the bare root: a drawing sheet has no document structure,
    and inventing headings for one is how handwriting becomes a clause.
    """
    if len(sections) < 2:
        return [], []

    def ref_index(ref: str) -> int:
        return int(ref.rsplit("/", 1)[1])

    def heading_of(elements: list[str]) -> ParagraphRef | None:
        for ref in elements:
            if ref.startswith("/paragraphs/"):
                i = ref_index(ref)
                return paragraphs[i] if i < len(paragraphs) else None
        return None

    nodes: list[SectionNode] = []

    def walk(index: int, level: int) -> None:
        if index >= len(sections):
            return
        _, elements = sections[index]
        paragraph = heading_of(elements)
        if paragraph is not None:
            offset, page, content = paragraph
            text = _clean(content)
            if _is_substantive(text) and len(text) <= MAX_HEADING_CHARS:
                nodes.append(
                    SectionNode(offset=offset, heading=text, page=page, level=level)
                )
        for ref in elements:
            if ref.startswith("/sections/"):
                walk(ref_index(ref), level + 1)

    boundaries: list[tuple[int, int]] = []
    for ref in sections[0][1]:
        if not ref.startswith("/sections/"):
            continue
        index = ref_index(ref)
        walk(index, 1)
        if index < len(sections):
            span = sections[index][0]
            if span:
                boundaries.append((span[0], span[0] + span[1]))

    return sorted(nodes, key=lambda n: n.offset), boundaries
