"""The lab's clock: the time the device and its firmware believe it is.

The clock can be jumped forward or backward and run faster than real time,
so that behaviour scheduled hours or days out - periodic statistics windows,
time-referenced sample intervals, retry backoffs - can be exercised in
seconds. It is a lab instrument, not device configuration: it persists across
reboots the way a hardware RTC keeps its time, and a factory reset does not
touch it.

The device applies the clock to its own timestamps directly. The firmware
sees it through libfaketime, which the agent's entrypoint preloads into
obuspa and which re-reads a control file on every call. That file is written
here in libfaketime's `@<start> x<rate>` form: on a change, the firmware's
clock becomes exactly `start` and advances at `rate` from that moment, so a
rate change never makes its clock run backwards. Real time is written as
`+0`, which makes libfaketime transparent.

libfaketime re-anchors `@<start>` whenever the file changes and also when
the process starts, so a freshly booted agent would begin at the time of the
last change. The agent's plug-in asks the device to rewrite the file during
its init (`clock_sync`), which is the analogue of the kernel reading the RTC
at boot.
"""
from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Optional

MIN_RATE = 1.0
MAX_RATE = 60.0


class LabClock:
    def __init__(self, path: Optional[str]):
        # fake(t) = base_fake + (t - base_real) * rate
        self.path = path
        self.base_real = time.time()
        self.base_fake = self.base_real
        self.rate = 1.0

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def now(self) -> float:
        return self.base_fake + (time.time() - self.base_real) * self.rate

    @property
    def offset(self) -> float:
        """Seconds the lab clock is ahead of real time, at this instant."""
        return self.now() - time.time()

    @property
    def is_real(self) -> bool:
        return self.rate == 1.0 and abs(self.offset) < 0.5

    def snapshot(self) -> dict:
        now = self.now()
        return {
            "real": self.is_real,
            "offset": int(round(self.offset)),
            "rate": self.rate,
            "now": now,
            "nowIso": _iso(now),
        }

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def adjust(self, jump: Optional[float] = None, rate: Optional[float] = None) -> dict:
        """Jumps the clock by `jump` seconds and/or sets its rate.

        The new base is snapped to a whole second because libfaketime's
        start-at form carries whole seconds; the device and the firmware
        then agree exactly.
        """
        if rate is not None:
            if not isinstance(rate, (int, float)) or isinstance(rate, bool):
                raise ValueError("rate must be a number")
            if not MIN_RATE <= rate <= MAX_RATE:
                raise ValueError(f"rate must be between {MIN_RATE:g} and {MAX_RATE:g}")
        if jump is not None:
            if not isinstance(jump, (int, float)) or isinstance(jump, bool):
                raise ValueError("jump must be a number of seconds")

        real = time.time()
        fake = self.now() + (jump or 0)
        self.base_real = real
        self.base_fake = float(int(fake))
        if rate is not None:
            self.rate = float(rate)
        self.write_file()
        return self.snapshot()

    def reset(self) -> dict:
        self.base_real = time.time()
        self.base_fake = self.base_real
        self.rate = 1.0
        self.write_file()
        return self.snapshot()

    # ------------------------------------------------------------------
    # Persistence and the firmware control file
    # ------------------------------------------------------------------

    def to_json(self) -> dict:
        return {"base_real": self.base_real, "base_fake": self.base_fake, "rate": self.rate}

    def load(self, saved: Optional[dict]) -> None:
        if not saved:
            return
        try:
            self.base_real = float(saved["base_real"])
            self.base_fake = float(saved["base_fake"])
            self.rate = float(saved.get("rate", 1.0))
        except (KeyError, TypeError, ValueError):
            self.reset()
            return
        if not MIN_RATE <= self.rate <= MAX_RATE:
            self.rate = 1.0

    def control_string(self) -> str:
        """The libfaketime control line for the clock as it is right now."""
        if self.is_real:
            return "+0"
        start = _dt.datetime.fromtimestamp(int(self.now()), _dt.timezone.utc)
        return f"@{start:%Y-%m-%d %H:%M:%S} x{self.rate:g}"

    def write_file(self) -> str:
        """Rewrites the control file so the firmware re-anchors to the clock now."""
        line = self.control_string()
        if self.path:
            temporary = self.path + ".tmp"
            with open(temporary, "w") as handle:
                handle.write(line + "\n")
            os.replace(temporary, self.path)
        return line


def _iso(timestamp: float) -> str:
    return _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
