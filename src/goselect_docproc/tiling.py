"""Drawing tiling for vision extraction.

This is the root-cause fix for corrupted tags on CAD sheets.

Claude views images in 28x28 patches. Each model tier caps both the long edge
and the visual-token count, and **oversized images are downscaled before the
model ever sees them**:

===============  ==========================  =============  ==================
Resolution tier  Models                      Max long edge  Max visual tokens
===============  ==========================  =============  ==================
High-resolution  Claude 4.7 and later        2576 px        4784
Standard         all other models (inc. 4.5) 1568 px        1568
===============  ==========================  =============  ==================

An E-size sheet scanned at 300 DPI is ~13200 px on the long edge. Sent whole to
a standard-tier model it is scaled by ~0.12, so 10 pt tag text (~42 px) arrives
about **5 px tall** - below the threshold at which any model reads characters
reliably. That is the mechanism behind ``V->1, F->5, D->0``; it is not an OCR
fault, and raising DI's OCR resolution cannot fix it.

**Azure OpenAI is worse, not better.** With ``detail="high"`` the image is fitted
into 2048x2048 and then, if the shortest side still exceeds 768 px, scaled again
so that it is 768. On the same E-size sheet that is a scale of ~0.075 and glyphs
arrive ~3 px tall. The cap is on the *shortest* side, so a wide sheet is punished
twice.

The fix is to **tile at native resolution** so no downscale occurs, and to
report legibility before spending a single token.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

PATCH = 28


@dataclass(frozen=True)
class VisionLimits:
    name: str
    max_long_edge: int
    max_visual_tokens: int
    max_short_edge: int | None = None
    """Azure OpenAI caps the *shortest* side, which usually binds before the long edge."""

    @classmethod
    def high_resolution(cls) -> VisionLimits:
        """Claude 4.7 and later, including Opus 5."""
        return cls("high-resolution", 2576, 4784)

    @classmethod
    def standard(cls) -> VisionLimits:
        """Everything else, including Claude Sonnet/Opus 4.5."""
        return cls("standard", 1568, 1568)

    @classmethod
    def azure_openai(cls) -> VisionLimits:
        """GPT-4o/4.1/5 family on Azure, ``detail="high"``.

        Documented behaviour: fit inside 2048x2048, then if the shortest side
        still exceeds 768 px scale again so that it is 768. Tokens are counted in
        512 px tiles, so ``max_visual_tokens`` is expressed in 28 px patches only
        to reuse the same arithmetic; the short-edge rule is what actually binds.
        """
        return cls("azure-openai-high", 2048, 10**9, max_short_edge=768)


def visual_tokens(width: int, height: int) -> int:
    return math.ceil(width / PATCH) * math.ceil(height / PATCH)


def downscale_factor(width: int, height: int, limits: VisionLimits) -> float:
    """The scale the service will apply. 1.0 means the image is sent untouched."""
    if width <= 0 or height <= 0:
        return 1.0
    by_edge = limits.max_long_edge / max(width, height)
    by_tokens = math.sqrt(PATCH * PATCH * limits.max_visual_tokens / (width * height))
    by_short = (
        limits.max_short_edge / min(width, height) if limits.max_short_edge else 1.0
    )
    return min(1.0, by_edge, by_tokens, by_short)


@dataclass(frozen=True)
class Legibility:
    width: int
    height: int
    scale: float
    tokens: int
    source_text_px: float
    effective_text_px: float
    tier: str

    @property
    def readable(self) -> bool:
        """Below ~12 px of glyph height, character confusion rises sharply."""
        return self.effective_text_px >= 12.0

    def summary(self) -> str:
        verdict = "OK" if self.readable else "TOO SMALL"
        return (
            f"{self.width}x{self.height} @ {self.tier}: scale={self.scale:.3f} "
            f"tokens={self.tokens} text {self.source_text_px:.1f}px -> "
            f"{self.effective_text_px:.1f}px [{verdict}]"
        )


def assess(
    width: int,
    height: int,
    limits: VisionLimits,
    source_dpi: int = 300,
    text_point_size: float = 10.0,
) -> Legibility:
    """Quantify whether tag text survives the service-side downscale."""
    source_text_px = text_point_size / 72.0 * source_dpi
    scale = downscale_factor(width, height, limits)
    return Legibility(
        width=width,
        height=height,
        scale=scale,
        tokens=visual_tokens(int(width * scale), int(height * scale)),
        source_text_px=source_text_px,
        effective_text_px=source_text_px * scale,
        tier=limits.name,
    )


Box = tuple[int, int, int, int]


def plan_tiles(
    width: int,
    height: int,
    limits: VisionLimits,
    overlap: float = 0.12,
) -> list[Box]:
    """Cover the image with native-resolution tiles that need no downscale.

    Tiles overlap so a tag straddling a boundary is whole in at least one tile;
    duplicates are removed later by tag identity, not by geometry.
    """
    if width <= 0 or height <= 0:
        return []
    if downscale_factor(width, height, limits) >= 1.0:
        return [(0, 0, width, height)]

    # Largest square tile that satisfies the edge, token and short-edge caps. A
    # square tile's shortest side is its side, so the short-edge cap binds directly.
    max_side = min(limits.max_long_edge, int(PATCH * math.sqrt(limits.max_visual_tokens)))
    if limits.max_short_edge:
        max_side = min(max_side, limits.max_short_edge)
    step = max(1, int(max_side * (1.0 - overlap)))

    boxes: list[Box] = []
    y = 0
    while y < height:
        x = 0
        bottom = min(y + max_side, height)
        while x < width:
            right = min(x + max_side, width)
            boxes.append((x, y, right, bottom))
            if right >= width:
                break
            x += step
        if bottom >= height:
            break
        y += step
    return boxes


@dataclass(frozen=True)
class Tile:
    box: Box
    png: bytes
    index: int

    @property
    def label(self) -> str:
        x0, y0, x1, y1 = self.box
        return f"tile{self.index}_{x0}_{y0}_{x1}_{y1}"


def tile_image(
    image_bytes: bytes,
    limits: VisionLimits | None = None,
    overlap: float = 0.12,
    max_tiles: int = 40,
) -> tuple[list[Tile], Legibility, Legibility]:
    """Split a drawing crop into legible tiles.

    Returns ``(tiles, whole_image_legibility, per_tile_legibility)`` so the cost
    and quality trade-off is explicit before any model call.
    """
    from PIL import Image

    limits = limits or VisionLimits.high_resolution()
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        width, height = image.size
        whole = assess(width, height, limits)

        boxes = plan_tiles(width, height, limits, overlap)
        if len(boxes) > max_tiles:
            raise ValueError(
                f"{len(boxes)} tiles exceeds max_tiles={max_tiles}; "
                "raise the cap deliberately or pre-crop the sheet by drawing zone"
            )

        tiles: list[Tile] = []
        for index, box in enumerate(boxes):
            buffer = io.BytesIO()
            image.crop(box).save(buffer, format="PNG", optimize=True)
            tiles.append(Tile(box=box, png=buffer.getvalue(), index=index))

    first = boxes[0] if boxes else (0, 0, width, height)
    per_tile = assess(first[2] - first[0], first[3] - first[1], limits)
    return tiles, whole, per_tile
