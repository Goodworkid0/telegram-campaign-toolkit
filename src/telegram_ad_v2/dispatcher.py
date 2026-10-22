from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import errors, types

from .models import DispatchStats
from .scheduler import SendScheduler
from .storage import StateStore
from .telegram_client import TelegramGateway

logger = logging.getLogger(__name__)


def load_whitelist_ids(path: Path) -> set[int]:
    result: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        result.add(int(value))
    return result


async def dispatch_once(
    gateway: TelegramGateway,
    store: StateStore,
    scheduler: SendScheduler,
    source_chat_id: int,
    source_message_id: int,
    allowed_target_chat_ids: set[int],
    dry_run: bool,
) -> DispatchStats:
    stats = DispatchStats()

    source_message = await gateway.get_message(source_chat_id, source_message_id)
    if source_message is None:
        raise ValueError("Source message not found.")

    me = await gateway.get_me()
    groups = await gateway.list_megagroups()

    for group in groups:
        if group.id not in allowed_target_chat_ids:
            stats.skipped += 1
            continue

        stats.attempted += 1
        if await store.already_sent(source_chat_id, source_message_id, group.id):
            logger.info("skip: already sent to %s (%s)", group.title, group.id)
            stats.skipped += 1
            continue

        latest = await gateway.get_latest_message(group)
        if latest and getattr(latest, "from_id", None):
            if isinstance(latest.from_id, types.PeerUser) and latest.from_id.user_id == me.id:
                logger.info("skip: latest message already ours in %s", group.title)
                stats.skipped += 1
                continue

        if dry_run:
            logger.info("dry-run: would send to %s (%s)", group.title, group.id)
            stats.sent += 1
            continue

        await scheduler.wait_for_slot()
        try:
            await gateway.forward_to_chat(group, source_message)
            scheduler.mark_sent()
            await store.mark_sent(source_chat_id, source_message_id, group.id)
            logger.info("sent: %s (%s)", group.title, group.id)
            stats.sent += 1
        except errors.FloodWaitError as flood_wait:
            logger.warning("flood wait: sleeping %ss", flood_wait.seconds)
            await asyncio.sleep(float(flood_wait.seconds))
            stats.failed += 1
        except Exception as exc:
            logger.exception("failed: %s (%s): %s", group.title, group.id, exc)
            stats.failed += 1

    return stats
