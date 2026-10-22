from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, errors, functions, types

from .models import ProxyConfig


class TelegramGateway:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone_number: str,
        session_dir: Path,
        proxy: ProxyConfig | None = None,
        session_path: str = "",
    ) -> None:
        session_dir.mkdir(parents=True, exist_ok=True)

        if session_path:
            resolved = Path(session_path)
        else:
            clean_phone = phone_number.replace(" ", "").replace("+", "")
            resolved = session_dir / clean_phone

        self.phone_number = phone_number
        self.session_path = str(resolved)

        kwargs = dict(api_id=api_id, api_hash=api_hash)
        if proxy:
            kwargs["proxy"] = proxy.to_telethon()

        self.client = TelegramClient(self.session_path, **kwargs)

    async def connect(
        self,
        code_provider: Callable[[], str] | None = None,
        password_provider: Callable[[], str] | None = None,
    ) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.send_code_request(self.phone_number)
            code = (code_provider() if code_provider else "").strip()
            if not code:
                code = input("Verification code: ").strip()

            try:
                await self.client.sign_in(self.phone_number, code)
            except errors.SessionPasswordNeededError:
                pwd = (password_provider() if password_provider else "").strip()
                if not pwd:
                    pwd = input("2FA password: ").strip()
                await self.client.sign_in(password=pwd)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def get_me(self):
        return await self.client.get_me()

    async def list_chats(self) -> list[types.TypeChat]:
        results = await self.client(
            functions.messages.GetDialogsRequest(
                offset_date=None,
                offset_id=0,
                offset_peer=types.InputPeerEmpty(),
                limit=200,
                hash=0,
            )
        )
        return results.chats

    async def list_megagroups(self) -> list[types.Channel]:
        chats = await self.list_chats()
        return [chat for chat in chats if isinstance(chat, types.Channel) and chat.megagroup]

    async def get_message(self, chat_id: int, message_id: int):
        return await self.client.get_messages(entity=chat_id, ids=message_id)

    async def get_latest_message(self, chat: types.Channel):
        messages = await self.client.get_messages(chat, limit=1)
        if not messages:
            return None
        return messages[0]

    async def forward_to_chat(self, target_chat, message) -> None:
        await self.client.forward_messages(entity=target_chat, messages=message)

    async def get_message_views(self, chat_id: int, message_id: int) -> int:
        msg = await self.client.get_messages(entity=chat_id, ids=message_id)
        if msg:
            return getattr(msg, "views", 0) or 0
        return 0

    async def send_message(self, target_chat, text: str) -> None:
        await self.client.send_message(entity=target_chat, message=text)

    async def copy_as_new(self, target_chat, message, text_override: str | None = None) -> None:
        text = text_override if text_override is not None else (message.message or "")
        media = getattr(message, "media", None)

        if media is not None:
            dl = await self.client.download_media(message, file=bytes)
            await self.client.send_file(entity=target_chat, file=dl, caption=text)
        else:
            await self.client.send_message(entity=target_chat, message=text)

    async def list_recent_messages(self, chat_id: int, limit: int = 20):
        messages = []
        async for msg in self.client.iter_messages(entity=chat_id, limit=limit):
            is_service = isinstance(msg, types.MessageService) or getattr(msg, "action", None) is not None
            if is_service:
                continue
            text = msg.message or ""
            media = bool(getattr(msg, "media", None))
            messages.append(
                {
                    "id": int(msg.id),
                    "date": msg.date.isoformat() if getattr(msg, "date", None) else "",
                    "text": (text[:120] + "\u2026") if len(text) > 120 else text,
                    "has_media": media,
                }
            )
        return messages

    async def resolve_chat_id(self, raw_value: str) -> int:
        entity = await self.client.get_entity(self._normalize_target(raw_value))
        return int(entity.id)

    async def join_chat(self, link_or_username: str) -> tuple[bool, str]:
        target = self._normalize_target(link_or_username)
        for attempt in range(3):
            try:
                await self.client(functions.channels.JoinChannelRequest(target))
                return (True, "")
            except errors.UserAlreadyParticipantError:
                return (True, "already in")
            except errors.FloodWaitError as e:
                wait = int(e.seconds)
                if attempt < 2:
                    await asyncio.sleep(wait + 1)
                    continue
                return (False, f"flood {wait}s")
            except errors.ChannelsTooMuchError:
                return (False, "too many channels")
            except errors.ChannelsAdminPublicTooMuchError:
                return (False, "admin limit")
            except errors.InviteHashExpiredError:
                return (False, "invite expired")
            except errors.InviteHashInvalidError:
                return (False, "invalid invite")
            except errors.UserBannedInChannelError:
                return (False, "banned")
            except errors.ChatWriteForbiddenError:
                return (False, "no write access")
            except Exception as e:
                return (False, type(e).__name__)
        return (False, "retries exhausted")

    async def leave_chat(self, chat_id: int) -> bool:
        try:
            await self.client(functions.channels.LeaveChannelRequest(channel=chat_id))
            return True
        except Exception:
            return False

    async def check_chat_membership(self, chat_id: int) -> bool:
        try:
            entity = await self.client.get_entity(chat_id)
            if isinstance(entity, types.Channel):
                return entity.left is False
            return False
        except Exception:
            return False

    def _normalize_target(self, raw_value: str) -> str:
        value = raw_value.strip()
        if not value:
            raise ValueError("Empty group target value.")

        if "t.me/" in value or value.startswith("https://") or value.startswith("http://"):
            parsed = urlparse(value)
            path = parsed.path.strip("/")
            if not path:
                raise ValueError(f"Invalid Telegram URL: {raw_value}")
            if path.startswith("+"):
                return path
            return path.split("/")[0]

        return value

    async def search_groups(self, query: str, limit: int = 50) -> list[dict]:
        results = []
        try:
            found = await self.client(functions.contacts.SearchRequest(
                q=query,
                limit=limit,
            ))
            for chat in found.chats:
                if isinstance(chat, types.Channel) and chat.megagroup:
                    results.append({
                        "id": int(chat.id),
                        "title": getattr(chat, "title", "") or "",
                        "username": getattr(chat, "username", "") or "",
                        "members": getattr(chat, "participants_count", 0) or 0,
                        "joinable": bool(chat.username),
                    })
        except Exception:
            pass
        return results
