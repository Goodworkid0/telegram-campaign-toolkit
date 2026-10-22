from __future__ import annotations

import logging
from collections.abc import Callable

from .config import get_settings
from .models import AccountConfig
from .telegram_client import TelegramGateway

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._gateways: dict[str, TelegramGateway] = {}

    def gateway_for(self, account: AccountConfig) -> TelegramGateway:
        key = account.label or account.phone_number
        if key not in self._gateways:
            gw = TelegramGateway(
                api_id=account.api_id,
                api_hash=account.api_hash,
                phone_number=account.phone_number,
                session_dir=self.settings.session_dir,
                proxy=account.proxy,
                session_path=account.session_path,
            )
            self._gateways[key] = gw
        return self._gateways[key]

    async def connect_all(
        self,
        accounts: list[AccountConfig] | None = None,
        code_provider: Callable[[], str] | None = None,
        password_provider: Callable[[], str] | None = None,
    ) -> dict[str, TelegramGateway]:
        if accounts is None:
            accounts = self.settings.get_accounts()

        connected: dict[str, TelegramGateway] = {}
        for acc in accounts:
            gw = self.gateway_for(acc)
            try:
                await gw.connect(
                    code_provider=code_provider,
                    password_provider=password_provider,
                )
                connected[acc.label or acc.phone_number] = gw
                me = await gw.get_me()
                username = getattr(me, "username", "?") or "?"
                logger.info(
                    "connected: %s as @%s (id=%d) via %s",
                    acc.label or acc.phone_number,
                    username,
                    me.id,
                    acc.proxy.host if acc.proxy else "direct",
                )
            except Exception:
                logger.exception("failed to connect account %s", acc.label or acc.phone_number)
        return connected

    async def disconnect_all(self) -> None:
        for key, gw in self._gateways.items():
            try:
                await gw.disconnect()
                logger.info("disconnected: %s", key)
            except Exception:
                logger.exception("error disconnecting %s", key)
        self._gateways.clear()

    def get_connected(self, label: str) -> TelegramGateway | None:
        return self._gateways.get(label)
