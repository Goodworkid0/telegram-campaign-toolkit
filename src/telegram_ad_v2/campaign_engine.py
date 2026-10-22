from __future__ import annotations

import asyncio
import logging
from telethon import errors, types

from .models import CampaignConfig, CampaignState, DispatchStats
from .account_manager import AccountManager
from .group_joiner import GroupJoiner
from .message_variator import MessageVariator
from .scheduler import SendScheduler
from .storage import StateStore
from .telegram_client import TelegramGateway
from .config import get_settings

logger = logging.getLogger(__name__)


class CampaignEngine:
    def __init__(
        self,
        account_mgr: AccountManager,
        store: StateStore,
        settings=None,
    ) -> None:
        self.account_mgr = account_mgr
        self.store = store
        self.settings = settings or get_settings()
        self.states: dict[str, CampaignState] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._stop_flags: dict[str, bool] = {}

    async def dispatch_once(
        self,
        campaign: CampaignConfig,
        gateway: TelegramGateway,
        scheduler: SendScheduler,
        variator: MessageVariator | None = None,
    ) -> DispatchStats:
        stats = DispatchStats()

        source_message = await gateway.get_message(
            campaign.source_chat_id, campaign.source_message_id
        )
        if source_message is None:
            raise ValueError(f"Source message not found: cid={campaign.source_chat_id} mid={campaign.source_message_id}")

        groups = await gateway.list_megagroups()
        me = await gateway.get_me()

        for group in groups:
            if campaign.target_group_ids and group.id not in campaign.target_group_ids:
                continue

            stats.attempted += 1

            already = await self.store.already_sent(
                campaign.source_chat_id, campaign.source_message_id, group.id
            )
            if already:
                stats.skipped += 1
                continue

            latest = await gateway.get_latest_message(group)
            if latest and getattr(latest, "from_id", None):
                if isinstance(latest.from_id, types.PeerUser) and latest.from_id.user_id == me.id:
                    stats.skipped += 1
                    continue

            if campaign.dry_run:
                logger.info("[%s] dry-run: would send to %s (%d)", campaign.name, group.title, group.id)
                stats.sent += 1
                continue

            await scheduler.wait_for_slot()

            variant_text = None
            if variator:
                variant_text = variator.next()

            try:
                if variant_text and not source_message.media:
                    await gateway.send_message(group, variant_text)
                else:
                    await gateway.forward_to_chat(group, source_message)

                scheduler.mark_sent()
                await self.store.mark_sent(
                    campaign.source_chat_id, campaign.source_message_id, group.id
                )
                logger.info("[%s] sent to %s (%d)", campaign.name, group.title, group.id)
                stats.sent += 1

                if campaign.auto_leave:
                    await asyncio.sleep(campaign.leave_delay_seconds)
                    joiner = GroupJoiner(gateway)
                    await joiner.leave_groups([int(group.id)])

            except errors.FloodWaitError as flood_wait:
                logger.warning("[%s] flood wait: %ss", campaign.name, flood_wait.seconds)
                await asyncio.sleep(float(flood_wait.seconds))
                stats.failed += 1
            except Exception:
                logger.exception("[%s] send failed to %s (%d)", campaign.name, group.title, group.id)
                stats.failed += 1

        return stats

    async def run_campaign_loop(
        self,
        campaign: CampaignConfig,
        gateway: TelegramGateway,
        scheduler: SendScheduler,
        variator: MessageVariator | None = None,
    ) -> None:
        state = CampaignState(name=campaign.name)
        self.states[campaign.name] = state
        self._stop_flags[campaign.name] = False

        logger.info("[%s] campaign loop started", campaign.name)

        while not self._stop_flags.get(campaign.name, True):
            try:
                stats = await self.dispatch_once(
                    campaign=campaign,
                    gateway=gateway,
                    scheduler=scheduler,
                    variator=variator,
                )
                state.cycles_completed += 1
                state.total_sent += stats.sent
                state.total_failed += stats.failed

                logger.info(
                    "[%s] cycle %d done: sent=%d skipped=%d failed=%d",
                    campaign.name,
                    state.cycles_completed,
                    stats.sent,
                    stats.skipped,
                    stats.failed,
                )

                await asyncio.sleep(campaign.loop_delay_seconds)
            except asyncio.CancelledError:
                logger.info("[%s] campaign loop cancelled", campaign.name)
                break
            except Exception:
                logger.exception("[%s] error in campaign loop, retrying in 60s", campaign.name)
                await asyncio.sleep(60)

        logger.info("[%s] campaign loop stopped", campaign.name)

    def stop_campaign(self, name: str) -> None:
        self._stop_flags[name] = True
        if name in self._running:
            self._running[name].cancel()

    def is_running(self, name: str) -> bool:
        return name in self._running and not self._running[name].done()

    def start_campaign(
        self,
        campaign: CampaignConfig,
        gateway_label: str,
        scheduler_kwargs: dict | None = None,
    ) -> asyncio.Task | None:
        gateway = self.account_mgr.get_connected(gateway_label)
        if gateway is None:
            logger.error("gateway not connected: %s", gateway_label)
            return None

        skwargs = scheduler_kwargs or {}
        scheduler = SendScheduler(
            send_interval_seconds=skwargs.pop("send_interval_seconds", self.settings.send_interval_seconds),
            jitter_min_seconds=skwargs.pop("jitter_min_seconds", self.settings.jitter_min_seconds),
            jitter_max_seconds=skwargs.pop("jitter_max_seconds", self.settings.jitter_max_seconds),
            max_messages_per_hour=skwargs.pop("max_messages_per_hour", self.settings.max_messages_per_hour),
            max_messages_per_day=skwargs.pop("max_messages_per_day", self.settings.max_messages_per_day),
            active_hours_start=skwargs.pop("active_hours_start", self.settings.active_hours_start),
            active_hours_end=skwargs.pop("active_hours_end", self.settings.active_hours_end),
        )

        variator = None
        if campaign.message_variants:
            variator = MessageVariator(variants=campaign.message_variants)

        task = asyncio.create_task(
            self.run_campaign_loop(campaign, gateway, scheduler, variator),
            name=f"campaign_{campaign.name}",
        )
        self._running[campaign.name] = task
        return task
