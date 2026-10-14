"""LED matrix rendering: Screen JSON -> 128x64 RGB frame.

Pure Python with no hardware dependency, so the same code draws the browser
preview and (later) the physical panel on the Pi.
"""

from .canvas import Canvas
from .layouts import render

__all__ = ["Canvas", "render"]
