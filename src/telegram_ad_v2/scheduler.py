import asyncio
import random
from collections import deque
from time import monotonic
from datetime import datetime, timezone


class SendScheduler:
    def __init__(
        self,
        send_interval_seconds: float,
        jitter_min_seconds: float,
        jitter_max_seconds: float,
        max_messages_per_hour: int,
        max_messages_per_day: int = 50,
        active_hours_start: int = 8,
        active_hours_end: int = 23,
    ) -> None:
        self.send_interval_seconds = send_interval_seconds
        self.jitter_min_seconds = jitter_min_seconds
        self.jitter_max_seconds = jitter_max_seconds
        self.max_messages_per_hour = max_messages_per_hour
        self.max_messages_per_day = max_messages_per_day
        self.active_hours_start = active_hours_start
        self.active_hours_end = active_hours_end
        self.sent_timestamps: deque[float] = deque()

    async def wait_for_slot(self) -> None:
        self._trim_old()
        while len(self.sent_timestamps) >= self.max_messages_per_hour:
            oldest = self.sent_timestamps[0]
            to_wait = max(0.0, 3600 - (monotonic() - oldest))
            await asyncio.sleep(min(to_wait, 30.0))
            self._trim_old()

        if self._daily_sent_count() >= self.max_messages_per_day:
            sleep_sec = self._seconds_until_active_hours()
            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)
            self._trim_day()

        if not self._is_active_hours():
            sleep_sec = self._seconds_until_active_hours()
            await asyncio.sleep(sleep_sec)

        jitter = random.uniform(self.jitter_min_seconds, self.jitter_max_seconds)
        await asyncio.sleep(self.send_interval_seconds + jitter)

    def mark_sent(self) -> None:
        self.sent_timestamps.append(monotonic())

    def _trim_old(self) -> None:
        now = monotonic()
        while self.sent_timestamps and now - self.sent_timestamps[0] > 3600:
            self.sent_timestamps.popleft()

    def _daily_sent_count(self) -> int:
        return sum(1 for ts in self.sent_timestamps if monotonic() - ts < 86400)

    def _trim_day(self) -> None:
        cutoff = monotonic() - 86400
        while self.sent_timestamps and self.sent_timestamps[0] < cutoff:
            self.sent_timestamps.popleft()

    def _is_active_hours(self) -> bool:
        now = datetime.now(timezone.utc).hour
        if self.active_hours_start <= self.active_hours_end:
            return self.active_hours_start <= now < self.active_hours_end
        else:
            return now >= self.active_hours_start or now < self.active_hours_end

    def _seconds_until_active_hours(self) -> float:
        now = datetime.now(timezone.utc)
        current_hour = now.hour
        if current_hour < self.active_hours_start:
            target = now.replace(hour=self.active_hours_start, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(
                hour=self.active_hours_start, minute=0, second=0, microsecond=0
            )
            import datetime as _dt

            target += _dt.timedelta(days=1)
        return max(0.0, (target - now).total_seconds())
