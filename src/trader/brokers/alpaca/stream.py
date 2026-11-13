"""Background ``trade_updates`` stream for the Alpaca paper adapter (architecture.md §4, §7).

A daemon :class:`threading.Thread` runs the SDK's ``TradingStream`` (its own asyncio loop) and
puts every raw message, stamped with the injected clock's receipt time, on a thread-safe queue
that :meth:`TradeUpdateStream.drain` empties without blocking (``BrokerAdapter.drain_events``
runs on the worker thread, ADR-0014).

The stream is a latency optimisation, never the source of truth: a message can be lost while
the socket reconnects, so every (re)connection is counted and the adapter re-reads open orders
over REST, and the reconciler compares REST views anyway. If a session ends or fails, the thread
opens a new one after an exponential backoff (``min_backoff * 2**n`` capped at ``max_backoff``,
no jitter) that :meth:`TradeUpdateStream.stop` interrupts at once.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from trader.brokers.alpaca.transport import TradeStreamSession
from trader.ops.clock import Clock

log = logging.getLogger(__name__)

OpenSession = Callable[[Callable[[], None]], TradeStreamSession]
_STOP_POLL_SECONDS = 0.5


class TradeUpdateStream:
    def __init__(
        self,
        *,
        open_session: OpenSession,
        clock: Clock,
        min_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        join_timeout_seconds: float = 15.0,
    ) -> None:
        if min_backoff_seconds <= 0 or max_backoff_seconds < min_backoff_seconds:
            raise ValueError("backoff bounds must be positive and ordered")
        self._open = open_session
        self._clock = clock
        self._min_backoff = min_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._join_timeout = join_timeout_seconds
        self._queue: queue.Queue[tuple[datetime, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._session: TradeStreamSession | None = None
        self._thread: threading.Thread | None = None
        self._connections = 0
        self._failures = 0
        self._gaps: list[datetime] = []

    # ------------------------------------------------------------------ public
    @property
    def connections(self) -> int:
        """Successful (re)connections so far; a change means events may have been missed."""
        with self._lock:
            return self._connections

    @property
    def gaps(self) -> tuple[datetime, ...]:
        """Times (injected clock) at which a session ended or failed."""
        with self._lock:
            return tuple(self._gaps)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the trade stream was already started")
        self._thread = threading.Thread(target=self._run, name="alpaca-trade-updates", daemon=True)
        self._thread.start()

    def drain(self) -> list[tuple[datetime, Any]]:
        """All queued messages in arrival order, without blocking."""
        items: list[tuple[datetime, Any]] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def stop(self) -> None:
        """Stop and join the thread. The stop request is repeated while the thread lives: the
        SDK ignores one that arrives before its event loop is running."""
        self._stop.set()
        thread = self._thread
        for _ in range(max(1, round(self._join_timeout / _STOP_POLL_SECONDS))):
            with self._lock:
                session = self._session
            if session is not None:
                try:
                    session.stop()
                except Exception as exc:  # the SDK may not have started its loop yet
                    log.debug("trade stream stop raised %s", type(exc).__name__)
            if thread is None:
                return
            thread.join(_STOP_POLL_SECONDS)
            if not thread.is_alive():
                return
        log.warning("alpaca trade_updates thread did not stop in %.0f s", self._join_timeout)

    # ------------------------------------------------------------------ thread
    def _connected(self) -> None:
        with self._lock:
            self._connections += 1
            self._failures = 0
        log.info("alpaca trade_updates stream connected")

    async def _handle(self, message: Any) -> None:
        self._queue.put((self._clock.now(), message))

    def _backoff(self) -> float:
        with self._lock:
            self._failures += 1
            failures = self._failures
        return float(min(self._max_backoff, self._min_backoff * 2 ** min(failures - 1, 16)))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                session = self._open(self._connected)
                session.subscribe_trade_updates(self._handle)
                with self._lock:
                    self._session = session
                if self._stop.is_set():
                    break
                session.run()
            except Exception as exc:  # the thread must outlive any SDK or network failure
                log.warning("alpaca trade_updates session failed: %s", type(exc).__name__)
            finally:
                with self._lock:
                    self._session = None
            if self._stop.is_set():
                break
            with self._lock:
                self._gaps.append(self._clock.now())
            delay = self._backoff()
            log.info("alpaca trade_updates reconnecting in %.1f s", delay)
            self._stop.wait(delay)
