"""A width x height RGB pixel buffer: the exact frame an LED matrix shows.

``to_bytes()`` is the raw frame (row-major RGB, what the matrix library takes);
``to_png()`` draws it as LED dots for previews.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Iterable

from .fonts import Font

RGB = tuple[int, int, int]

BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)
GOLD: RGB = (255, 176, 0)

# Span kinds -> colors (see views.py). Team/player spans carry their own color.
KIND_COLORS: dict[str, RGB] = {"label": WHITE, "value": GOLD}


def span_color(span: dict[str, Any]) -> RGB:
    if span.get("color"):
        return tuple(span["color"])  # type: ignore[return-value]
    return KIND_COLORS.get(span.get("kind", "label"), WHITE)


def dim(color: RGB, factor: float) -> RGB:
    return tuple(int(c * factor) for c in color)  # type: ignore[return-value]


class Canvas:
    def __init__(self, width: int = 128, height: int = 64) -> None:
        self.width = width
        self.height = height
        self.buf = bytearray(width * height * 3)

    # ------------------------------------------------------------------ pixels

    def set(self, x: int, y: int, color: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.buf[i : i + 3] = bytes(color)

    def get(self, x: int, y: int) -> RGB:
        i = (y * self.width + x) * 3
        return (self.buf[i], self.buf[i + 1], self.buf[i + 2])

    def fill(self, x: int, y: int, w: int, h: int, color: RGB) -> None:
        for yy in range(max(0, y), min(self.height, y + h)):
            for xx in range(max(0, x), min(self.width, x + w)):
                self.set(xx, yy, color)

    def hline(self, x0: int, x1: int, y: int, color: RGB) -> None:
        self.fill(x0, y, x1 - x0 + 1, 1, color)

    # ------------------------------------------------------------------ text

    def text(self, x: int, y: int, text: str, font: Font, color: RGB, scale: int = 1) -> int:
        """Draw text with its top-left at (x, y). Returns x after the text."""
        for ch in font.normalize(text):
            glyph = font.glyph(ch)
            for dx, dy in glyph.pixels:
                if scale == 1:
                    self.set(x + dx, y + dy, color)
                else:
                    self.fill(x + dx * scale, y + dy * scale, scale, scale, color)
            x += (glyph.width + font.spacing) * scale
        return x

    def spans(self, x: int, y: int, spans: Iterable[dict[str, Any]], font: Font, scale: int = 1) -> int:
        """Draw tagged spans left to right, each in its kind's color."""
        for span in spans:
            x = self.text(x, y, span["text"], font, span_color(span), scale)
        return x

    def text_right(self, right: int, y: int, text: str, font: Font, color: RGB, scale: int = 1) -> int:
        """Right-align so the text's last pixel lands on ``right``. Returns left x."""
        left = right - font.measure(text, scale) + 1
        self.text(left, y, text, font, color, scale)
        return left

    def text_center(self, center: int, y: int, text: str, font: Font, color: RGB, scale: int = 1) -> None:
        self.text(center - font.measure(text, scale) // 2, y, text, font, color, scale)

    # ------------------------------------------------------------------ output

    def to_bytes(self) -> bytes:
        return bytes(self.buf)

    def to_png(self, pitch: int = 6, unlit: RGB = (22, 22, 22)) -> bytes:
        """The frame as LED dots: each pixel a (pitch-1)px square with a dark gap."""
        width, height = self.width * pitch, self.height * pitch
        rows = []
        for y in range(self.height):
            led_row = bytearray()
            for x in range(self.width):
                color = self.get(x, y)
                led_row += bytes(color if color != BLACK else unlit) * (pitch - 1) + b"\x00\x00\x00"
            rows += [b"\x00" + bytes(led_row)] * (pitch - 1) + [b"\x00" + bytes(width * 3)]
        raw = b"".join(rows)

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
