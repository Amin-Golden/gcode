import sys
import time
from dataclasses import dataclass
from math import cos, hypot, pi, sin
import re

import numpy as np

try:
    from skimage.morphology import skeletonize
    from scipy import ndimage

    _HAS_SKIMAGE = True
except ImportError:
    skeletonize = None
    ndimage = None
    _HAS_SKIMAGE = False

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTextLayout,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
PERSIAN_LETTERS = set("اآبپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیيكة")
NUMERIC_KEYS = {"iban", "card_number", "expiry", "cvv2"}
PERSIAN_DISPLAY_FONT = "B Nazanin"
NUMERIC_DISPLAY_FONT = "Calibri"
MACHINE_WIDTH_MM = 105.0
MACHINE_HEIGHT_MM = 90.0
CARD_ORIGIN_X_MM = 0.0
CARD_ORIGIN_Y_MM = 30.0
STANDARD_BAUD_RATES = [
    300,
    600,
    1200,
    2400,
    4800,
    9600,
    14400,
    19200,
    38400,
    57600,
    115200,
    128000,
    230400,
    250000,
    500000,
    1000000,
]


# Centerline extraction settings
CENTERLINE_PX_PER_MM = 50.0      # raster resolution used for skeletonization
CENTERLINE_PAD_PX = 6            # blank border around the rasterized glyph
CENTERLINE_RDP_EPS_PX = 0.7      # polyline simplification tolerance in pixels
CENTERLINE_SPUR_MM = 0.35        # prune skeleton branches shorter than this that hang off a junction
CENTERLINE_NOISE_MM = 0.12       # drop tiny isolated fragments shorter than this (raster noise)
DOT_MAX_MM = 1.4                 # connected components no larger than this may be Persian dots
DOT_FILL_RATIO = 0.5             # ... and at least this solid (area / bbox area) to count as a dot
DOT_MARK_RADIUS_MM = 0.18        # radius of the small circle engraved for each dot/spot
DOT_MARK_SEGMENTS = 8            # number of segments used to trace that circle


_NEIGHBOR_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _trace_skeleton(skel):
    """Convert a 1px-thick boolean skeleton into a list of pixel polylines.

    Returns a list of (points, deg_start, deg_end) where points is a list of
    (col, row) float tuples and deg_* are the skeleton-graph degrees of the two
    endpoints. Junctions (degree>=3) and endpoints (degree==1) break the
    skeleton into separate strokes; the degrees let callers tell a real stroke
    apart from a short spur branching off a junction.
    """
    rows, cols = np.nonzero(skel)
    if len(rows) == 0:
        return []

    pixels = set(zip(rows.tolist(), cols.tolist()))
    neighbors = {}
    for r, c in pixels:
        nb = [(r + dr, c + dc) for dr, dc in _NEIGHBOR_OFFSETS if (r + dr, c + dc) in pixels]
        neighbors[(r, c)] = nb
    degree = {p: len(nb) for p, nb in neighbors.items()}

    visited = set()

    def edge_key(a, b):
        return (a, b) if a <= b else (b, a)

    def walk(start, nxt):
        line = [start]
        prev, cur = start, nxt
        while True:
            line.append(cur)
            visited.add(edge_key(prev, cur))
            if degree[cur] != 2:
                break
            advanced = False
            for nb in neighbors[cur]:
                if nb != prev and edge_key(cur, nb) not in visited:
                    prev, cur = cur, nb
                    advanced = True
                    break
            if not advanced:
                break
        return line

    polylines = []
    # Start from endpoints and junctions first so degree-2 chains stay intact.
    for node in (p for p in pixels if degree[p] != 2):
        for nb in neighbors[node]:
            if edge_key(node, nb) not in visited:
                polylines.append(walk(node, nb))
    # Remaining untouched edges belong to closed loops (every pixel degree 2).
    for p in pixels:
        for nb in neighbors[p]:
            if edge_key(p, nb) not in visited:
                polylines.append(walk(p, nb))

    return [([(c, r) for r, c in line], degree[line[0]], degree[line[-1]]) for line in polylines]


def _rdp(points, eps):
    """Ramer-Douglas-Peucker polyline simplification on (x, y) tuples."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        x1, y1 = points[start]
        x2, y2 = points[end]
        dx, dy = x2 - x1, y2 - y1
        norm = hypot(dx, dy)
        dmax, idx = 0.0, -1
        for i in range(start + 1, end):
            x0, y0 = points[i]
            if norm == 0:
                dist = hypot(x0 - x1, y0 - y1)
            else:
                dist = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
            if dist > dmax:
                dmax, idx = dist, i
        if dmax > eps and idx != -1:
            keep[idx] = True
            stack.append((start, idx))
            stack.append((idx, end))
    return [points[i] for i, k in enumerate(keep) if k]


@dataclass
class TextItem:
    key: str
    label: str
    text: str
    x: float
    y: float


class StrokeFont:
    """Single-line laser font styled after B Nazanin and Calibri."""

    def __init__(self):
        self.advance = 1.08
        self.space = 0.52
        self.glyphs = self.build_glyphs()

    @staticmethod
    def curve(p0, p1, p2, p3, steps=8):
        points = []
        for index in range(steps + 1):
            t = index / steps
            mt = 1.0 - t
            x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
            y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
            points.append((x, y))
        return points

    @staticmethod
    def join(*parts):
        points = []
        for part in parts:
            if points and part and points[-1] == part[0]:
                points.extend(part[1:])
            else:
                points.extend(part)
        return points

    def build_glyphs(self):
        c = self.curve
        j = self.join

        glyphs = {
            "0": [j(c((0.5, 1.0), (0.88, 0.98), (0.88, 0.02), (0.5, 0.0)), c((0.5, 0.0), (0.12, 0.02), (0.12, 0.98), (0.5, 1.0)))],
            "1": [[(0.34, 0.74), (0.52, 1.0), (0.52, 0.0)], [(0.35, 0.0), (0.72, 0.0)]],
            "2": [j(c((0.18, 0.78), (0.34, 1.08), (0.88, 1.02), (0.82, 0.66)), c((0.82, 0.66), (0.78, 0.42), (0.36, 0.3), (0.18, 0.0)), [(0.18, 0.0), (0.86, 0.0)])],
            "3": [j(c((0.2, 0.88), (0.42, 1.05), (0.86, 0.94), (0.66, 0.56)), c((0.66, 0.56), (0.98, 0.42), (0.78, -0.08), (0.22, 0.08)))],
            "4": [[(0.78, 0.0), (0.78, 1.0)], [(0.78, 0.46), (0.16, 0.46), (0.66, 1.0)]],
            "5": [j([(0.82, 1.0), (0.28, 1.0), (0.22, 0.58)], c((0.22, 0.58), (0.94, 0.74), (0.94, -0.04), (0.24, 0.08)))],
            "6": [j(c((0.78, 0.92), (0.22, 0.9), (0.08, 0.1), (0.54, 0.02)), c((0.54, 0.02), (0.96, 0.12), (0.86, 0.62), (0.42, 0.54)), c((0.42, 0.54), (0.18, 0.48), (0.22, 0.16), (0.54, 0.02)))],
            "7": [[(0.16, 1.0), (0.86, 1.0), (0.34, 0.0)]],
            "8": [j(c((0.5, 1.0), (0.92, 0.94), (0.82, 0.54), (0.5, 0.5)), c((0.5, 0.5), (0.18, 0.46), (0.08, 0.06), (0.5, 0.0)), c((0.5, 0.0), (0.92, 0.06), (0.82, 0.46), (0.5, 0.5)), c((0.5, 0.5), (0.18, 0.54), (0.08, 0.94), (0.5, 1.0)))],
            "9": [j(c((0.24, 0.08), (0.78, 0.1), (0.92, 0.9), (0.46, 0.98)), c((0.46, 0.98), (0.04, 0.88), (0.14, 0.38), (0.58, 0.46)), c((0.58, 0.46), (0.84, 0.52), (0.78, 0.84), (0.46, 0.98)))],
        }
        glyphs.update(
            {
                "/": [c((0.22, 0.0), (0.34, 0.32), (0.66, 0.68), (0.78, 1.0), steps=5)],
                "-": [[(0.24, 0.5), (0.76, 0.5)]],
                ".": [[(0.45, 0.0), (0.55, 0.0)]],
                ":": [[(0.45, 0.25), (0.55, 0.25)], [(0.45, 0.75), (0.55, 0.75)]],
                "A": [[(0.12, 0.0), (0.5, 1.0), (0.88, 0.0)], [(0.28, 0.42), (0.72, 0.42)]],
                "B": [[(0.18, 0.0), (0.18, 1.0)], j(c((0.18, 1.0), (0.9, 0.98), (0.9, 0.54), (0.18, 0.5)), c((0.18, 0.5), (0.92, 0.48), (0.92, 0.02), (0.18, 0.0)))],
                "C": [c((0.86, 0.86), (0.54, 1.08), (0.1, 0.78), (0.12, 0.48)) + c((0.12, 0.48), (0.14, 0.12), (0.54, -0.08), (0.86, 0.12))[1:]],
                "D": [[(0.18, 0.0), (0.18, 1.0)], c((0.18, 1.0), (0.9, 0.92), (0.98, 0.1), (0.18, 0.0))],
                "E": [[(0.82, 1.0), (0.18, 1.0), (0.18, 0.0), (0.82, 0.0)], [(0.18, 0.5), (0.66, 0.5)]],
                "F": [[(0.18, 0.0), (0.18, 1.0), (0.82, 1.0)], [(0.18, 0.5), (0.66, 0.5)]],
                "I": [[(0.5, 0.0), (0.5, 1.0)], [(0.28, 1.0), (0.72, 1.0)], [(0.28, 0.0), (0.72, 0.0)]],
                "L": [[(0.18, 1.0), (0.18, 0.0), (0.82, 0.0)]],
                "M": [[(0.12, 0.0), (0.12, 1.0), (0.5, 0.38), (0.88, 1.0), (0.88, 0.0)]],
                "N": [[(0.16, 0.0), (0.16, 1.0), (0.84, 0.0), (0.84, 1.0)]],
                "O": [j(c((0.5, 1.0), (0.88, 0.98), (0.88, 0.02), (0.5, 0.0)), c((0.5, 0.0), (0.12, 0.02), (0.12, 0.98), (0.5, 1.0)))],
                "R": [[(0.18, 0.0), (0.18, 1.0)], c((0.18, 1.0), (0.9, 0.94), (0.86, 0.46), (0.18, 0.5)), [(0.44, 0.5), (0.9, 0.0)]],
                "S": [j(c((0.84, 0.86), (0.5, 1.08), (0.12, 0.8), (0.24, 0.56)), c((0.24, 0.56), (0.34, 0.38), (0.88, 0.42), (0.76, 0.12)), c((0.76, 0.12), (0.5, -0.08), (0.18, 0.02), (0.12, 0.16)))],
                "T": [[(0.12, 1.0), (0.88, 1.0)], [(0.5, 1.0), (0.5, 0.0)]],
                "V": [[(0.12, 1.0), (0.5, 0.0), (0.88, 1.0)]],
                "X": [[(0.14, 0.0), (0.86, 1.0)], [(0.14, 1.0), (0.86, 0.0)]],
            }
        )

        # Elegant single-line Persian skeletons for engraving, not filled font outlines.
        dot1 = c((0.44, 1.13), (0.47, 1.17), (0.53, 1.17), (0.56, 1.13), steps=3)
        dot2 = [c((0.33, 1.13), (0.36, 1.17), (0.42, 1.17), (0.45, 1.13), steps=3), c((0.56, 1.13), (0.59, 1.17), (0.65, 1.17), (0.68, 1.13), steps=3)]
        dot3 = [
            c((0.29, 1.12), (0.32, 1.16), (0.38, 1.16), (0.41, 1.12), steps=3),
            c((0.49, 1.26), (0.52, 1.3), (0.58, 1.3), (0.61, 1.26), steps=3),
            c((0.69, 1.12), (0.72, 1.16), (0.78, 1.16), (0.81, 1.12), steps=3),
        ]
        low_dot1 = c((0.44, -0.18), (0.47, -0.14), (0.53, -0.14), (0.56, -0.18), steps=3)
        low_dot2 = [c((0.33, -0.18), (0.36, -0.14), (0.42, -0.14), (0.45, -0.18), steps=3), c((0.56, -0.18), (0.59, -0.14), (0.65, -0.14), (0.68, -0.18), steps=3)]
        low_dot3 = [
            c((0.29, -0.18), (0.32, -0.14), (0.38, -0.14), (0.41, -0.18), steps=3),
            c((0.49, -0.32), (0.52, -0.28), (0.58, -0.28), (0.61, -0.32), steps=3),
            c((0.69, -0.18), (0.72, -0.14), (0.78, -0.14), (0.81, -0.18), steps=3),
        ]

        base = c((0.12, 0.2), (0.34, -0.02), (0.66, -0.02), (0.9, 0.2), steps=7)
        cup = c((0.1, 0.42), (0.28, 0.02), (0.68, -0.06), (0.92, 0.28), steps=7)
        tall = c((0.5, 0.0), (0.48, 0.34), (0.5, 0.72), (0.5, 1.08), steps=5)
        hook = j(c((0.86, 0.68), (0.58, 1.0), (0.16, 0.78), (0.22, 0.48), steps=6), c((0.22, 0.48), (0.34, 0.28), (0.62, 0.32), (0.82, 0.38), steps=5))
        tail = c((0.84, 0.34), (0.62, 0.04), (0.32, -0.02), (0.14, 0.12), steps=7)

        glyphs.update(
            {
                "ا": [tall],
                "آ": [tall, [(0.28, 1.18), (0.72, 1.28)]],
                "ب": [base, low_dot1],
                "پ": [base, *low_dot3],
                "ت": [base, *dot2],
                "ث": [base, *dot3],
                "ج": [hook, tail, low_dot1],
                "چ": [hook, tail, *low_dot3],
                "ح": [hook, tail],
                "خ": [hook, tail, dot1],
                "د": [[(0.25, 0.82), (0.78, 0.52), (0.62, 0.18), (0.25, 0.18)]],
                "ذ": [[(0.25, 0.82), (0.78, 0.52), (0.62, 0.18), (0.25, 0.18)], dot1],
                "ر": [[(0.75, 0.78), (0.72, 0.35), (0.42, 0.02), (0.15, 0.1)]],
                "ز": [[(0.75, 0.78), (0.72, 0.35), (0.42, 0.02), (0.15, 0.1)], dot1],
                "ژ": [[(0.75, 0.78), (0.72, 0.35), (0.42, 0.02), (0.15, 0.1)], *dot3],
                "س": [[(0.08, 0.42), (0.25, 0.15), (0.42, 0.42), (0.6, 0.15), (0.86, 0.42)]],
                "ش": [[(0.08, 0.42), (0.25, 0.15), (0.42, 0.42), (0.6, 0.15), (0.86, 0.42)], *dot3],
                "ص": [[(0.1, 0.2), (0.48, 0.05), (0.88, 0.18), (0.82, 0.58), (0.42, 0.62), (0.25, 0.34)]],
                "ض": [[(0.1, 0.2), (0.48, 0.05), (0.88, 0.18), (0.82, 0.58), (0.42, 0.62), (0.25, 0.34)], dot1],
                "ط": [[(0.16, 0.18), (0.86, 0.18), (0.72, 0.62), (0.3, 0.62), (0.16, 0.18)], tall],
                "ظ": [[(0.16, 0.18), (0.86, 0.18), (0.72, 0.62), (0.3, 0.62), (0.16, 0.18)], tall, dot1],
                "ع": [[(0.78, 0.82), (0.36, 0.82), (0.2, 0.56), (0.48, 0.42), (0.82, 0.32), (0.7, 0.05), (0.28, 0.02)]],
                "غ": [[(0.78, 0.82), (0.36, 0.82), (0.2, 0.56), (0.48, 0.42), (0.82, 0.32), (0.7, 0.05), (0.28, 0.02)], dot1],
                "ف": [[(0.1, 0.18), (0.82, 0.18), (0.78, 0.62), (0.45, 0.72), (0.32, 0.45), (0.55, 0.3)], dot1],
                "ق": [[(0.1, 0.18), (0.82, 0.18), (0.78, 0.62), (0.45, 0.72), (0.32, 0.45), (0.55, 0.3)], *dot2],
                "ک": [[(0.18, 0.0), (0.18, 1.0)], [(0.8, 0.86), (0.35, 0.46), (0.82, 0.12)]],
                "گ": [[(0.18, 0.0), (0.18, 1.0)], [(0.8, 0.86), (0.35, 0.46), (0.82, 0.12)], [(0.42, 1.1), (0.82, 1.1)]],
                "ل": [[(0.58, 1.0), (0.58, 0.22), (0.32, 0.0), (0.12, 0.15)]],
                "م": [[(0.78, 0.62), (0.35, 0.62), (0.2, 0.25), (0.5, 0.05), (0.82, 0.25), (0.78, 0.62)]],
                "ن": [cup, dot1],
                "و": [[(0.25, 0.72), (0.72, 0.72), (0.82, 0.35), (0.52, 0.05), (0.18, 0.14)]],
                "ه": [[(0.48, 0.75), (0.16, 0.48), (0.35, 0.12), (0.72, 0.18), (0.84, 0.54), (0.48, 0.75)]],
                "ی": [cup, *low_dot2],
            }
        )
        glyphs["ي"] = glyphs["ی"]
        glyphs["ك"] = glyphs["ک"]
        glyphs["ة"] = glyphs["ه"]
        return glyphs

    def normalize(self, text, numeric=False):
        text = text.translate(PERSIAN_DIGITS)
        text = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه")
        if numeric:
            allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ /-:.")
            return "".join(char.upper() for char in text if char.upper() in allowed)
        return text

    def visual_chars(self, text, numeric=False):
        text = self.normalize(text, numeric=numeric)
        if numeric:
            return list(text)
        if any(char in PERSIAN_LETTERS for char in text):
            return list(reversed(text))
        return list(text.upper())

    def text_width(self, text, size, numeric=False):
        chars = self.visual_chars(text, numeric=numeric)
        width = 0.0
        for char in chars:
            width += self.space if char.isspace() else self.advance
        return width * size

    def strokes_for_text(self, text, x, y, size, numeric=False):
        strokes = []
        cursor = x
        for char in self.visual_chars(text, numeric=numeric):
            if char.isspace():
                cursor += self.space * size
                continue
            glyph = self.glyphs.get(char.upper()) or self.glyphs.get(char)
            if glyph is None:
                glyph = [[(0.18, 0.0), (0.82, 1.0)], [(0.18, 1.0), (0.82, 0.0)]]
            for stroke in glyph:
                strokes.append([QPointF(cursor + px * size, y + py * size) for px, py in stroke])
            cursor += self.advance * size
        return strokes


class LaserPreview(QWidget):
    itemMoved = pyqtSignal(int, float, float)
    itemSelected = pyqtSignal(int)

    def __init__(self, items, stroke_font, size_getter, font_factory, centerline_provider=None, parent=None):
        super().__init__(parent)
        self.items = items
        self.stroke_font = stroke_font
        self.size_getter = size_getter
        self.font_factory = font_factory
        self.centerline_provider = centerline_provider
        self.work_width = 86.0
        self.work_height = 54.0
        self.padding = 28
        self.selected_index = 0
        self.drag_index = None
        self.drag_offset = QPointF(0, 0)
        self.setMinimumSize(620, 440)
        self.setMouseTracking(True)

    def set_work_area(self, width, height):
        self.work_width = max(1.0, float(width))
        self.work_height = max(1.0, float(height))
        self.update()

    def set_selected_index(self, index):
        self.selected_index = index
        self.update()

    def scale_factor(self):
        available_w = max(1, self.width() - self.padding * 2)
        available_h = max(1, self.height() - self.padding * 2)
        return min(available_w / self.work_width, available_h / self.work_height)

    def origin(self):
        scale = self.scale_factor()
        used_w = self.work_width * scale
        used_h = self.work_height * scale
        return QPointF((self.width() - used_w) / 2, (self.height() + used_h) / 2)

    def screen_to_machine(self, point):
        scale = self.scale_factor()
        origin = self.origin()
        return QPointF((point.x() - origin.x()) / scale, (origin.y() - point.y()) / scale)

    def machine_to_screen(self, point):
        scale = self.scale_factor()
        origin = self.origin()
        return QPointF(origin.x() + point.x() * scale, origin.y() - point.y() * scale)

    def preview_text(self, item):
        return self.stroke_font.normalize(item.text, numeric=item.key in NUMERIC_KEYS)

    def item_bounds(self, item):
        text = self.preview_text(item)
        if not text:
            return QRectF(item.x - 1.0, item.y - 1.0, 2.0, 2.0)
        ratio = self.screen_font_ratio()
        font = self.font_factory(item, self.size_getter() * ratio)
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text) / ratio
        height = metrics.height() / ratio
        return QRectF(item.x, item.y - height * 0.78, width, height).normalized()

    def screen_font_ratio(self):
        return 20.0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(245, 246, 248))

        scale = self.scale_factor()
        painter.translate(self.origin())
        painter.scale(scale, -scale)

        self.draw_work_area(painter)
        self.draw_text_items(painter)
        painter.end()

    def draw_work_area(self, painter):
        painter.setPen(QPen(QColor(214, 219, 226), 0))
        for x in range(0, int(self.work_width) + 1, 5):
            painter.drawLine(QPointF(float(x), 0.0), QPointF(float(x), self.work_height))
        for y in range(0, int(self.work_height) + 1, 5):
            painter.drawLine(QPointF(0.0, float(y)), QPointF(self.work_width, float(y)))

        painter.setPen(QPen(QColor(46, 52, 64), 0))
        painter.drawRect(QRectF(0, 0, self.work_width, self.work_height))

        axis_pen = QPen(QColor(35, 130, 95), 0)
        axis_pen.setDashPattern([2, 2])
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(0.0, 0.0), QPointF(min(20.0, self.work_width), 0.0))
        painter.drawLine(QPointF(0.0, 0.0), QPointF(0.0, min(20.0, self.work_height)))

    def draw_text_items(self, painter):
        painter.resetTransform()
        for index, item in enumerate(self.items):
            text = self.preview_text(item)
            if not text:
                continue
            is_selected = index == self.selected_index

            strokes = self.centerline_provider(item) if self.centerline_provider else None
            if strokes:
                stroke_pen = QPen(QColor(25, 85, 150) if is_selected else QColor(42, 48, 58), 1.4)
                stroke_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                stroke_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(stroke_pen)
                dot_color = QColor(25, 85, 150) if is_selected else QColor(42, 48, 58)
                for stroke in strokes:
                    if len(stroke) == 1:
                        center = self.machine_to_screen(stroke[0])
                        painter.setBrush(dot_color)
                        painter.drawEllipse(center, 1.6, 1.6)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        continue
                    painter.drawPolyline(QPolygonF([self.machine_to_screen(point) for point in stroke]))
            else:
                anchor = self.machine_to_screen(QPointF(item.x, item.y))
                font = self.font_factory(item, self.size_getter() * self.scale_factor())
                painter.setFont(font)
                painter.setPen(QPen(QColor(25, 85, 150) if is_selected else QColor(42, 48, 58), 1))
                painter.setLayoutDirection(Qt.LayoutDirection.LeftToRight if item.key in NUMERIC_KEYS else Qt.LayoutDirection.RightToLeft)
                painter.drawText(anchor, text)

            bounds = self.item_bounds(item)
            top_left = self.machine_to_screen(QPointF(bounds.left(), bounds.top()))
            bottom_right = self.machine_to_screen(QPointF(bounds.right(), bounds.bottom()))
            screen_bounds = QRectF(top_left, bottom_right).normalized()
            box_pen = QPen(QColor(225, 80, 68) if is_selected else QColor(137, 147, 161), 0)
            box_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(box_pen)
            painter.drawRect(screen_bounds.adjusted(-3, -3, 3, 3))

    def hit_test(self, screen_pos):
        machine_pos = self.screen_to_machine(screen_pos)
        for index in reversed(range(len(self.items))):
            if self.item_bounds(self.items[index]).adjusted(-1.5, -1.5, 1.5, 1.5).contains(machine_pos):
                return index
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.hit_test(event.position())
        if index is None:
            return
        self.selected_index = index
        self.drag_index = index
        item = self.items[index]
        self.drag_offset = self.screen_to_machine(event.position()) - QPointF(item.x, item.y)
        self.itemSelected.emit(index)
        self.update()

    def mouseMoveEvent(self, event):
        if self.drag_index is None:
            return
        machine_pos = self.screen_to_machine(event.position()) - self.drag_offset
        x = max(0.0, min(self.work_width, machine_pos.x()))
        y = max(0.0, min(self.work_height, machine_pos.y()))
        self.itemMoved.emit(self.drag_index, x, y)

    def mouseReleaseEvent(self, event):
        self.drag_index = None


class LaserTextGCodeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.stroke_font = StrokeFont()
        self._centerline_cache = {}
        self.setWindowTitle("تولید G-code حکاکی لیزر با فونت خطی")
        self.items = [
            TextItem("first_name", "نام", "علی", 8.0, 42.0),
            TextItem("last_name", "نام خانوادگی", "رضایی", 8.0, 34.0),
            TextItem("iban", "شماره شبا", "IR 12 3456 7890 1234 5678 9012 34", 8.0, 26.0),
            TextItem("card_number", "شماره کارت", "6037 9975 1234 5678", 8.0, 19.0),
            TextItem("expiry", "تاریخ انقضا", "12/29", 8.0, 10.0),
            TextItem("cvv2", "CCV2", "123", 46.0, 10.0),
        ]
        self.text_fields = []
        self.x_fields = []
        self.y_fields = []
        self.init_ui()
        self.refresh_data()

    def init_ui(self):
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        main_layout = QHBoxLayout(self)

        controls = QVBoxLayout()
        controls.addWidget(self.build_text_group())
        controls.addWidget(self.build_output_group())
        controls.addWidget(self.build_device_group())
        controls.addLayout(self.build_buttons())

        self.gcode_text = QTextEdit()
        self.gcode_text.setReadOnly(True)
        self.gcode_text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.gcode_text.setPlaceholderText("G-code تولید شده در این بخش نمایش داده می‌شود.")
        self.gcode_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        controls.addWidget(self.gcode_text, stretch=1)

        preview_column = QVBoxLayout()
        preview_label = QLabel("پیش‌نمایش خروجی لیزر - متن‌ها خطی هستند و با ماوس جابه‌جا می‌شوند.")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview = LaserPreview(self.items, self.stroke_font, self.font_size, self.preview_font, self.centerline_paths_for_item)
        self.preview.itemMoved.connect(self.on_preview_item_moved)
        self.preview.itemSelected.connect(self.on_preview_item_selected)
        preview_column.addWidget(preview_label)
        preview_column.addWidget(self.preview, stretch=1)

        main_layout.addLayout(controls, stretch=3)
        main_layout.addLayout(preview_column, stretch=4)

    def display_font(self, item):
        families = set(QFontDatabase.families())
        if item.key in NUMERIC_KEYS:
            family = NUMERIC_DISPLAY_FONT if NUMERIC_DISPLAY_FONT in families else "Arial"
            return QFont(family, 11)
        family = PERSIAN_DISPLAY_FONT if PERSIAN_DISPLAY_FONT in families else "Tahoma"
        return QFont(family, 13)

    def preview_font(self, item, size):
        families = set(QFontDatabase.families())
        if item.key in NUMERIC_KEYS:
            family = NUMERIC_DISPLAY_FONT if NUMERIC_DISPLAY_FONT in families else "Arial"
            font = QFont(family)
            font.setPixelSize(max(1, int(size)))
            return font
        family = PERSIAN_DISPLAY_FONT if PERSIAN_DISPLAY_FONT in families else "Tahoma"
        font = QFont(family)
        font.setPixelSize(max(1, int(size)))
        return font

    def display_font_name(self, requested, fallback):
        families = set(QFontDatabase.families())
        return requested if requested in families else f"{fallback} (جایگزین {requested})"

    def build_text_group(self):
        group = QGroupBox("متن و موقعیت هر بخش")
        layout = QGridLayout()
        for col, title in enumerate(["بخش", "متن", "X mm", "Y mm"]):
            layout.addWidget(QLabel(title), 0, col)

        for row, item in enumerate(self.items, start=1):
            label = QLabel(item.label)
            text_edit = QLineEdit(item.text)
            text_edit.setLayoutDirection(Qt.LayoutDirection.RightToLeft if item.key not in NUMERIC_KEYS else Qt.LayoutDirection.LeftToRight)
            text_edit.setFont(self.display_font(item))
            text_edit.textChanged.connect(self.refresh_data)

            x_spin = self.position_spin(item.x)
            y_spin = self.position_spin(item.y)

            self.text_fields.append(text_edit)
            self.x_fields.append(x_spin)
            self.y_fields.append(y_spin)

            layout.addWidget(label, row, 0)
            layout.addWidget(text_edit, row, 1)
            layout.addWidget(x_spin, row, 2)
            layout.addWidget(y_spin, row, 3)

        group.setLayout(layout)
        return group

    def position_spin(self, value):
        spin = QDoubleSpinBox()
        spin.setRange(-500.0, 500.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        spin.valueChanged.connect(self.refresh_data)
        return spin

    def build_output_group(self):
        group = QGroupBox("تنظیمات خروجی GRBL-M3")
        layout = QFormLayout()

        self.font_size_spin = QDoubleSpinBox()
        self.font_size_spin.setRange(1.0, 30.0)
        self.font_size_spin.setValue(4.5)
        self.font_size_spin.setSingleStep(0.25)
        self.font_size_spin.valueChanged.connect(self.refresh_data)

        self.work_width_spin = QDoubleSpinBox()
        self.work_width_spin.setRange(10.0, 1000.0)
        self.work_width_spin.setValue(86.0)
        self.work_width_spin.setSingleStep(1.0)
        self.work_width_spin.valueChanged.connect(self.refresh_data)

        self.work_height_spin = QDoubleSpinBox()
        self.work_height_spin.setRange(10.0, 1000.0)
        self.work_height_spin.setValue(54.0)
        self.work_height_spin.setSingleStep(1.0)
        self.work_height_spin.valueChanged.connect(self.refresh_data)

        self.feed_rate_spin = QDoubleSpinBox()
        self.feed_rate_spin.setRange(10.0, 10000.0)
        self.feed_rate_spin.setValue(700.0)
        self.feed_rate_spin.setSingleStep(50.0)

        self.travel_rate_spin = QDoubleSpinBox()
        self.travel_rate_spin.setRange(10.0, 20000.0)
        self.travel_rate_spin.setValue(2500.0)
        self.travel_rate_spin.setSingleStep(100.0)

        self.laser_power_spin = QDoubleSpinBox()
        self.laser_power_spin.setRange(0.0, 255.0)
        self.laser_power_spin.setValue(90.0)
        self.laser_power_spin.setSingleStep(5.0)

        layout.addRow("نمایش فارسی:", QLabel(f"{self.display_font_name(PERSIAN_DISPLAY_FONT, 'Tahoma')}"))
        layout.addRow("نمایش اعداد:", QLabel(f"{self.display_font_name(NUMERIC_DISPLAY_FONT, 'Arial')}"))
        layout.addRow("G-code:", QLabel("خط مرکزی تک‌حرکته (centerline) از همان فونت‌های UI"))
        layout.addRow("اندازه متن mm:", self.font_size_spin)
        layout.addRow("عرض کارت mm:", self.work_width_spin)
        layout.addRow("ارتفاع کارت mm:", self.work_height_spin)
        layout.addRow("سرعت حکاکی F:", self.feed_rate_spin)
        layout.addRow("سرعت حرکت آزاد F:", self.travel_rate_spin)
        layout.addRow("Transfer mode:", QLabel("buffered"))
        layout.addRow("S-value max:", QLabel("255"))
        layout.addRow("قدرت لیزر S برای GRBL:", self.laser_power_spin)
        group.setLayout(layout)
        return group

    def build_device_group(self):
        group = QGroupBox("ارسال به دستگاه")
        layout = QFormLayout()

        self.port_combo = QComboBox()
        self.port_combo.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.refresh_ports_button = QPushButton("بازخوانی پورت‌ها")
        self.refresh_ports_button.clicked.connect(self.refresh_serial_ports)

        self.baud_combo = QComboBox()
        self.baud_combo.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        for baud in STANDARD_BAUD_RATES:
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentText("115200")

        self.send_button = QPushButton("ارسال به دستگاه")
        self.send_button.clicked.connect(self.on_send)
        self.send_button.setEnabled(serial is not None)

        status = "pySerial آماده است." if serial is not None else "pySerial نصب نیست؛ فقط ذخیره فایل فعال است."
        self.device_status = QLabel(status)
        self.device_status.setWordWrap(True)
        self.estimated_time_label = QLabel("زمان تخمینی: -")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        layout.addRow("پورت:", self.port_combo)
        layout.addRow("", self.refresh_ports_button)
        layout.addRow("Baud:", self.baud_combo)
        layout.addRow(self.estimated_time_label)
        layout.addRow(self.progress_bar)
        layout.addRow(self.send_button)
        layout.addRow(self.device_status)
        group.setLayout(layout)
        self.refresh_serial_ports()
        return group

    def build_buttons(self):
        layout = QHBoxLayout()
        self.generate_button = QPushButton("تولید G-code")
        self.generate_button.clicked.connect(self.on_generate)
        self.card_outline_button = QPushButton("تست مستطیل کارت")
        self.card_outline_button.clicked.connect(self.on_card_outline_test)
        self.save_button = QPushButton("ذخیره فایل")
        self.save_button.clicked.connect(self.on_save)
        layout.addWidget(self.generate_button)
        layout.addWidget(self.card_outline_button)
        layout.addWidget(self.save_button)
        return layout

    def refresh_serial_ports(self):
        if list_ports is None:
            self.port_combo.clear()
            self.port_combo.addItem("pyserial نصب نیست", "")
            self.port_combo.setEnabled(False)
            self.baud_combo.setEnabled(False)
            self.refresh_ports_button.setEnabled(False)
            return

        current_port = self.port_combo.currentData() if self.port_combo.count() else None
        self.port_combo.clear()
        ports = list(list_ports.comports())
        for port in ports:
            label = f"{port.device} - {port.description}"
            self.port_combo.addItem(label, port.device)

        if not ports:
            self.port_combo.addItem("پورتی پیدا نشد", "")
            self.port_combo.setEnabled(False)
            self.baud_combo.setEnabled(False)
            self.send_button.setEnabled(False)
            self.device_status.setText("هیچ پورت سریال فعالی پیدا نشد.")
            return

        self.port_combo.setEnabled(True)
        self.baud_combo.setEnabled(True)
        self.send_button.setEnabled(serial is not None)
        if current_port:
            index = self.port_combo.findData(current_port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.device_status.setText(f"{len(ports)} پورت سریال پیدا شد.")

    def font_size(self):
        return self.font_size_spin.value()

    def refresh_data(self):
        for index, item in enumerate(self.items):
            text = self.text_fields[index].text().strip()
            if item.key in NUMERIC_KEYS:
                text = self.stroke_font.normalize(text, numeric=True)
                if text != self.text_fields[index].text():
                    self.text_fields[index].blockSignals(True)
                    self.text_fields[index].setText(text)
                    self.text_fields[index].blockSignals(False)
            item.text = text
            item.x = self.x_fields[index].value()
            item.y = self.y_fields[index].value()

        if hasattr(self, "preview"):
            self.preview.set_work_area(self.work_width_spin.value(), self.work_height_spin.value())
            self.preview.update()

    def on_preview_item_selected(self, index):
        self.preview.set_selected_index(index)
        self.text_fields[index].setFocus()

    def on_preview_item_moved(self, index, x, y):
        self.x_fields[index].blockSignals(True)
        self.y_fields[index].blockSignals(True)
        self.x_fields[index].setValue(x)
        self.y_fields[index].setValue(y)
        self.x_fields[index].blockSignals(False)
        self.y_fields[index].blockSignals(False)
        self.items[index].x = x
        self.items[index].y = y
        self.preview.update()

    def build_strokes(self):
        size = self.font_size()
        shapes = []
        for item in self.items:
            if not item.text:
                continue
            strokes = self.stroke_font.strokes_for_text(
                item.text,
                item.x,
                item.y,
                size,
                numeric=item.key in NUMERIC_KEYS,
            )
            shapes.append((item.key, strokes))
        return shapes

    def build_font_outline_paths(self):
        shapes = []
        for item in self.items:
            path = self.font_outline_path_for_item(item)
            polygons = path.toSubpathPolygons()
            if polygons:
                shapes.append((item.key, polygons))
        return shapes

    def build_centerline_shapes(self):
        """Single-stroke (pen-style) polylines for every item, in machine mm."""
        shapes = []
        for item in self.items:
            if not item.text:
                continue
            strokes = self.centerline_paths_for_item(item)
            if strokes:
                shapes.append((item.key, strokes))
        return shapes

    def glyph_pixel_path(self, item, px_per_mm):
        """Return the shaped glyph outline (QPainterPath) of an item in pixel space.

        Qt performs Persian/Arabic shaping and joining here, so the resulting
        outline already represents correctly connected cursive text.
        """
        text = self.stroke_font.normalize(item.text, numeric=item.key in NUMERIC_KEYS)
        raw_path = QPainterPath()
        if not text:
            return raw_path, text

        font = self.preview_font(item, self.font_size() * px_per_mm)

        option = QTextOption()
        option.setTextDirection(Qt.LayoutDirection.LeftToRight if item.key in NUMERIC_KEYS else Qt.LayoutDirection.RightToLeft)

        layout = QTextLayout(text, font)
        layout.setTextOption(option)
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(10000)
        layout.endLayout()

        for glyph_run in layout.glyphRuns():
            raw_font = glyph_run.rawFont()
            for glyph_index, position in zip(glyph_run.glyphIndexes(), glyph_run.positions()):
                glyph_path = raw_font.pathForGlyph(glyph_index)
                raw_path.addPath(glyph_path.translated(position))
        return raw_path, text

    def centerline_relative(self, item):
        """Single-stroke centerlines for an item, relative to its (x, y) anchor.

        Returns a list of polylines (each a list of (dx, dy) offsets in mm).
        Cached by (text, numeric flag, font size) because the skeleton only
        depends on the shaped text, not on its position on the bed.
        """
        if not _HAS_SKIMAGE:
            return None

        text = self.stroke_font.normalize(item.text, numeric=item.key in NUMERIC_KEYS)
        cache_key = (text, item.key in NUMERIC_KEYS, round(self.font_size(), 4))
        cached = self._centerline_cache.get(cache_key)
        if cached is not None:
            return cached
        if not text:
            self._centerline_cache[cache_key] = []
            return []

        ppm = CENTERLINE_PX_PER_MM
        pad = CENTERLINE_PAD_PX
        raw_path, _ = self.glyph_pixel_path(item, ppm)
        raw_path.setFillRule(Qt.FillRule.WindingFill)
        bounds = raw_path.boundingRect()
        if bounds.isEmpty():
            self._centerline_cache[cache_key] = []
            return []

        width_px = int(np.ceil(bounds.width())) + 2 * pad
        height_px = int(np.ceil(bounds.height())) + 2 * pad

        image = QImage(width_px, height_px, QImage.Format.Format_Grayscale8)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        painter.translate(pad - bounds.left(), pad - bounds.top())
        painter.drawPath(raw_path)
        painter.end()

        stride = image.bytesPerLine()
        buffer = image.constBits()
        buffer.setsize(image.sizeInBytes())
        arr = np.frombuffer(buffer, dtype=np.uint8).reshape((height_px, stride))[:, :width_px]
        binary = arr > 127

        bh = bounds.height()

        def to_mm(col, row):
            return ((col - pad) / ppm, (bh + pad - row) / ppm)

        # Process each connected blob on its own so that small, solid components
        # (Persian dots, hamza, the colon, the decimal point) survive as engraved
        # spots instead of being pruned away with skeleton spurs.
        labels, count = ndimage.label(binary, structure=np.ones((3, 3), dtype=int))
        polylines = []
        for label_id in range(1, count + 1):
            component = labels == label_id
            ys, xs = np.nonzero(component)
            w_mm = (xs.max() - xs.min() + 1) / ppm
            h_mm = (ys.max() - ys.min() + 1) / ppm
            fill_ratio = component.sum() / max(1, (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1))

            if max(w_mm, h_mm) <= DOT_MAX_MM and fill_ratio >= DOT_FILL_RATIO:
                # A dot/spot: engrave one point at its centroid.
                polylines.append([to_mm(xs.mean(), ys.mean())])
                continue

            skeleton = skeletonize(component)
            for line, deg_a, deg_b in _trace_skeleton(skeleton):
                line = _rdp(line, CENTERLINE_RDP_EPS_PX)
                stroke = [to_mm(col, row) for col, row in line]
                if len(stroke) < 2:
                    continue
                length = sum(
                    hypot(stroke[i + 1][0] - stroke[i][0], stroke[i + 1][1] - stroke[i][1])
                    for i in range(len(stroke) - 1)
                )
                is_spur = (deg_a >= 3 or deg_b >= 3) and min(deg_a, deg_b) == 1 and length < CENTERLINE_SPUR_MM
                if is_spur or length < CENTERLINE_NOISE_MM:
                    continue
                polylines.append(stroke)

        self._centerline_cache[cache_key] = polylines
        return polylines

    def centerline_paths_for_item(self, item):
        """Centerline strokes placed at the item's machine position (list of QPointF lists)."""
        relative = self.centerline_relative(item)
        if not relative:
            return []
        return [[QPointF(item.x + dx, item.y + dy) for dx, dy in stroke] for stroke in relative]

    @staticmethod
    def dot_mark_points(center):
        """A small closed circle (list of QPointF) used to engrave a single dot."""
        cx, cy = center.x(), center.y()
        return [
            QPointF(
                cx + DOT_MARK_RADIUS_MM * cos(2 * pi * k / DOT_MARK_SEGMENTS),
                cy + DOT_MARK_RADIUS_MM * sin(2 * pi * k / DOT_MARK_SEGMENTS),
            )
            for k in range(DOT_MARK_SEGMENTS + 1)
        ]

    def font_outline_path_for_item(self, item):
        ppm = 20.0
        raw_path, text = self.glyph_pixel_path(item, ppm)
        path = QPainterPath()
        if not text:
            return path

        px_per_mm = ppm
        bounds = raw_path.boundingRect()
        if bounds.isEmpty():
            return path

        for polygon in raw_path.toSubpathPolygons():
            if polygon.isEmpty():
                continue
            machine_polygon = []
            for point in polygon:
                x = item.x + (point.x() - bounds.left()) / px_per_mm
                y = item.y - (point.y() - bounds.bottom()) / px_per_mm
                machine_polygon.append(QPointF(x, y))
            if machine_polygon:
                path.moveTo(machine_polygon[0])
                for point in machine_polygon[1:]:
                    path.lineTo(point)
                path.closeSubpath()
        return path

    def rotate_output_point(self, point):
        return QPointF(
            CARD_ORIGIN_X_MM + self.work_width_spin.value() - point.x(),
            CARD_ORIGIN_Y_MM + self.work_height_spin.value() - point.y(),
        )

    def card_machine_rect_points(self):
        x0 = CARD_ORIGIN_X_MM
        y0 = CARD_ORIGIN_Y_MM
        x1 = x0 + self.work_width_spin.value()
        y1 = y0 + self.work_height_spin.value()
        return [
            QPointF(x0, y0),
            QPointF(x1, y0),
            QPointF(x1, y1),
            QPointF(x0, y1),
            QPointF(x0, y0),
        ]

    def gcode_header(self):
        return [
            "(Generated by laser_text_gcode.py)",
            "(Target firmware: GRBL-M3)",
            "(Transfer mode: buffered)",
            "(S-value max: 255)",
            "(Mode: single-stroke centerlines from displayed fonts)",
            "(Output rotation: 180 degrees)",
            f"(Machine bed: {MACHINE_WIDTH_MM:.3f} x {MACHINE_HEIGHT_MM:.3f} mm)",
            f"(Card origin: X{CARD_ORIGIN_X_MM:.3f} Y{CARD_ORIGIN_Y_MM:.3f})",
            "(Fonts: B Nazanin for Persian, Calibri for numeric fields)",
            "$H ; homing",
            "G00 G17 G40 G21 G54",
            "G90",
            "M8",
            "M5",
            f"G0 X{CARD_ORIGIN_X_MM:.3f}Y{CARD_ORIGIN_Y_MM:.3f}",
        ]

    def generate_gcode(self):
        self.refresh_data()
        feed_rate = self.feed_rate_spin.value()
        travel_rate = self.travel_rate_spin.value()
        power = self.laser_power_spin.value()

        lines = self.gcode_header()

        if not _HAS_SKIMAGE:
            lines.append("(WARNING: scikit-image not installed, falling back to hollow outlines)")
            shapes = self.build_font_outline_paths()
            close_stroke = True
        else:
            shapes = self.build_centerline_shapes()
            close_stroke = False

        for label, strokes in shapes:
            lines.append(f"(Text: {label})")
            for stroke in strokes:
                if not stroke:
                    continue
                if len(stroke) == 1:
                    # A dot/spot: engrave a tiny circle so it is a real, visible
                    # moving toolpath that reliably burns (a zero-length dwell is
                    # invisible in viewers and may not fire the laser).
                    stroke = self.dot_mark_points(stroke[0])
                start = self.rotate_output_point(stroke[0])
                lines.append(f"G0 X{start.x():.3f}Y{start.y():.3f} F{travel_rate:.0f}")
                lines.append("M3")
                lines.append(f"G1 X{start.x():.3f}Y{start.y():.3f} S{power:.0f}F{feed_rate:.0f}")
                for point in stroke[1:]:
                    rotated = self.rotate_output_point(point)
                    lines.append(f"G1 X{rotated.x():.3f}Y{rotated.y():.3f}")
                if close_stroke:
                    lines.append(f"G1 X{start.x():.3f}Y{start.y():.3f}")
                lines.append("M5")
            lines.append("")

        lines.extend(["M9", "G1 S0", "G90", "$H ; homing"])
        return "\n".join(lines)

    def generate_card_outline_test_gcode(self):
        travel_rate = self.travel_rate_spin.value()
        feed_rate = self.feed_rate_spin.value()
        power = min(self.laser_power_spin.value(), 30.0)
        points = self.card_machine_rect_points()

        lines = self.gcode_header()
        lines.append("(Card outline test)")
        start = points[0]
        lines.append(f"G0 X{start.x():.3f}Y{start.y():.3f} F{travel_rate:.0f}")
        lines.append("M3")
        lines.append(f"G1 X{start.x():.3f}Y{start.y():.3f} S{power:.0f}F{feed_rate:.0f}")
        for point in points[1:]:
            lines.append(f"G1 X{point.x():.3f}Y{point.y():.3f}")
        lines.extend(["M5", "M9", "G1 S0", "G90", "$H ; homing"])
        return "\n".join(lines)

    def estimate_gcode_seconds(self, gcode):
        current_x = 0.0
        current_y = 0.0
        current_feed = self.travel_rate_spin.value()
        seconds = 0.0

        for command in self.serial_commands(gcode):
            match = re.search(r"\b(G0|G00|G1|G01)\b", command.upper())
            code = match.group(1) if match else ""
            params = self.gcode_params(command)
            if "F" in params and params["F"] > 0:
                current_feed = params["F"]
            if code not in {"G0", "G00", "G1", "G01"}:
                continue

            target_x = params.get("X", current_x)
            target_y = params.get("Y", current_y)
            distance = hypot(target_x - current_x, target_y - current_y)
            if current_feed > 0:
                seconds += distance / current_feed * 60.0
            current_x = target_x
            current_y = target_y
        return seconds

    def gcode_params(self, command):
        params = {}
        for key, value in re.findall(r"([XYZFS])\s*(-?\d+(?:\.\d+)?)", command.upper()):
            params[key] = float(value)
        return params

    def format_duration(self, seconds):
        total = max(0, int(round(seconds)))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def update_estimated_time(self, gcode):
        seconds = self.estimate_gcode_seconds(gcode)
        self.estimated_time_label.setText(f"زمان تخمینی: {self.format_duration(seconds)}")
        self.progress_bar.setValue(0)

    def on_generate(self):
        gcode = self.generate_gcode()
        self.gcode_text.setPlainText(gcode)
        self.update_estimated_time(gcode)

    def on_card_outline_test(self):
        gcode = self.generate_card_outline_test_gcode()
        self.gcode_text.setPlainText(gcode)
        self.update_estimated_time(gcode)

    def on_save(self):
        gcode = self.gcode_text.toPlainText().strip() or self.generate_gcode()
        self.update_estimated_time(gcode)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "ذخیره G-code",
            "laser_output.gcode",
            "G-code files (*.gcode *.nc *.tap);;All Files (*)",
        )
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(gcode)
            self.device_status.setText(f"فایل ذخیره شد: {path}")

    def on_send(self):
        if serial is None:
            QMessageBox.warning(self, "ارسال ممکن نیست", "برای ارسال مستقیم، pyserial را نصب کنید.")
            return

        gcode = self.gcode_text.toPlainText().strip() or self.generate_gcode()
        self.gcode_text.setPlainText(gcode)
        self.update_estimated_time(gcode)
        self.progress_bar.setValue(0)
        port = self.port_combo.currentData()
        baud = self.baud_combo.currentData()
        if not port:
            QMessageBox.warning(self, "پورت انتخاب نشده", "هیچ پورت سریالی برای ارسال انتخاب نشده است.")
            return

        try:
            with serial.Serial(port, baudrate=baud, timeout=3) as ser:
                time.sleep(2.0)
                ser.reset_input_buffer()
                self.wait_for_controller_ready(ser)
                commands = self.serial_commands(gcode)
                for index, command in enumerate(commands, start=1):
                    self.send_command_and_wait_ok(ser, command)
                    percent = int(index * 100 / max(1, len(commands)))
                    self.progress_bar.setValue(percent)
                    if index % 25 == 0 or index == len(commands):
                        self.device_status.setText(f"ارسال: {index} از {len(commands)} خط ({percent}٪)")
                        QApplication.processEvents()
                ser.flush()
                self.progress_bar.setValue(100)
            self.device_status.setText(f"G-code با موفقیت به {port} ارسال شد.")
        except Exception as exc:
            self.device_status.setText(f"خطا در ارسال: {exc}")

    def serial_commands(self, gcode):
        commands = []
        for line in gcode.splitlines():
            command = line.strip()
            if not command or command.startswith(";") or command.startswith("("):
                continue
            if ";" in command:
                command = command.split(";", 1)[0].strip()
            if command:
                commands.append(command)
        return commands

    def wait_for_controller_ready(self, ser):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            waiting = ser.readline().decode("ascii", errors="ignore").strip().lower()
            if waiting.startswith("start") or waiting.startswith("ok"):
                return
        ser.write(b"\n")
        ser.flush()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            response = ser.readline().decode("ascii", errors="ignore").strip().lower()
            if response.startswith("ok"):
                return

    def send_command_and_wait_ok(self, ser, command):
        ser.write((command + "\n").encode("ascii", errors="ignore"))
        ser.flush()

        deadline = time.time() + (60.0 if command.startswith("$H") else 20.0)
        while time.time() < deadline:
            response = ser.readline().decode("ascii", errors="ignore").strip()
            if not response:
                continue
            lower = response.lower()
            if lower.startswith("ok"):
                return
            if lower.startswith("error") or "resend" in lower:
                raise RuntimeError(f"Marlin rejected '{command}': {response}")
            if "busy:" in lower:
                deadline = time.time() + (60.0 if command.startswith("$H") else 20.0)
                continue
        raise TimeoutError(f"پاسخ ok از دستگاه دریافت نشد: {command}")


def main():
    app = QApplication(sys.argv)
    window = LaserTextGCodeApp()
    window.resize(1280, 760)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
