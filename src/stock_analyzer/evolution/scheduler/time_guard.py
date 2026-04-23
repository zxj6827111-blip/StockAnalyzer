"""Time-window guardrail for off-hours execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum


class TimeGuardMode(StrEnum):
    """Execution power mode based on current time window."""

    HARD_STOP = "hard_stop"
    SOFT_YIELD = "soft_yield"
    TRANSITION = "transition"
    FULL_POWER = "full_power"


@dataclass(frozen=True, slots=True)
class TimeGuardDecision:
    """Decision for one timestamp."""

    mode: TimeGuardMode
    action: str
    window_name: str


@dataclass(frozen=True, slots=True)
class _Window:
    name: str
    start: time
    end: time


class TimeGuard:
    """Classify runtime window into hard/soft/transition/full-power."""

    def __init__(self, open_auction_start: time | None = None) -> None:
        auction_start = open_auction_start or time(8, 30)
        self._hard_stop_windows = (
            _Window("open_auction_guard", auction_start, time(9, 35)),
            _Window("close_auction_guard", time(14, 55), time(15, 5)),
        )
        self._soft_yield_windows = (
            _Window("morning_session", time(9, 35), time(11, 35)),
            _Window("afternoon_session", time(13, 0), time(14, 55)),
        )
        self._transition_windows = (
            _Window("midday_transition", time(11, 35), time(13, 0)),
            _Window("post_close_transition", time(15, 5), time(15, 30)),
        )

    @property
    def open_auction_start(self) -> time:
        """Return start time of open-auction hard-stop window."""
        return self._hard_stop_windows[0].start

    def evaluate(self, moment: datetime | None = None) -> TimeGuardDecision:
        """Return mode and recommended action for one timestamp."""
        current = moment or datetime.now()
        if current.weekday() >= 5:
            return TimeGuardDecision(
                mode=TimeGuardMode.FULL_POWER,
                action="full CPU and I/O allowed",
                window_name="weekend",
            )

        current_time = current.time()
        hard = self._match_window(current_time, self._hard_stop_windows)
        if hard is not None:
            return TimeGuardDecision(
                mode=TimeGuardMode.HARD_STOP,
                action="terminate tasks with checkpoint write",
                window_name=hard.name,
            )

        soft = self._match_window(current_time, self._soft_yield_windows)
        if soft is not None:
            return TimeGuardDecision(
                mode=TimeGuardMode.SOFT_YIELD,
                action="yield CPU to <=10% and avoid heavy work",
                window_name=soft.name,
            )

        transition = self._match_window(current_time, self._transition_windows)
        if transition is not None:
            return TimeGuardDecision(
                mode=TimeGuardMode.TRANSITION,
                action="limit CPU and disable high I/O tasks",
                window_name=transition.name,
            )

        return TimeGuardDecision(
            mode=TimeGuardMode.FULL_POWER,
            action="full CPU and I/O allowed",
            window_name="offhours",
        )

    @staticmethod
    def _match_window(current: time, windows: tuple[_Window, ...]) -> _Window | None:
        for window in windows:
            if _in_window(current=current, start=window.start, end=window.end):
                return window
        return None


def _in_window(current: time, start: time, end: time) -> bool:
    current_min = current.hour * 60 + current.minute
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute

    if start_min <= end_min:
        return start_min <= current_min < end_min
    return current_min >= start_min or current_min < end_min
