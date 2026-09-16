"""Tails the agent's console log for the UI.

The agent container writes everything it prints to a file on the shared run
volume (see agent/entrypoint.sh). This follows that file the way `tail -F`
would - surviving the per-boot rotation - and keeps a bounded history so a
browser opening late still sees the boot.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ConsoleTail:
    """Follows a log file on a background thread, fanning lines out to
    asyncio subscribers and keeping a ring buffer for late joiners."""

    def __init__(self, path: str, history: int = 20000, poll_seconds: float = 0.2):
        self.path = path
        self.poll_seconds = poll_seconds
        self._history: collections.deque = collections.deque(maxlen=history)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Timestamps of boot markers seen in the log, most recent last. Several
        # in quick succession means the agent is failing to start.
        self._boots: collections.deque = collections.deque(maxlen=50)

    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="console-tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def entries(self, since: int = 0, tail: Optional[int] = None) -> list[dict]:
        """Lines with seq > since; `tail` limits to the most recent N."""
        with self._lock:
            items = [e for e in self._history if e["seq"] > since]
        if tail is not None:
            items = items[-tail:]
        return items

    @property
    def latest_seq(self) -> int:
        return self._seq

    def boots_within(self, seconds: float) -> int:
        """How many boot markers arrived in the last `seconds`."""
        cutoff = time.time() - seconds
        with self._lock:
            return sum(1 for ts in self._boots if ts >= cutoff)

    # ------------------------------------------------------------------

    def _emit(self, text: str) -> None:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": time.time(), "text": text}
            self._history.append(entry)
            if text.startswith("entrypoint: ==== boot"):
                self._boots.append(entry["ts"])
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(entry)
            except Exception:
                pass

    def _run(self) -> None:
        handle = None
        inode = None
        partial = b""

        while not self._stop.is_set():
            try:
                stat = os.stat(self.path)
            except FileNotFoundError:
                if handle is not None:
                    handle.close()
                    handle = None
                    inode = None
                time.sleep(self.poll_seconds)
                continue

            # Rotated (new inode) or truncated: start again from the top
            if handle is None or stat.st_ino != inode or stat.st_size < handle.tell():
                if handle is not None:
                    handle.close()
                try:
                    handle = open(self.path, "rb")
                except OSError:
                    handle = None
                    time.sleep(self.poll_seconds)
                    continue
                inode = stat.st_ino
                partial = b""

            chunk = handle.read()
            if chunk:
                data = partial + chunk
                lines = data.split(b"\n")
                partial = lines.pop()       # incomplete trailing line, if any
                for raw in lines:
                    self._emit(raw.decode("utf-8", "replace").rstrip("\r"))
            else:
                time.sleep(self.poll_seconds)

        if handle is not None:
            handle.close()


def asyncio_bridge(tail: ConsoleTail, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    """Subscribes an asyncio queue to a tail running on another thread.

    Returns the queue; the caller unsubscribes with `tail.unsubscribe(queue.callback)`.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def on_line(entry: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, entry)
        except RuntimeError:
            pass

    queue.callback = on_line     # type: ignore[attr-defined]
    tail.subscribe(on_line)
    return queue
