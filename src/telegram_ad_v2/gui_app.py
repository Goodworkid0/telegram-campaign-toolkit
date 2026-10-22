from __future__ import annotations

import asyncio
from pathlib import Path

import streamlit as st

from telegram_ad_v2.config import get_settings
from telegram_ad_v2.dispatcher import dispatch_once, load_whitelist_ids
from telegram_ad_v2.scheduler import SendScheduler
from telegram_ad_v2.storage import StateStore
from telegram_ad_v2.telegram_client import TelegramGateway


def run(coro):
    return asyncio.run(coro)


st.set_page_config(page_title="telegram-ad v2", layout="wide")
settings = get_settings()

st.title("telegram-ad v2 (GUI)")

st.info(
    "First login may require a verification code (and possibly 2FA password). "
    "This GUI will ask for them below."
)

with st.sidebar:
    st.header("Auth")
    phone = st.text_input("Phone number", value=settings.phone_number)
    api_id = st.number_input("API ID", value=int(settings.api_id), step=1)
    api_hash = st.text_input("API Hash", value=settings.api_hash, type="password")
    session_dir = st.text_input("Session dir", value=str(settings.session_dir))

    code = st.text_input("Verification code (if needed)")
    password = st.text_input("2FA password (if needed)", type="password")

    st.divider()
    st.header("Files")
    legacy_groups = st.text_input("Legacy groups file", value="../assets/groups.txt")
    whitelist_file = st.text_input("Whitelist IDs file", value="scripts/allowed_groups.txt")

    st.divider()
    st.header("Dispatch")
    source_chat_id = st.text_input("Source chat ID")
    source_message_id = st.text_input("Source message ID")
    messages_limit = st.number_input("Messages to show", min_value=1, max_value=200, value=30, step=1)
    dry_run = st.checkbox("Dry run", value=True)
    confirm_send = st.checkbox("Confirm send (required if dry_run is off)", value=False)


def make_gateway() -> TelegramGateway:
    return TelegramGateway(
        api_id=int(api_id),
        api_hash=str(api_hash),
        phone_number=str(phone),
        session_dir=Path(session_dir),
    )


col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("List chats"):
        gateway = make_gateway()

        async def _list():
            await gateway.connect(
                code_provider=(lambda: code) if code else None,
                password_provider=(lambda: password) if password else None,
            )
            try:
                chats = await gateway.list_chats()
                rows = []
                for chat in chats:
                    rows.append(
                        {
                            "id": int(chat.id),
                            "title": getattr(chat, "title", "<no-title>"),
                            "type": chat.__class__.__name__,
                        }
                    )
                return rows
            finally:
                await gateway.disconnect()

        try:
            data = run(_list())
            st.dataframe(data, use_container_width=True)
        except Exception as exc:
            st.error(str(exc))

    if st.button("List messages"):
        if not source_chat_id.strip():
            st.warning("Provide a chat ID (use Source chat ID field)")
        else:
            gateway = make_gateway()

            async def _messages():
                await gateway.connect(
                    code_provider=(lambda: code) if code else None,
                    password_provider=(lambda: password) if password else None,
                )
                try:
                    return await gateway.list_recent_messages(int(source_chat_id), int(messages_limit))
                finally:
                    await gateway.disconnect()

            try:
                data = run(_messages())
                st.dataframe(data, use_container_width=True)
                st.caption("Use the 'id' column as Source message ID")
            except Exception as exc:
                st.error(str(exc))

with col2:
    if st.button("Import groups -> whitelist"):
        gateway = make_gateway()
        src = Path(legacy_groups)
        dst = Path(whitelist_file)

        async def _import():
            await gateway.connect(
                code_provider=(lambda: code) if code else None,
                password_provider=(lambda: password) if password else None,
            )
            resolved: set[int] = set()
            failed: list[str] = []
            try:
                for line in src.read_text(encoding="utf-8").splitlines():
                    candidate = line.strip()
                    if not candidate or candidate.startswith("#"):
                        continue
                    try:
                        resolved.add(await gateway.resolve_chat_id(candidate))
                    except Exception:
                        failed.append(candidate)
            finally:
                await gateway.disconnect()

            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("\n".join(str(x) for x in sorted(resolved)) + ("\n" if resolved else ""), encoding="utf-8")
            return len(resolved), len(failed), str(dst)

        try:
            ok, bad, out_path = run(_import())
            st.success(f"Resolved: {ok} | Failed: {bad} | Written: {out_path}")
        except Exception as exc:
            st.error(str(exc))

with col3:
    if st.button("Dispatch once"):
        if not source_chat_id.strip() or not source_message_id.strip():
            st.warning("Provide source chat ID and source message ID")
        elif (not dry_run) and (not confirm_send):
            st.warning("Enable Confirm send when Dry run is off")
        else:
            gateway = make_gateway()
            store = StateStore(Path(settings.sqlite_path))
            scheduler = SendScheduler(
                send_interval_seconds=float(settings.send_interval_seconds),
                jitter_min_seconds=float(settings.jitter_min_seconds),
                jitter_max_seconds=float(settings.jitter_max_seconds),
                max_messages_per_hour=int(settings.max_messages_per_hour),
            )

            async def _dispatch():
                await store.init()
                allowed = load_whitelist_ids(Path(whitelist_file))
                await gateway.connect(
                    code_provider=(lambda: code) if code else None,
                    password_provider=(lambda: password) if password else None,
                )
                try:
                    return await dispatch_once(
                        gateway=gateway,
                        store=store,
                        scheduler=scheduler,
                        source_chat_id=int(source_chat_id),
                        source_message_id=int(source_message_id),
                        allowed_target_chat_ids=allowed,
                        dry_run=bool(dry_run),
                    )
                finally:
                    await gateway.disconnect()

            try:
                stats = run(_dispatch())
                st.success(
                    f"attempted={stats.attempted} sent={stats.sent} skipped={stats.skipped} failed={stats.failed}"
                )
            except Exception as exc:
                st.error(str(exc))

with col4:
    st.caption("Tip")
    st.write("Run a dry-run first, then enable Confirm send.")
