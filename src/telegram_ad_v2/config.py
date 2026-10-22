from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import AccountConfig, CampaignConfig, ProxyConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_id: int = Field(default=0, alias="TG_API_ID")
    api_hash: str = Field(default="", alias="TG_API_HASH")
    phone_number: str = Field(default="", alias="TG_PHONE_NUMBER")

    session_dir: Path = Field(default=Path("data/sessions"), alias="TG_SESSION_DIR")
    sqlite_path: Path = Field(default=Path("data/state.db"), alias="TG_SQLITE_PATH")

    send_interval_seconds: float = Field(default=8.0, alias="TG_SEND_INTERVAL_SECONDS")
    loop_interval_seconds: float = Field(default=120.0, alias="TG_LOOP_INTERVAL_SECONDS")
    jitter_min_seconds: float = Field(default=0.5, alias="TG_JITTER_MIN_SECONDS")
    jitter_max_seconds: float = Field(default=2.0, alias="TG_JITTER_MAX_SECONDS")

    max_messages_per_hour: int = Field(default=40, alias="TG_MAX_MESSAGES_PER_HOUR")
    max_messages_per_day: int = Field(default=50, alias="TG_MAX_MESSAGES_PER_DAY")

    active_hours_start: int = Field(default=8, alias="TG_ACTIVE_HOURS_START")
    active_hours_end: int = Field(default=23, alias="TG_ACTIVE_HOURS_END")

    accounts_json: str = Field(default="", alias="TG_ACCOUNTS_JSON")
    campaigns_dir: Path = Field(default=Path("data/campaigns"), alias="TG_CAMPAIGNS_DIR")
    group_lists_dir: Path = Field(default=Path("data/group_lists"), alias="TG_GROUP_LISTS_DIR")
    default_proxy: str = Field(default="", alias="TG_DEFAULT_PROXY")

    auto_leave_after_send: bool = Field(default=False, alias="TG_AUTO_LEAVE_AFTER_SEND")
    join_warmup_seconds: float = Field(default=60.0, alias="TG_JOIN_WARMUP_SECONDS")

    def get_accounts(self) -> list[AccountConfig]:
        default_proxy = ProxyConfig.parse(self.default_proxy)
        accounts: list[AccountConfig] = []

        if self.account_json_env_is_valid():
            raw = json.loads(self.accounts_json)
            for acc in raw if isinstance(raw, list) else [raw]:
                phone = acc.get("phone_number", self.phone_number)
                api_id_val = acc.get("api_id", self.api_id)
                api_hash_val = acc.get("api_hash", self.api_hash)
                label = acc.get("label", phone)
                session_path = acc.get("session_path", "")
                proxy_raw = acc.get("proxy", "")
                proxy = ProxyConfig.parse(proxy_raw) if proxy_raw else default_proxy
                accounts.append(
                    AccountConfig(
                        phone_number=phone,
                        api_id=int(api_id_val),
                        api_hash=str(api_hash_val),
                        session_path=session_path,
                        proxy=proxy,
                        label=label,
                    )
                )

        if not accounts:
            accounts.append(
                AccountConfig(
                    phone_number=self.phone_number,
                    api_id=self.api_id,
                    api_hash=self.api_hash,
                    session_path="",
                    proxy=default_proxy,
                    label=self.phone_number,
                )
            )

        return accounts

    def account_json_env_is_valid(self) -> bool:
        raw = (self.accounts_json or "").strip()
        if not raw:
            return False
        try:
            val = json.loads(raw)
            return isinstance(val, (dict, list)) and len(val) > 0
        except (json.JSONDecodeError, TypeError):
            return False

    def load_campaigns(self) -> list[CampaignConfig]:
        import json as _json

        campaigns: list[CampaignConfig] = []
        campaign_dir = Path(self.campaigns_dir)
        if not campaign_dir.exists():
            return campaigns
        for f in sorted(campaign_dir.glob("*.json")):
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                name = f.stem
                campaigns.append(CampaignConfig.from_dict(data, name))
            except Exception:
                continue
        return campaigns


@lru_cache
def get_settings() -> Settings:
    return Settings()
