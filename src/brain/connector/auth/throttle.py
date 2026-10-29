"""Slow down failed sign-ins to the vault connector, and report the ones that matter.

After ``n`` failed sign-ins from an address in the last hour, that address has
to wait ``min(60, 2 ** (n - 1))`` seconds after its last failure before the
next try: 1 second, then 2, 4, 8 and so on, never more than 60. Failures from
one address never slow down another, so a stranger cannot lock the owner out.
An IPv6 address counts as its whole /64 network, because one holder can use
any address in it; an IPv4 address (also one written as IPv4-mapped IPv6)
counts on its own.

Three alerts, each passed to ``alert`` as one plain message:

- an address (or /64) reaches 10 failures in the hour: once, and again only
  after its count has dropped below 10 and climbed back;
- the password was right but the code was wrong: at most once an hour;
- all addresses together reach 50 failures in the hour: at most once an hour.

``record_failure`` records a failure and returns the alerts it makes due
without sending them, so a caller holding a lock can send them after letting
go (``send``). ``failure`` does both. An error raised by ``alert`` is logged
and the other messages still go.

The counts live in memory and start empty when the connector restarts. An
address whose last failure left the window is forgotten, so memory stays
bounded by the failures of the last hour. All methods may be called from any
thread.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence

_logger = logging.getLogger(__name__)

MAX_DELAY = 60.0
ALERT_AT = 10
ALL_ALERT_AT = 50


def _delay(failures: int) -> float:
    if failures == 0:
        return 0.0
    return min(MAX_DELAY, float(2 ** (failures - 1)))


def _key(addr: str) -> str:
    """What failures are counted against: the address, or its /64 network for IPv6."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv4Address):
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.IPv6Network((int(ip), 64), strict=False))


class Throttle:
    def __init__(
        self,
        *,
        alert: Callable[[str], None],
        window: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._alert = alert
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._alerted: set[str] = set()
        self._all: deque[float] = deque()
        self._code_alert_at: float | None = None
        self._all_alert_at: float | None = None

    def _count(self, key: str, now: float) -> int:
        failures = self._failures.get(key)
        if failures is not None:
            while failures and failures[0] <= now - self._window:
                failures.popleft()
            if not failures:
                del self._failures[key]
        count = len(failures) if failures else 0
        if count < ALERT_AT:
            self._alerted.discard(key)
        return count

    def _forget_old(self, now: float) -> None:
        while self._failures:
            key, failures = next(iter(self._failures.items()))
            if failures[-1] > now - self._window:
                break
            del self._failures[key]
            self._alerted.discard(key)
        while self._all and self._all[0] <= now - self._window:
            self._all.popleft()

    def _quiet_since(self, last: float | None, now: float) -> bool:
        return last is None or now - last >= self._window

    def delay_for(self, addr: str) -> float:
        """The wait after the last failure from ``addr``: 0 with no failures in the window."""
        with self._lock:
            return _delay(self._count(_key(addr), self._clock()))

    def retry_after(self, addr: str) -> float:
        """Seconds ``addr`` must still wait before its next try; 0 when it may try now."""
        key = _key(addr)
        with self._lock:
            now = self._clock()
            count = self._count(key, now)
            if count == 0:
                return 0.0
            return max(0.0, self._failures[key][-1] + _delay(count) - now)

    def record_failure(self, addr: str, *, only_code_wrong: bool = False) -> tuple[int, list[str]]:
        """Record a failed sign-in from ``addr`` without sending any alert.

        ``only_code_wrong`` says the password was right and the code was not a
        valid one. Returns the failures of ``addr`` in the window and the alert
        messages this failure made due, for ``send``.
        """
        key = _key(addr)
        alerts: list[str] = []
        with self._lock:
            now = self._clock()
            self._forget_old(now)
            self._count(key, now)
            failures = self._failures.setdefault(key, deque())
            failures.append(now)
            self._failures.move_to_end(key)
            self._all.append(now)
            count = len(failures)
            if count >= ALERT_AT and key not in self._alerted:
                self._alerted.add(key)
                alerts.append(f"Repeated failed sign-ins to the vault connector from {key}")
            if only_code_wrong and self._quiet_since(self._code_alert_at, now):
                self._code_alert_at = now
                alerts.append(
                    f"Correct password but wrong code for the vault connector from {addr}"
                )
            if len(self._all) >= ALL_ALERT_AT and self._quiet_since(self._all_alert_at, now):
                self._all_alert_at = now
                alerts.append(
                    f"{ALL_ALERT_AT} failed sign-ins to the vault connector in the last hour"
                )
        return count, alerts

    def send(self, alerts: Sequence[str]) -> None:
        """Pass each message to ``alert``; an error is logged and the other messages still go."""
        for message in alerts:
            try:
                self._alert(message)
            except Exception:
                _logger.exception("a sign-in alert was not sent: %s", message)

    def failure(self, addr: str, *, only_code_wrong: bool = False) -> int:
        """Record a failed sign-in from ``addr``, send the alerts it makes due, return its failures."""
        count, alerts = self.record_failure(addr, only_code_wrong=only_code_wrong)
        self.send(alerts)
        return count

    def success(self, addr: str) -> None:
        """Forget the failures of ``addr`` (its /64 for IPv6) after it signed in."""
        key = _key(addr)
        with self._lock:
            self._failures.pop(key, None)
            self._alerted.discard(key)
