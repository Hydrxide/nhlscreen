"""Smooth the live game clock using readings the board hasn't shown yet.

The NHL publishes a new clock reading only about every 20 seconds, and a
reading can be up to ~19 seconds staler than its CDN headers admit. Counting
down from each reading and snapping to the next one makes the board jump both
ways. But the board runs SPOILER_DELAY behind real time (25s, more than one
snapshot interval), so while it shows one reading it usually already holds the
next. ClockTrack plans the clock between consecutive readings so it arrives at
each one exactly on time, with no jump:

  running -> running   count down at whatever steady rate gets there on time
  running -> stopped   count down normally, hold at the stopped time when reached
  stopped -> running   hold, then start in time to reach the reading normally
  stopped -> stopped   (clock moved in between) the same: hold, then run
  past the last known reading: count down normally (or hold if stopped)

Times are epoch seconds of *display* time (when the reading was true + the
delay). A plan is handed to renderers as a ClockSegment: shown =
seconds - (now - start) * rate, the same formula they already use.
"""

from __future__ import annotations

from dataclasses import dataclass

# Steady rates outside this range (x nominal) mean a reading was badly off;
# jump at the next reading instead of racing the clock.
MAX_RATE = 3.0


@dataclass(frozen=True)
class ClockPoint:
    at: float  # display time: when the board should show this reading
    seconds: float
    running: bool
    period: int
    intermission: bool = False

    def same_reading(self, other: "ClockPoint") -> bool:
        return (self.seconds, self.running, self.period, self.intermission) == (
            other.seconds, other.running, other.period, other.intermission)


@dataclass(frozen=True)
class ClockSegment:
    start: float  # epoch seconds
    seconds: float  # clock value at start
    rate: float  # game seconds per real second; 0 = held

    def value(self, now: float) -> float:
        return max(0.0, self.seconds - (now - self.start) * self.rate)


class ClockTrack:
    def __init__(self, nominal_rate: float = 1.0, max_points: int = 12) -> None:
        self.rate = nominal_rate  # 1 live; the replay speed in demos
        self.max_points = max_points
        self.points: list[ClockPoint] = []

    def add(self, point: ClockPoint, now: float) -> None:
        """Record a reading as soon as it's fetched (it'll be shown at ``point.at``).

        Re-fetching an unchanged reading adds nothing: its first sighting is the
        best guess of when it became true.
        """
        if self.points:
            last = self.points[-1]
            if last.same_reading(point) or point.at <= last.at:
                return
            if now > last.at:
                # The board already ran past the last reading (a late fetch): continue
                # from what it's showing now so the new plan has no jump.
                seg = self.segment(now)
                if seg is not None:
                    self.points.append(ClockPoint(now, seg.value(now), seg.rate > 0, last.period, last.intermission))
        self.points.append(point)
        # Keep only what's still needed: the segment in progress onwards.
        current = max((i for i, p in enumerate(self.points) if p.at <= now), default=0)
        self.points = self.points[max(current, len(self.points) - self.max_points):]

    def segment(self, now: float) -> ClockSegment | None:
        """The piece of the plan in effect at ``now`` (None before the first reading)."""
        k = max((i for i, p in enumerate(self.points) if p.at <= now), default=None)
        if k is None:
            return None
        p = self.points[k]
        nxt = self.points[k + 1] if k + 1 < len(self.points) else None
        hold = ClockSegment(p.at, p.seconds, 0.0)
        run = ClockSegment(p.at, p.seconds, self.rate)
        if nxt is None or nxt.period != p.period or p.intermission or nxt.intermission:
            return run if p.running and not p.intermission else hold

        span = nxt.at - p.at
        drop = p.seconds - nxt.seconds
        if drop <= 0:  # no clock movement (or time put back on): hold, adjust at nxt
            return hold
        if drop / span > MAX_RATE * self.rate:
            return run if p.running else hold  # too far off to chase; jump at nxt

        if p.running and nxt.running:
            return ClockSegment(p.at, p.seconds, drop / span)
        if p.running:  # stopped by nxt: run at the normal pace (faster if behind), then hold
            rate = max(self.rate, drop / span)
            reach = p.at + drop / rate
            return ClockSegment(p.at, p.seconds, rate) if now < reach else ClockSegment(reach, nxt.seconds, 0.0)
        # Stopped at p, and the clock moved before nxt: start in time to get there.
        start = nxt.at - drop / self.rate
        if start <= p.at:
            return ClockSegment(p.at, p.seconds, drop / span)
        return hold if now < start else ClockSegment(start, p.seconds, self.rate)
