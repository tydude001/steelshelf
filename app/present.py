"""Shaping data for the templates: dates and chart geometry. Pure, no I/O.

The charts are inline SVG drawn in Jinja; this module only works out where the
points go, so the geometry is testable and the templates stay markup.
"""

from dataclasses import dataclass
from datetime import UTC, datetime


def _local(stamp: str) -> datetime:
    """SQLite's `datetime('now')` is UTC with no zone; this is that moment, local."""
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone()


def local_date(stamp: str) -> str:
    """'2026-09-26 02:00:00' (UTC) → '2026-09-25' in Chicago: the day it was, here."""
    return _local(stamp).date().isoformat()


def day(stamp: str | None) -> str:
    """'2026-09-25 14:03:11' → '25 Sep 2026', in the server's local zone."""
    if not stamp:
        return ""
    when = _local(stamp)
    return f"{when.day} {when:%b %Y}"


def short_day(stamp: str | None) -> str:
    """'25 Sep', for chart axes."""
    if not stamp:
        return ""
    when = _local(stamp)
    return f"{when.day} {when:%b}"


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    value: float
    stamp: str


@dataclass(frozen=True)
class Line:
    """A line chart's points in a `width` × `height` viewBox, plus the axis height."""

    points: list[Point]
    width: int
    height: int
    axis: float

    @property
    def polyline(self) -> str:
        return " ".join(f"{p.x:g},{p.y:g}" for p in self.points)

    @property
    def first(self) -> Point:
        return self.points[0]

    @property
    def last(self) -> Point:
        return self.points[-1]


def line(
    series: list[tuple[str, float]], width: int = 342, height: int = 96,
    pad_x: int = 28, top: int = 30, bottom: int = 24,
) -> Line | None:
    """Place (timestamp, value) pairs, oldest first: x by time, y by value.

    The lowest value sits on the floor of the plot and the highest at its top;
    a flat series runs through the middle. Points that share a moment (or a
    series of one) spread evenly instead. None for an empty series.
    """
    if not series:
        return None
    times = [_local(s).timestamp() for s, _ in series]
    values = [v for _, v in series]
    left, right = pad_x, width - pad_x
    floor, ceiling = height - bottom, top
    t0, span = times[0], times[-1] - times[0]
    lo, rise = min(values), max(values) - min(values)
    n = len(series)
    points = []
    for i, ((stamp, value), t) in enumerate(zip(series, times)):
        if span > 0:
            x = left + (t - t0) / span * (right - left)
        else:
            x = (left + right) / 2 if n == 1 else left + i / (n - 1) * (right - left)
        y = (floor + ceiling) / 2 if rise == 0 else floor - (value - lo) / rise * (floor - ceiling)
        points.append(Point(round(x, 1), round(y, 1), value, stamp))
    return Line(points, width, height, axis=floor + 6)
