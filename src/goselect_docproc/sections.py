"""Section index - the traceability fix.

Reference misattribution happens when code falls back to *nearest text above in
reading order*. On a two-column or landscape page that is the wrong section,
because DI's linearised reading order does not follow the visual layout.

The fix is **span-ordered anchoring**: index every heading by its span offset,
then attribute any element to the last heading whose offset precedes it. Binary
search, exact, no model call.

Three strategies, best first:

``di-sections``
    Document Intelligence's own ``sections`` tree. It supplies nesting depth
    directly, so heading level stops being a guess, and - critically - its root
    children are *separate documents stapled into one file*. On the Howey
    package the specification is one subtree (offsets 0-18684) and the drawing
    sheets are another (18708-21911). Attribution never crosses that line, which
    is what stops a drawing inheriting a specification clause.

``roles``
    Paragraphs tagged ``title`` or ``sectionHeading``, flat.

``geometry-fallback``
    Glyph height and boldness. Reports itself unreliable.

Note that DI's ``ocr.font`` add-on returns ``similarFontFamily``, ``fontStyle``,
``fontWeight``, ``color`` and ``backgroundColor`` - it does **not** return a font
size, so glyph height is derived from the bounding polygon.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Any

from . import geometry as geo
from .contracts import SectionNode
from .spans import overlaps

HEADING_ROLES = ("title", "sectionHeading")
NUMBERED = re.compile(r"^\s*(\d+(?:\.\d+)*)[\s.\-\u2013)]+\S")
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


def _level(text: str) -> int:
    match = NUMBERED.match(text or "")
    return match.group(1).count(".") + 1 if match else 1


def _clean(text: str) -> str:
    return BULLET.sub("", (text or "").strip()).strip()


def _is_substantive(text: str) -> bool:
    """DI tags decorative rules such as an em-dash as ``sectionHeading``."""
    return any(c.isalnum() for c in text)


def _looks_like_heading(text: str) -> bool:
    if not text or len(text) > MAX_HEADING_CHARS or not _is_substantive(text):
        return False
    if BULLET.match(text):
        return False
    return bool(NUMBERED.match(text)) or text.istitle() or text.isupper()


def _di_section_tree(result: Any) -> tuple[list[SectionNode], list[tuple[int, int]]]:
    """Walk DI's ``sections`` tree, returning headings and per-document boundaries.

    A single ``sections`` entry is the bare root: a drawing sheet has no document
    structure, and inventing headings for one is how handwriting becomes a clause.
    """
    sections = list(getattr(result, "sections", None) or [])
    if len(sections) < 2:
        return [], []
    paragraphs = list(result.paragraphs or [])

    def ref_index(ref: str) -> int:
        return int(ref.rsplit("/", 1)[1])

    def heading_paragraph(section: Any) -> Any:
        for ref in section.elements or []:
            if ref.startswith("/paragraphs/"):
                i = ref_index(ref)
                return paragraphs[i] if i < len(paragraphs) else None
        return None

    nodes: list[SectionNode] = []

    def walk(index: int, level: int) -> None:
        if index >= len(sections):
            return
        section = sections[index]
        paragraph = heading_paragraph(section)
        if paragraph is not None and paragraph.spans and paragraph.bounding_regions:
            text = _clean(paragraph.content)
            if _is_substantive(text) and len(text) <= MAX_HEADING_CHARS:
                nodes.append(
                    SectionNode(
                        offset=paragraph.spans[0].offset,
                        heading=text,
                        page=paragraph.bounding_regions[0].page_number,
                        level=level,
                    )
                )
        for ref in section.elements or []:
            if ref.startswith("/sections/"):
                walk(ref_index(ref), level + 1)

    boundaries: list[tuple[int, int]] = []
    for ref in sections[0].elements or []:
        if not ref.startswith("/sections/"):
            continue
        index = ref_index(ref)
        walk(index, 1)
        spans = getattr(sections[index], "spans", None) or []
        if spans:
            boundaries.append((spans[0].offset, spans[0].offset + spans[0].length))

    return sorted(nodes, key=lambda n: n.offset), boundaries


def build_section_index(result: Any, min_role_headings: int = 3) -> SectionIndex:
    sections = list(getattr(result, "sections", None) or [])
    di_nodes, boundaries = _di_section_tree(result)
    if len(di_nodes) >= min_role_headings:
        return SectionIndex(
            nodes=di_nodes,
            strategy="di-sections",
            role_headings=len(di_nodes),
            boundaries=tuple(boundaries),
        )

    if sections and not di_nodes:
        # The service analysed structure and found none. Scanning roles anyway is
        # how every equipment label on a one-line diagram becomes a clause.
        return SectionIndex(nodes=[], strategy="di-sections-flat", role_headings=0)

    role_nodes = [
        SectionNode(
            offset=p.spans[0].offset,
            heading=_clean(p.content),
            page=p.bounding_regions[0].page_number,
            level=_level(p.content),
        )
        for p in (result.paragraphs or [])
        if p.role in HEADING_ROLES
        and p.spans
        and p.bounding_regions
        and _is_substantive(_clean(p.content))
    ]

    if len(role_nodes) >= min_role_headings:
        return SectionIndex(
            nodes=sorted(role_nodes, key=lambda n: n.offset),
            strategy="roles",
            role_headings=len(role_nodes),
        )

    return SectionIndex(
        nodes=_by_geometry(result),
        strategy="geometry-fallback",
        role_headings=len(role_nodes),
    )


def _by_geometry(result: Any) -> list[SectionNode]:
    bold_spans = [
        (s.offset, s.length)
        for style in (result.styles or [])
        if getattr(style, "font_weight", None) == "bold"
        for s in (style.spans or [])
    ]

    candidates = [
        p for p in (result.paragraphs or []) if p.spans and p.bounding_regions and p.content
    ]
    heights = sorted(
        geo.height(p.bounding_regions[0].polygon) for p in candidates if len(p.content) < 200
    )
    body_height = heights[len(heights) // 2] if heights else 0.0

    nodes: list[SectionNode] = []
    for p in candidates:
        text = _clean(p.content)
        if not _looks_like_heading(text):
            continue
        taller = geo.height(p.bounding_regions[0].polygon) > body_height * 1.15
        emphasised = overlaps([(s.offset, s.length) for s in p.spans], bold_spans)
        if taller or emphasised:
            nodes.append(
                SectionNode(
                    offset=p.spans[0].offset,
                    heading=text,
                    page=p.bounding_regions[0].page_number,
                    level=_level(text),
                )
            )
    return sorted(nodes, key=lambda n: n.offset)
