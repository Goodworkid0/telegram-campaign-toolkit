from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

from .models import JoinStats
from .telegram_client import TelegramGateway

logger = logging.getLogger(__name__)


class GroupJoiner:
    def __init__(
        self,
        gateway: TelegramGateway,
        join_delay_min: float = 3.0,
        join_delay_max: float = 8.0,
        max_joins_per_session: int = 30,
    ) -> None:
        self.gateway = gateway
        self.join_delay_min = join_delay_min
        self.join_delay_max = join_delay_max
        self.max_joins_per_session = max_joins_per_session
        self.joins_in_session = 0

    async def join_from_file(self, file_path: Path) -> JoinStats:
        stats = JoinStats()

        if not file_path.exists():
            raise FileNotFoundError(f"Group list not found: {file_path}")

        raw_links = [
            line.strip()
            for line in file_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        stats.attempted = len(raw_links)
        logger.info("attempting to join %d groups on %s", stats.attempted, self.gateway.phone_number)

        for link in raw_links:
            if self.joins_in_session >= self.max_joins_per_session:
                logger.warning("join cap reached (%d), stopping", self.max_joins_per_session)
                break

            delay = random.uniform(self.join_delay_min, self.join_delay_max)
            await asyncio.sleep(delay)

            try:
                ok, reason = await self.gateway.join_chat(link)
                if ok:
                    stats.joined += 1
                    self.joins_in_session += 1
                    logger.info("joined: %s on %s", link, self.gateway.phone_number)
                else:
                    stats.failed += 1
                    logger.warning("failed to join %s on %s: %s", link, self.gateway.phone_number, reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                stats.failed += 1
                logger.exception("error joining %s on %s", link, self.gateway.phone_number)

        return stats

    async def leave_groups(self, chat_ids: list[int], leave_delay: float = 2.0) -> int:
        left = 0
        for chat_id in chat_ids:
            try:
                if await self.gateway.leave_chat(chat_id):
                    left += 1
                    logger.info("left chat %d on %s", chat_id, self.gateway.phone_number)
                await asyncio.sleep(leave_delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("error leaving chat %d on %s", chat_id, self.gateway.phone_number)
        return left

    async def leave_all_megagroups(self) -> int:
        groups = await self.gateway.list_megagroups()
        ids = [int(g.id) for g in groups]
        logger.info("leaving %d megagroups on %s", len(ids), self.gateway.phone_number)
        return await self.leave_groups(ids, leave_delay=2.0)
