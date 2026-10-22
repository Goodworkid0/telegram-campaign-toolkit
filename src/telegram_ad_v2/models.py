from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class DispatchStats:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class JoinStats:
    attempted: int = 0
    joined: int = 0
    already_member: int = 0
    failed: int = 0


class ProxyType(str, Enum):
    SOCKS5 = "socks5"
    SOCKS4 = "socks4"
    HTTP = "http"


@dataclass
class ProxyConfig:
    proxy_type: ProxyType = ProxyType.SOCKS5
    host: str = ""
    port: int = 1080
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, raw: str) -> ProxyConfig | None:
        if not raw or not raw.strip():
            return None
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw.strip())
            proxy_type = ProxyType(parsed.scheme) if parsed.scheme else ProxyType.SOCKS5
            return cls(
                proxy_type=proxy_type,
                host=parsed.hostname or "",
                port=parsed.port or 1080,
                username=parsed.username or None,
                password=parsed.password or None,
            )
        except (ValueError, KeyError):
            return None

    def scheme(self) -> str:
        return self.proxy_type.value

    def to_telethon(self) -> tuple[str, int, str | None, str | None]:
        return (self.scheme(), self.host, self.port, self.username, self.password)


@dataclass
class AccountConfig:
    phone_number: str
    api_id: int
    api_hash: str
    session_path: str = ""
    proxy: ProxyConfig | None = None
    label: str = ""


@dataclass
class CampaignConfig:
    name: str
    source_chat_id: int
    source_message_id: int
    target_group_ids: set[int] = field(default_factory=set)
    account_labels: list[str] = field(default_factory=list)
    enabled: bool = True
    dry_run: bool = True
    auto_leave: bool = False
    leave_delay_seconds: int = 300
    loop_delay_seconds: int = 300
    message_variants: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict, name: str) -> CampaignConfig:
        return cls(
            name=name,
            source_chat_id=int(data.get("source_chat_id", 0)),
            source_message_id=int(data.get("source_message_id", 0)),
            target_group_ids=set(int(x) for x in data.get("target_group_ids", [])),
            account_labels=list(data.get("account_labels", [])),
            enabled=bool(data.get("enabled", True)),
            dry_run=bool(data.get("dry_run", True)),
            auto_leave=bool(data.get("auto_leave", False)),
            leave_delay_seconds=int(data.get("leave_delay_seconds", 300)),
            loop_delay_seconds=int(data.get("loop_delay_seconds", 300)),
            message_variants=list(data.get("message_variants", [])),
        )


@dataclass
class CampaignState:
    name: str
    last_sent_at: float = 0.0
    cycles_completed: int = 0
    total_sent: int = 0
    total_failed: int = 0
