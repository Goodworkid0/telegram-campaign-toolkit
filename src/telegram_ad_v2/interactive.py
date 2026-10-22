from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import signal
import socket
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table

from .config import get_settings
from .dispatcher import load_whitelist_ids
from .group_joiner import GroupJoiner
from .message_variator import MessageVariator
from .storage import StateStore
from .telegram_client import TelegramGateway
from .models import ProxyConfig, DispatchStats, AccountConfig

console = Console()
logger = logging.getLogger(__name__)


def _parse_proxy(raw: str) -> ProxyConfig | None:
    if not raw.strip():
        return None
    p = ProxyConfig.parse(raw)
    return p if p and p.host else None


def _detect_tor() -> dict[str, str]:
    presets: dict[str, str] = {}
    for port, label in [(9050, "Tor (9050)"), (9150, "Tor Browser (9150)")]:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            presets[label] = f"socks5://127.0.0.1:{port}"
        except Exception:
            pass
    return presets


STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "on", "with",
    "is", "are", "by", "at", "from", "it", "be", "as", "no", "not", "all",
    "we", "you", "i", "me", "my", "our", "your", "this", "that", "its",
    "official", "chat", "group", "channel", "community", "team", "support",
    "news", "updates", "discussion", "free", "best", "top", "new", "pro",
    "com", "org", "net", "io", "https", "http", "www",
}


def _expand_keywords(titles: list[str], seed: list[str], max_new: int = 30) -> list[str]:
    all_words: list[str] = []
    for t in titles:
        words = re.findall(r"[a-zA-Z0-9]{3,}", t.lower())
        all_words.extend(w for w in words if w not in STOP_WORDS)

    c = Counter(all_words)
    expanded = set(seed)
    for word, _ in c.most_common(max_new * 2):
        if word not in expanded:
            expanded.add(word)
            if len(expanded) - len(seed) >= max_new:
                break

    bigrams = Counter()
    for t in titles:
        words = [w for w in re.findall(r"[a-zA-Z0-9]{3,}", t.lower()) if w not in STOP_WORDS]
        for a, b in zip(words, words[1:]):
            bigrams[f"{a} {b}"] += 1

    for phrase, _ in bigrams.most_common(max_new):
        if phrase not in expanded:
            expanded.add(phrase)
            if len(expanded) - len(seed) >= max_new * 2:
                break

    new = [k for k in expanded if k not in set(seed)]
    return new[:max_new]


# ═══════════════════════════════════════════════════════════════

class Session:
    def __init__(self) -> None:
        self.s = get_settings()
        self.store = StateStore(self.s.sqlite_path)
        self.accounts: list[AccountConfig] = []
        self.gateways: list[TelegramGateway] = []
        self.source_chat_id: int | None = None
        self.source_chat_title: str = ""
        self.source_msg_id: int | None = None
        self.source_msg_text: str = ""
        self.source_has_media: bool = False
        self.target_ids: set[int] = set()
        self._group_lists_dir = Path("data/group_lists")
        self._group_lists_dir.mkdir(parents=True, exist_ok=True)
        self._active_list: str = "default"
        self._group_db_path = Path("data/group_db.json")
        self.target_file = self._group_lists_dir / f"{self._active_list}.txt"
        self.variants: list[str] = []
        self.interval: int = 8
        self.per_hour: int = 40
        self._use_forward: bool = False
        self._reach_cache: dict[int, int] = {}
        self._total_reach: int = 0
        self._proxy: ProxyConfig | None = None
        self._campaigns_dir = self.s.campaigns_dir
        self._campaigns_dir.mkdir(parents=True, exist_ok=True)
        self._scheduler_task: asyncio.Task | None = None
        self._running = True

    async def start(self) -> None:
        console.print()
        console.print(Panel.fit("[bold cyan]Telegram Ad[/bold cyan]  v2", border_style="cyan"))
        await self.store.init()
        self._migrate_old_lists()
        self._load_saved_state()

        self._scheduler_task = asyncio.create_task(self._cron_loop())

        while self._running:
            try:
                await self._main_menu()
            except (KeyboardInterrupt, EOFError):
                self._running = False

        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        self._save_state()
        console.print("\n[dim]Bye.[/dim]")

    # ══════════════════════════════════════════════════════════

    def _migrate_old_lists(self) -> None:
        old = Path("scripts/allowed_groups.txt")
        new = self._group_lists_dir / "default.txt"
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_text(old.read_text())

    async def _main_menu(self) -> None:
        connected = sum(1 for g in self.gateways if g.client.is_connected())
        campaigns = self._list_campaign_files()

        lines = [
            f"  [1] [bold]Start Sending[/bold]          accts: {connected}  targets: {len(self.target_ids)}  msg: {self.source_msg_id or '?'}  reach: {self._total_reach:,}{'  [FWD]' if self._use_forward else ''}",
            "  [2] [bold]Dry Run[/bold]               simulate without real sends",
            f"  [3] [bold]Accounts[/bold]              setup phone numbers & proxies  (now: {len(self.accounts)})",
            f"  [4] [bold]Groups[/bold]                join / import / view targets  (now: {len(self.target_ids)})",
            f"  [5] [bold]Campaigns[/bold]             create, edit, schedule, run  (now: {len(campaigns)})",
            "  [6] [bold]Exit[/bold]",
        ]

        console.print(Panel("\n".join(lines), title="[bold]Menu[/bold]", border_style="cyan"))

        c = Prompt.ask(">", choices=["1", "2", "3", "4", "5", "6", "h"], show_choices=False, default="1")

        if c == "1":
            await self._wizard_send(dry=False)
        elif c == "2":
            await self._wizard_send(dry=True)
        elif c == "3":
            await self._menu_accounts()
        elif c == "4":
            await self._menu_groups()
        elif c == "5":
            await self._menu_campaigns()
        elif c == "h":
            await self._health_check()
        else:
            self._running = False

    # ══════════════════════════════════════════════════════════
    # Send wizard — shared between live and dry-run
    # ══════════════════════════════════════════════════════════

    async def _wizard_send(self, dry: bool) -> None:
        label = "[yellow]DRY RUN[/yellow]" if dry else "[bold red]LIVE[/bold red]"
        console.print(f"\n{label} [dim]— configure then dispatch[/dim]\n")

        # -- ensure connected --
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            console.print("[red]No accounts connected.[/red]")
            return

        # -- pick message --
        if not self.source_msg_id or Confirm.ask("Pick a new ad message?", default=False):
            await self._pick_message()

        if not self.source_msg_id:
            console.print("[red]No message selected.[/red]")
            return

        # -- targets --
        if not self.target_ids or Confirm.ask("Reload targets?", default=False):
            await self._pick_targets()

        if not self.target_ids:
            console.print("[red]No targets.[/red]")
            return

        # -- send mode --
        self._use_forward = Confirm.ask(
            "Forward from source channel? (shows views, but 'Forwarded from' header visible)",
            default=self._use_forward,
        )
        if self._use_forward:
            console.print("  [dim]Forward mode — view count tracked on source channel post[/dim]")

        # -- variants --
        if Confirm.ask("Add message variants (anti-spam spin)?", default=bool(self.variants)):
            raw = Prompt.ask("  Templates (comma-separated, {a|b} spin)", default=",".join(self.variants) if self.variants else "")
            self.variants = [v.strip() for v in raw.split(",") if v.strip()] if raw.strip() else []
            console.print(f"  [dim]{len(self.variants)} variant(s)[/dim]")

        # -- pacing --
        self.interval = IntPrompt.ask("Seconds between sends", default=self.interval)
        self.per_hour = IntPrompt.ask("Max per hour (per account)", default=self.per_hour)

        # -- confirm & send --
        console.print()
        console.print(
            f"  Accounts : [cyan]{len(self.gateways)}[/cyan]"
            f"  |  Targets : [magenta]{len(self.target_ids)}[/magenta]"
            f"  |  Variants : {'[green]on[/green]' if self.variants else '[dim]off[/dim]'}"
            f"  |  Pace : {self.interval}s / {self.per_hour}/hr"
        )

        if dry:
            console.print("\n[bold yellow]DRY RUN — no real messages sent[/bold yellow]\n")
        else:
            if not Confirm.ask(f"\n[bold red]SEND to {len(self.target_ids)} groups?[/bold red]", default=False):
                return
            if not Confirm.ask("[bold red]CONFIRM — cannot undo[/bold red]", default=False):
                return
            console.print()

        await self._dispatch(dry)

    # ══════════════════════════════════════════════════════════
    # Accounts
    # ══════════════════════════════════════════════════════════

    async def _menu_accounts(self) -> None:
        while True:
            console.print()
            console.print(Panel("[bold]Accounts[/bold]", border_style="cyan"))

            if self.accounts:
                console.print("Phone numbers:")
                for i, a in enumerate(self.accounts):
                    console.print(f"  [{i+1}] [cyan]{a.phone_number}[/cyan]  (api_id={a.api_id})")
            else:
                console.print("[dim]No accounts configured.[/dim]")

            proxy_label = f"{self._proxy.scheme()}://{self._proxy.host}:{self._proxy.port}" if self._proxy else "none"

            console.print()
            c = Prompt.ask(
                f"[1] Add phone  [2] Remove phone  [3] Proxy (now: {proxy_label})  [4] Re-auth  [5] Back",
                choices=["1", "2", "3", "4", "5"],
                default="5",
            )
            if c == "1":
                await self._add_account()
            elif c == "2" and self.accounts:
                idx = IntPrompt.ask("Remove #", default=1) - 1
                if 0 <= idx < len(self.accounts):
                    removed = self.accounts.pop(idx)
                    console.print(f"[yellow]Removed {removed.phone_number}[/yellow]")
            elif c == "3":
                await self._menu_proxy()
            elif c == "4" and self.accounts:
                await self._re_auth()
            else:
                break

    async def _re_auth(self) -> None:
        if len(self.accounts) == 1:
            idx = 0
        else:
            idx = IntPrompt.ask("Re-auth which #?", default=1) - 1

        if not (0 <= idx < len(self.accounts)):
            return

        acc = self.accounts[idx]
        console.print(f"\nRe-authenticating [cyan]{acc.phone_number}[/cyan]")
        console.print(f"Current API ID: [dim]{acc.api_id}[/dim]")

        new_api_id = IntPrompt.ask("New API ID (Enter to keep current)", default=acc.api_id)
        new_api_hash = Prompt.ask("New API hash (Enter to keep current)", default=acc.api_hash)
        new_phone = Prompt.ask("New phone (Enter to keep current)", default=acc.phone_number)

        # delete old session
        clean = acc.phone_number.replace(" ", "").replace("+", "")
        old_path = self.s.session_dir / clean
        if old_path.exists():
            old_path.unlink()
        journal = Path(str(old_path) + "-journal")
        if journal.exists():
            journal.unlink()
        console.print(f"  [dim]Session cleared: {old_path.name}[/dim]")

        self.accounts[idx] = AccountConfig(
            phone_number=new_phone,
            api_id=new_api_id,
            api_hash=new_api_hash,
            label=new_phone.replace(" ", "").replace("+", ""),
            session_path="",
        )
        console.print("[green]Updated. Reconnect on next action.[/green]")
        self._save_state()

    async def _add_account(self) -> None:
        phone = Prompt.ask("Phone number (+...)")
        if not phone.startswith("+"):
            phone = "+" + phone
        api_id = IntPrompt.ask("API ID", default=self.s.api_id)
        api_hash = Prompt.ask("API hash", default=self.s.api_hash)

        self.accounts.append(AccountConfig(
            phone_number=phone,
            api_id=api_id,
            api_hash=api_hash,
            label=phone.replace(" ", "").replace("+", ""),
            session_path="",
        ))
        console.print(f"[green]Added {phone}[/green]")
        console.print("[dim]API keys are per-app (my.telegram.org) — same key works for all accounts.[/dim]")

    async def _menu_proxy(self) -> None:
        console.print()
        console.print(Panel("[bold]Proxy Setup[/bold]", border_style="cyan"))

        current = f"{self._proxy.scheme()}://{self._proxy.host}:{self._proxy.port}" if self._proxy else "none"
        console.print(f"Current proxy: [cyan]{current}[/cyan]")

        tor = _detect_tor()
        if tor:
            console.print("\n[green]Tor detected:[/green]")
            for label, url in tor.items():
                console.print(f"  {url}  ({label})")
            if Confirm.ask("Use Tor?", default=True):
                urls = list(tor.values())
                url = urls[0] if len(urls) == 1 else urls[IntPrompt.ask("Pick port #", default=1) - 1]
                self._proxy = _parse_proxy(url)
                console.print(f"[green]Proxy set: {url}[/green]")
        else:
            console.print("[dim]Tor not detected.[/dim]")

        c = Prompt.ask("\n[1] Manual proxy URL  [2] Clear proxy  [3] Test proxy  [4] Back", choices=["1", "2", "3", "4"], default="4")
        if c == "1":
            raw = Prompt.ask("Proxy URL (socks5://host:port)")
            self._proxy = _parse_proxy(raw)
            console.print("[green]Set.[/green]")
        elif c == "2":
            self._proxy = None
            console.print("[yellow]Proxy cleared.[/yellow]")
        elif c == "3":
            await self._proxy_test()

    async def _proxy_test(self) -> None:
        if not self._proxy:
            console.print("[yellow]No proxy configured.[/yellow]")
            return

        console.print(f"[cyan]Testing {self._proxy.host}:{self._proxy.port}...[/cyan]")
        try:
            import socks
            s = socks.socksocket()
            s.set_proxy(
                getattr(socks, f"PROXY_TYPE_{self._proxy.proxy_type.name}", socks.PROXY_TYPE_SOCKS5),
                self._proxy.host, self._proxy.port,
            )
            s.settimeout(5)
            s.connect(("api.telegram.org", 443))
            s.close()
            console.print("[green]Proxy OK — can reach Telegram[/green]")
        except Exception as exc:
            console.print(f"[red]Proxy FAILED: {exc}[/red]")

    async def _connect_accounts(self) -> None:
        if not self.accounts:
            loaded = self.s.get_accounts()
            if loaded:
                for a in loaded:
                    if a.phone_number and a.api_id:
                        self.accounts.append(AccountConfig(
                            phone_number=a.phone_number,
                            api_id=a.api_id,
                            api_hash=a.api_hash,
                            label=a.phone_number.replace(" ", "").replace("+", ""),
                            session_path=a.session_path,
                        ))
            if not self.accounts:
                await self._add_account()
                if not self.accounts:
                    return
                self.accounts = self.accounts[-1:]

        console.print(f"\n[cyan]Connecting {len(self.accounts)} account(s)...[/cyan]")
        self.gateways.clear()

        for acc in self.accounts:
            proxy = acc.proxy or self._proxy
            gw = TelegramGateway(
                api_id=acc.api_id,
                api_hash=acc.api_hash,
                phone_number=acc.phone_number,
                session_dir=self.s.session_dir,
                proxy=proxy,
                session_path=acc.session_path,
            )
            try:
                await gw.connect()
                me = await gw.get_me()
                uname = getattr(me, "username", "?") or "?"
                console.print(f"  [green]{acc.label}[/green] → @{uname}")
                self.gateways.append(gw)
            except Exception as exc:
                console.print(f"  [red]{acc.label}[/red] → {exc}")

        self._save_accounts_state()

    # ══════════════════════════════════════════════════════════
    # Groups
    # ══════════════════════════════════════════════════════════

    async def _menu_groups(self) -> None:
        while True:
            console.print()
            console.print(Panel("[bold]Groups[/bold]", border_style="cyan"))

            lists = sorted(self._group_lists_dir.glob("*.txt"))
            list_names = [f.stem for f in lists]

            console.print(f"Active list: [bold cyan]{self._active_list}[/bold cyan]  ({len(self.target_ids)} targets)")
            if list_names:
                console.print("Available lists:")
                for name in list_names:
                    count = len(load_whitelist_ids(self._group_lists_dir / f"{name}.txt"))
                    marker = "[bold]← active[/bold]" if name == self._active_list else ""
                    console.print(f"  [cyan]{name}[/cyan]  ({count} groups) {marker}")

            console.print()
            c = Prompt.ask(
                "[1] Join from file  [2] View targets  [3] Lists  [4] Discover  [5] Back",
                choices=["1", "2", "3", "4", "5"],
                default="5",
            )

            if c == "1":
                await self._join_groups()
            elif c == "2":
                await self._view_targets()
            elif c == "3":
                await self._menu_group_lists()
            elif c == "4":
                await self._menu_discover()
            else:
                break

    async def _menu_group_lists(self) -> None:
        while True:
            console.print()
            console.print(Panel("[bold]Group Lists[/bold]", border_style="cyan"))

            lists = sorted(self._group_lists_dir.glob("*.txt"))
            for i, f in enumerate(lists):
                count = len(load_whitelist_ids(f))
                active = " ← active" if f.stem == self._active_list else ""
                console.print(f"  [{i+1}] [cyan]{f.stem}[/cyan] ({count} groups){active}")

            console.print()
            c = Prompt.ask(
                "[1] Create new  [2] Load (switch)  [3] Delete  [4] Back",
                choices=["1", "2", "3", "4"],
                default="4",
            )

            if c == "1":
                name = Prompt.ask("List name (e.g. crypto, spam)", default="")
                if name.strip():
                    self._active_list = name
                    self.target_file = self._group_lists_dir / f"{name}.txt"
                    self.target_ids = set()
                    self.target_file.write_text("")
                    console.print(f"[green]Created & activated '{name}'[/green]")
            elif c == "2" and lists:
                pick = IntPrompt.ask("Load #", default=1) - 1
                if 0 <= pick < len(lists):
                    name = lists[pick].stem
                    self._active_list = name
                    self.target_file = lists[pick]
                    self.target_ids = load_whitelist_ids(self.target_file)
                    console.print(f"[green]Loaded '{name}' — {len(self.target_ids)} targets[/green]")
            elif c == "3" and lists:
                pick = IntPrompt.ask("Delete #", default=1) - 1
                if 0 <= pick < len(lists):
                    name = lists[pick].stem
                    lists[pick].unlink()
                    console.print(f"[yellow]Deleted '{name}'[/yellow]")
                    if name == self._active_list:
                        remaining = sorted(self._group_lists_dir.glob("*.txt"))
                        if remaining:
                            self._active_list = remaining[0].stem
                            self.target_file = remaining[0]
                            self.target_ids = load_whitelist_ids(self.target_file)
                        else:
                            self._active_list = "default"
                            self.target_file = self._group_lists_dir / "default.txt"
                            self.target_ids = set()
            else:
                break

    async def _join_groups(self) -> None:
        source = Prompt.ask("File with invite links", default="../assets/groups.txt")
        path = Path(source)
        if not path.exists():
            console.print(f"[red]Not found: {path}[/red]")
            return

        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        gw = self.gateways[0]
        max_j = IntPrompt.ask("Max joins this session", default=50)

        console.print("[cyan]Joining...[/cyan]")
        joiner = GroupJoiner(gw, max_joins_per_session=max_j)
        stats = await joiner.join_from_file(path)
        console.print(f"  joined={stats.joined}  already={stats.already_member}  failed={stats.failed}")

        console.print("[cyan]Resolving IDs...[/cyan]")
        resolved: set[int] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                resolved.add(await gw.resolve_chat_id(line))
            except Exception:
                pass

        self.target_ids |= resolved
        self.target_file.parent.mkdir(parents=True, exist_ok=True)
        self.target_file.write_text("\n".join(str(x) for x in sorted(self.target_ids)) + "\n")
        console.print(f"[green]{len(resolved)} new targets added → {len(self.target_ids)} total[/green]")

    async def _menu_discover(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        gw = self.gateways[0]

        console.print()
        c = Prompt.ask(
            "[1] Quick search   [2] Mass discover (auto-expand keywords, build DB)   [3] View DB   [4] Back",
            choices=["1", "2", "3", "4"],
            default="1",
        )

        if c == "1":
            await self._quick_discover(gw)
        elif c == "2":
            await self._mass_discover(gw)
        elif c == "3":
            await self._view_group_db()
        else:
            pass

    async def _quick_discover(self, gw) -> None:

        keywords = Prompt.ask(
            "Keywords (comma-separated, e.g. crypto, btc, nft)",
            default="crypto",
        )

        all_results: list[dict] = []
        seen: set[int] = set()

        for kw in [k.strip() for k in keywords.split(",") if k.strip()]:
            console.print(f"[cyan]Searching '{kw}'...[/cyan]")
            results = await gw.search_groups(kw, limit=30)
            new = 0
            for r in results:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    r["keyword"] = kw
                    all_results.append(r)
                    new += 1
            console.print(f"  found {len(results)}, {new} new")

        if not all_results:
            console.print("[yellow]No joinable groups found for those keywords.[/yellow]")
            return

        all_results.sort(key=lambda r: r.get("members", 0), reverse=True)

        table = Table(title=f"Discovered Groups ({len(all_results)})")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="green")
        table.add_column("Members", style="magenta", justify="right")
        table.add_column("Username", style="cyan")
        table.add_column("Keyword", style="dim")

        for i, r in enumerate(all_results[:50]):
            members = f"{r['members']:,}" if r["members"] else "?"
            table.add_row(
                str(i + 1), r["title"], members,
                f"@{r['username']}" if r["username"] else "—",
                r.get("keyword", ""),
            )

        console.print(table)

        console.print(
            "\n[dim]Enter #s to join (e.g. 1,3,5-8), or 'all' to join all, or Enter to skip[/dim]"
        )
        pick = Prompt.ask("Join which?", default="")

        if not pick.strip():
            return

        to_join: list[int] = []
        if pick.lower() == "all":
            to_join = list(range(min(20, len(all_results))))
        else:
            for part in pick.split(","):
                part = part.strip()
                if "-" in part:
                    try:
                        a, b = part.split("-", 1)
                        to_join.extend(range(int(a) - 1, int(b)))
                    except ValueError:
                        pass
                else:
                    try:
                        to_join.append(int(part) - 1)
                    except ValueError:
                        pass

        joined = 0
        failed = 0
        reasons: dict[str, int] = {}
        for idx in to_join:
            if not (0 <= idx < len(all_results)):
                continue
            r = all_results[idx]
            username = r.get("username", "")
            if not username:
                continue

            ok, reason = await gw.join_chat(username)
            if ok:
                target_id = r["id"]
                self.target_ids.add(target_id)

                self.target_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.target_file, "a") as f:
                    f.write(f"{target_id}\n")

                tag = " [dim](already in)[/dim]" if reason else ""
                console.print(f"  [green]✓[/green] {r['title']}{tag}")
                joined += 1
                await asyncio.sleep(random.uniform(1.5, 4))
            else:
                failed += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                if reason == "already in":
                    target_id = r["id"]
                    self.target_ids.add(target_id)
                    with open(self.target_file, "a") as f:
                        f.write(f"{target_id}\n")
                    joined += 1
                else:
                    console.print(f"  [red]✗[/red] {r['title']}  [dim]({reason})[/dim]")

        summary = ", ".join(f"{c}x {r}" for r, c in sorted(reasons.items()))
        console.print(f"\n[green]Joined {joined}[/green]  failed {failed} → added to '{self._active_list}' ({len(self.target_ids)} total)")
        if summary:
            console.print(f"[dim]Reasons: {summary}[/dim]")

    def _load_group_db(self) -> dict:
        if self._group_db_path.exists():
            try:
                return json.loads(self._group_db_path.read_text())
            except Exception:
                pass
        return {}

    def _save_group_db(self, db: dict) -> None:
        self._group_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._group_db_path.write_text(json.dumps(db, indent=2))

    async def _mass_discover(self, gw) -> None:
        seed = Prompt.ask(
            "Seed keywords (comma-separated)",
            default="crypto, btc, eth, bitcoin, trading, nft, defi, airdrop, token, solana",
        )
        seeds = set(k.strip() for k in seed.split(",") if k.strip())
        depth = IntPrompt.ask("Expansion depth (1=seed only, 2=seed+related, 3=deep)", default=2, **{})

        console.print(f"\n[bold cyan]Mass Discovery[/bold cyan]  seeds={len(seeds)}  depth={depth}")
        console.print("[dim]Building group database...[/dim]\n")

        db = self._load_group_db()
        seen_ids: set[int] = set(int(k) for k in db.keys() if k.lstrip("-").isdigit())
        all_found: list[dict] = []
        current_keywords = list(seeds)
        titles_seen: list[str] = []

        for level in range(depth):
            level_found = 0

            for i, kw in enumerate(current_keywords):
                if not kw.strip():
                    continue
                console.print(f"  [L{level+1}] '{kw}'...", end=" ")
                try:
                    results = await gw.search_groups(kw, limit=50)
                except Exception:
                    console.print("[red]error[/red]")
                    await asyncio.sleep(1)
                    continue

                new = 0
                for r in results:
                    rid = r["id"]
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        r["_keyword"] = kw
                        r["_level"] = level + 1
                        all_found.append(r)
                        db[str(rid)] = {
                            "id": rid,
                            "title": r["title"],
                            "username": r.get("username", ""),
                            "members": r.get("members", 0),
                            "joinable": r.get("joinable", False),
                            "keyword": kw,
                            "level": level + 1,
                        }
                        titles_seen.append(r["title"])
                        new += 1
                        level_found += 1

                console.print(f"{len(results)} found, [green]{new} new[/green]")
                await asyncio.sleep(random.uniform(0.8, 1.5))

            if level_found == 0:
                console.print(f"  [dim]no new groups at depth {level+1}, stopping[/dim]")
                break

            self._save_group_db(db)

            if level < depth - 1:
                expanded = _expand_keywords(titles_seen, seeds, max_new=15)
                current_keywords = [k for k in expanded if k not in seeds]
                console.print(f"  [dim]expanded keywords: {', '.join(current_keywords[:10])}[/dim]\n")
                seeds.update(current_keywords)

        console.print(f"\n[green]Database: {len(db)} groups total[/green] ({len(all_found)} new this session)")

        if all_found:
            console.print()
            c = Prompt.ask(
                f"[1] Join ALL {len(all_found)} found  [2] Pick manually  [3] Just save to DB  [4] Back",
                choices=["1", "2", "3", "4"],
                default="1",
            )
            if c == "1":
                to_join = list(range(len(all_found)))
                await self._do_join(all_found, to_join, gw)
            elif c == "2":
                self._show_discover_results(all_found, gw)
            elif c == "3":
                console.print("[dim]Saved to DB. Use View DB later to join.[/dim]")

    async def _view_group_db(self) -> None:
        db = self._load_group_db()
        if not db:
            console.print("[yellow]Database is empty. Run mass discover first.[/yellow]")
            return

        entries = sorted(db.values(), key=lambda x: x.get("members", 0), reverse=True)

        table = Table(title=f"Group Database ({len(entries)})")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="green")
        table.add_column("Members", style="magenta", justify="right")
        table.add_column("Username", style="cyan")
        table.add_column("Keyword", style="dim")

        for i, e in enumerate(entries[:60]):
            members = f"{e.get('members', 0):,}" if e.get("members") else "?"
            table.add_row(
                str(i + 1), e.get("title", ""), members,
                f"@{e.get('username', '')}" if e.get("username") else "—",
                e.get("keyword", ""),
            )

        console.print(table)
        console.print(f"[dim]Showing top 60 of {len(entries)}. Total reach: {sum(e.get('members', 0) or 0 for e in entries):,}[/dim]")

        if Confirm.ask("\nJoin groups from DB?", default=False):
            pick = Prompt.ask(
                "Join which? (#s, range 1-5, 'all' for all, or keyword filter like 'crypto')",
                default="",
            )
            if not pick.strip():
                return

            if any(c.isalpha() for c in pick) and pick not in ("all", ""):
                filtered = [e for e in entries if pick.lower() in e.get("keyword", "").lower() or pick.lower() in e.get("title", "").lower()]
                console.print(f"[dim]Filter '{pick}': {len(filtered)} groups[/dim]")
                results = [{"id": e["id"], "title": e.get("title", ""), "username": e.get("username", ""), "members": e.get("members", 0)} for e in filtered]
            else:
                results = [{"id": e["id"], "title": e.get("title", ""), "username": e.get("username", ""), "members": e.get("members", 0)} for e in entries]

            to_join = self._parse_join_selection(pick, len(results))
            if not to_join:
                return

            if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
                await self._connect_accounts()
            if not self.gateways:
                return

            await self._do_join(results, to_join, self.gateways[0])

    async def _show_discover_results(self, results: list[dict], gw) -> None:
        results.sort(key=lambda r: r.get("members", 0), reverse=True)

        table = Table(title=f"Discovered ({len(results)})")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="green")
        table.add_column("Members", style="magenta", justify="right")
        table.add_column("Username", style="cyan")
        table.add_column("KW", style="dim")

        for i, r in enumerate(results[:50]):
            members = f"{r.get('members', 0):,}" if r.get("members") else "?"
            table.add_row(
                str(i + 1), r["title"], members,
                f"@{r.get('username', '')}" if r.get("username") else "—",
                r.get("_keyword", r.get("keyword", "")),
            )

        console.print(table)

        pick = Prompt.ask(
            "Join which? (#s, range 1-5, 'all' for first 20, Enter to skip)",
            default="",
        )
        if not pick.strip():
            return

        to_join = self._parse_join_selection(pick, len(results))
        await self._do_join(results, to_join, gw)

    def _parse_join_selection(self, pick: str, max_val: int) -> list[int]:
        if pick.lower() == "all":
            return list(range(min(20, max_val)))

        indices: list[int] = []
        for part in pick.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    indices.extend(range(int(a) - 1, int(b)))
                except ValueError:
                    pass
            else:
                try:
                    indices.append(int(part) - 1)
                except ValueError:
                    pass
        return [i for i in indices if 0 <= i < max_val]

    async def _do_join(self, results: list[dict], to_join: list[int], gw) -> None:
        joined = 0
        failed = 0
        reasons: dict[str, int] = {}

        for idx in to_join:
            r = results[idx]
            username = r.get("username", "")
            if not username:
                continue

            ok, reason = await gw.join_chat(username)
            if ok:
                target_id = r["id"]
                self.target_ids.add(target_id)
                self.target_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.target_file, "a") as f:
                    f.write(f"{target_id}\n")
                tag = " [dim](already in)[/dim]" if reason else ""
                console.print(f"  [green]✓[/green] {r['title']}{tag}")
                joined += 1
                await asyncio.sleep(random.uniform(1.5, 4))
            else:
                failed += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                if reason == "already in":
                    target_id = r["id"]
                    self.target_ids.add(target_id)
                    with open(self.target_file, "a") as f:
                        f.write(f"{target_id}\n")
                    joined += 1
                else:
                    console.print(f"  [red]✗[/red] {r['title']}  [dim]({reason})[/dim]")

        summary = ", ".join(f"{c}x {r}" for r, c in sorted(reasons.items()))
        console.print(f"\n[green]Joined {joined}[/green]  failed {failed} → '{self._active_list}' ({len(self.target_ids)} total)")
        if summary:
            console.print(f"[dim]Reasons: {summary}[/dim]")

    async def _view_targets(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        gw = self.gateways[0]
        groups = await gw.list_megagroups()

        await self._load_reach_cache()

        table = Table(title="Your Groups")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Members", style="magenta", justify="right")
        table.add_column("Target", style="white")

        for g in groups:
            title = getattr(g, "title", "") or ""
            members = self._reach_cache.get(int(g.id), 0)
            is_target = "YES" if g.id in self.target_ids else "—"
            table.add_row(str(g.id), title, f"{members:,}" if members else "?", is_target)

        console.print(table)

    async def _pick_targets(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        gw = self.gateways[0]

        console.print()
        c = Prompt.ask(
            f"Target source: [1] All my megagroups  [2] Active list ({self._active_list}: {len(self.target_ids)})  [3] Manual IDs  [4] Switch list",
            choices=["1", "2", "3", "4"],
            default="2",
        )

        if c == "1":
            groups = await gw.list_megagroups()
            self.target_ids = {int(g.id) for g in groups}
            console.print(f"[green]{len(self.target_ids)} groups from megagroups.[/green]")
        elif c == "2":
            if self.target_file.exists():
                self.target_ids = load_whitelist_ids(self.target_file)
                console.print(f"[green]{len(self.target_ids)} loaded from {self._active_list}.[/green]")
            else:
                console.print(f"[yellow]List '{self._active_list}' is empty.[/yellow]")
        elif c == "3":
            raw = Prompt.ask("Chat IDs (comma or space separated)")
            ids = raw.replace(",", " ").split()
            self.target_ids = {int(x) for x in ids if x.strip().lstrip("-").isdigit()}
            console.print(f"[green]{len(self.target_ids)} added.[/green]")
        else:
            lists = sorted(self._group_lists_dir.glob("*.txt"))
            if lists:
                console.print()
                for i, f in enumerate(lists):
                    count = len(load_whitelist_ids(f))
                    console.print(f"  [{i+1}] {f.stem} ({count} groups)")
                pick = IntPrompt.ask("Switch to #", default=1) - 1
                if 0 <= pick < len(lists):
                    name = lists[pick].stem
                    self._active_list = name
                    self.target_file = lists[pick]
                    self.target_ids = load_whitelist_ids(self.target_file)
                    console.print(f"[green]Switched to '{name}' — {len(self.target_ids)} targets[/green]")
            else:
                console.print("[yellow]No lists yet. Create one in Groups → Lists.[/yellow]")

    async def _load_reach_cache(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            return
        gw = self.gateways[0]
        groups = await gw.list_megagroups()
        self._reach_cache = {}
        for g in groups:
            count = getattr(g, "participants_count", 0) or 0
            if count > 0:
                self._reach_cache[int(g.id)] = count

    # ══════════════════════════════════════════════════════════
    # Pick message
    # ══════════════════════════════════════════════════════════

    async def _pick_message(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            console.print("[red]No accounts connected. Go to [3] Accounts first.[/red]")
            return

        gw = self.gateways[0]
        console.print("[cyan]Loading chats...[/cyan]")
        chats = await gw.list_chats()

        if not chats:
            console.print("[yellow]No chats found on this account. Join some groups first.[/yellow]")
            return

        table = Table(title="Pick Source Chat")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="green")
        table.add_column("Type", style="magenta")

        indexed = list(chats)
        for i, chat in enumerate(indexed):
            title = getattr(chat, "title", "<no-title>") or "<no-title>"
            table.add_row(str(i + 1), title, chat.__class__.__name__)

        console.print(table)
        pick = Prompt.ask("Chat #", default="")
        if not pick.strip():
            return

        try:
            chat = indexed[int(pick) - 1]
            self.source_chat_id = int(chat.id)
            self.source_chat_title = getattr(chat, "title", "") or f"id={chat.id}"
        except (ValueError, IndexError):
            console.print("[red]Invalid[/red]")
            return

        limit = IntPrompt.ask("Messages to show", default=20)
        msgs = await gw.list_recent_messages(self.source_chat_id, limit=limit)

        if not msgs:
            console.print("[yellow]No messages in this chat.[/yellow]")
            return

        table = Table(title=f"Messages — {self.source_chat_title}")
        table.add_column("#", style="dim", width=4)
        table.add_column("ID", style="cyan")
        table.add_column("Text", style="white", max_width=50)
        table.add_column("Media", width=6)

        for i, m in enumerate(msgs):
            table.add_row(str(i + 1), str(m["id"]), m["text"], "*" if m["has_media"] else "")

        console.print(table)
        pick = Prompt.ask("Message #", default="")
        if not pick.strip():
            return

        try:
            m = msgs[int(pick) - 1]
            self.source_msg_id = m["id"]
            self.source_msg_text = m["text"]
            self.source_has_media = m["has_media"]
        except (ValueError, IndexError):
            console.print("[red]Invalid[/red]")
            return

        console.print(f"[green]Selected:[/green] {self.source_msg_text[:80]}")

    # ══════════════════════════════════════════════════════════
    # Dispatch
    # ══════════════════════════════════════════════════════════

    async def _dispatch(self, dry: bool) -> None:
        variator = MessageVariator(variants=self.variants) if self.variants else None
        source_msg = None
        stats = DispatchStats()
        targets = list(self.target_ids)
        total = len(targets)

        if not self._reach_cache:
            await self._load_reach_cache()
        reach_sent = 0

        for i, tid in enumerate(targets):
            gw = self.gateways[i % len(self.gateways)]

            stats.attempted += 1

            group_reach = self._reach_cache.get(tid, 0)

            if await self.store.already_sent(self.source_chat_id, self.source_msg_id, tid):
                stats.skipped += 1
                continue

            if dry:
                reach_sent += group_reach
                console.print(f"  [{gw.phone_number[-4:]}] [dim]dry → {tid}[/dim]  [dim]({group_reach:,} members)[/dim]")
                stats.sent += 1
                continue

            await self._pace(gw)

            if source_msg is None:
                source_msg = await gw.get_message(self.source_chat_id, self.source_msg_id)
                if source_msg is None:
                    console.print("[red]Source message gone. Pick another.[/red]")
                    self.source_msg_id = None
                    await self._pick_message()
                    if not self.source_msg_id:
                        return
                    source_msg = await gw.get_message(self.source_chat_id, self.source_msg_id)
                    if source_msg is None:
                        return

                from telethon import types as tt
                if isinstance(source_msg, tt.MessageService):
                    console.print("[red]That message is a service event (pin, join, etc). Pick another.[/red]")
                    self.source_msg_id = None
                    await self._pick_message()
                    if not self.source_msg_id:
                        return
                    source_msg = await gw.get_message(self.source_chat_id, self.source_msg_id)
                    if source_msg is None or isinstance(source_msg, tt.MessageService):
                        return

            text = variator.next() if variator else None

            try:
                if self._use_forward:
                    await gw.forward_to_chat(tid, source_msg)
                elif text and not self.source_has_media:
                    await gw.send_message(tid, text)
                else:
                    await gw.copy_as_new(tid, source_msg, text_override=text)

                await self.store.mark_sent(self.source_chat_id, self.source_msg_id, tid)
                reach_sent += group_reach
                stats.sent += 1
                members = f"({group_reach:,}m)" if group_reach else ""
                console.print(f"  [{gw.phone_number[-4:]}] [green]→ {tid}[/green] {members}  ({i+1}/{total})")
            except Exception as exc:
                cls = exc.__class__.__name__
                msg_str = str(exc)
                if "Flood" in cls:
                    wait = getattr(exc, "seconds", 30)
                    console.print(f"  [{gw.phone_number[-4:]}] [yellow]flood {wait}s[/yellow]")
                    await asyncio.sleep(wait)
                elif "banned" in msg_str.lower() and ("supergroup" in msg_str.lower() or "channel" in msg_str.lower()):
                    console.print(f"  [{gw.phone_number[-4:]}] [red]BANNED from sending. This account is done for now.[/red]")
                    gw._banned = True
                    break
                else:
                    console.print(f"  [{gw.phone_number[-4:]}] [red]fail: {exc}[/red]")
                stats.failed += 1

        self._total_reach += reach_sent

        table = Table(title="Results")
        table.add_column("Sent", style="green")
        table.add_column("Skipped", style="yellow")
        table.add_column("Failed", style="red")
        table.add_column("Reach", style="cyan")
        table.add_row(
            str(stats.sent), str(stats.skipped), str(stats.failed),
            f"[bold]{reach_sent:,}[/bold]" if reach_sent else "—",
        )

        secondary = Table(title="Cumulative Totals")
        secondary.add_column("Total Sent", style="green")
        secondary.add_column("Total Reach", style="bold cyan")
        secondary.add_row(
            str(stats.sent),
            f"[bold]{self._total_reach:,}[/bold]" if self._total_reach else "—",
        )

        console.print()
        console.print(table)
        console.print(secondary)

        if self._use_forward and not dry and self.source_chat_id and self.source_msg_id:
            views = await self.gateways[0].get_message_views(
                self.source_chat_id, self.source_msg_id
            )
            console.print(f"[cyan]Source post views: {views:,}[/cyan]")
        console.print()

    async def _pace(self, gw: TelegramGateway) -> None:
        if not hasattr(gw, "_sent_ts"):
            gw._sent_ts: deque[float] = deque()
            gw._last_send = 0.0
        ts = gw._sent_ts
        now = time.monotonic()

        while ts and now - ts[0] > 3600:
            ts.popleft()

        while len(ts) >= self.per_hour:
            w = max(0.0, 3600 - (now - ts[0]))
            await asyncio.sleep(min(w, 30))
            now = time.monotonic()
            while ts and now - ts[0] > 3600:
                ts.popleft()

        elapsed = now - gw._last_send
        if elapsed < self.interval:
            await asyncio.sleep(self.interval - elapsed + random.uniform(0.5, 2.0))

        gw._last_send = time.monotonic()
        ts.append(gw._last_send)

    # ══════════════════════════════════════════════════════════
    # Campaigns
    # ══════════════════════════════════════════════════════════

    def _list_campaign_files(self) -> list[Path]:
        if not self._campaigns_dir.exists():
            return []
        return sorted(self._campaigns_dir.glob("*.json"))

    def _load_campaign(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def _save_campaign(self, name: str, data: dict) -> None:
        self._campaigns_dir.mkdir(parents=True, exist_ok=True)
        (self._campaigns_dir / f"{name}.json").write_text(json.dumps(data, indent=2))

    async def _menu_campaigns(self) -> None:
        while True:
            console.print()
            console.print(Panel("[bold]Campaigns[/bold]", border_style="cyan"))
            files = self._list_campaign_files()

            if files:
                for i, f in enumerate(files):
                    try:
                        d = self._load_campaign(f)
                        sched = d.get("schedule", "")
                        sched_str = f" [dim]({sched})[/dim]" if sched else ""
                        dry = " [yellow]DRY[/yellow]" if d.get("dry_run", True) else " [red]LIVE[/red]"
                        enabled = "" if d.get("enabled", True) else " [red]DISABLED[/red]"
                        console.print(f"  [{i+1}] [cyan]{f.stem}[/cyan]{dry}{enabled}{sched_str}  → {len(d.get('target_group_ids',[]))} targets")
                    except Exception:
                        console.print(f"  [{i+1}] [red]{f.stem} (broken)[/red]")
            else:
                console.print("[dim]No campaigns yet.[/dim]")

            console.print()
            c = Prompt.ask(
                "[1] Create  [2] Edit  [3] Delete  [4] Run now  [5] Schedule  [6] Back",
                choices=["1", "2", "3", "4", "5", "6"],
                default="6",
            )

            if c == "1":
                await self._campaign_create()
            elif c in ("2", "3", "4", "5") and files:
                pick = IntPrompt.ask("Campaign #", default=1) - 1
                if 0 <= pick < len(files):
                    if c == "2":
                        await self._campaign_edit(files[pick])
                    elif c == "3":
                        files[pick].unlink()
                        console.print("[yellow]Deleted.[/yellow]")
                    elif c == "4":
                        await self._campaign_run(files[pick])
                    elif c == "5":
                        await self._campaign_schedule(files[pick])
            else:
                break

    async def _campaign_create(self) -> None:
        # ensure accounts connected
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            console.print("[cyan]No accounts connected, connecting first...[/cyan]")
            await self._connect_accounts()
        if not self.gateways:
            console.print("[red]Need at least one account. Go to [3] Accounts first.[/red]")
            return

        name = Prompt.ask("Campaign name", default="campaign_1")
        if not name.strip():
            return

        # pick message
        if self.source_msg_id:
            console.print(f"[dim]Current message: #{self.source_msg_id} — {self.source_msg_text[:60]}[/dim]")
        if not self.source_msg_id or Confirm.ask("Pick a new ad message?", default=not bool(self.source_msg_id)):
            await self._pick_message()
        if not self.source_msg_id:
            console.print("[red]No message selected. Aborted.[/red]")
            return

        # pick targets
        if not self.target_ids or Confirm.ask("Pick new targets?", default=False):
            await self._pick_targets()

        variants_raw = Prompt.ask(
            "Message variants (comma-sep, {a|b} spin)",
            default=",".join(self.variants) if self.variants else "",
        )
        variants = [v.strip() for v in variants_raw.split(",") if v.strip()] if variants_raw.strip() else []

        dry = Confirm.ask("Dry run mode?", default=True)
        loop = IntPrompt.ask("Loop delay (seconds between cycles)", default=600)
        auto_leave = Confirm.ask("Auto-leave after send?", default=False)
        leave_delay = IntPrompt.ask("  Leave after N seconds", default=300) if auto_leave else 300

        data = {
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_msg_id,
            "target_group_ids": sorted(self.target_ids),
            "account_labels": [a.label for a in self.accounts],
            "enabled": True,
            "dry_run": dry,
            "auto_leave": auto_leave,
            "leave_delay_seconds": leave_delay,
            "loop_delay_seconds": loop,
            "message_variants": variants,
            "schedule": "",
        }

        self._save_campaign(name, data)
        console.print(f"[green]Campaign '{name}' created.[/green]")

    async def _campaign_edit(self, path: Path) -> None:
        data = self._load_campaign(path)
        name = path.stem

        console.print(f"\nEditing [cyan]{name}[/cyan]")
        console.print(f"  Source: {data['source_chat_id']}:{data['source_message_id']}")
        console.print(f"  Targets: {len(data.get('target_group_ids', []))}")
        console.print(f"  Dry run: {data.get('dry_run', True)}")
        console.print(f"  Loop: {data.get('loop_delay_seconds', 600)}s")
        console.print(f"  Auto-leave: {data.get('auto_leave', False)}")
        console.print(f"  Schedule: {data.get('schedule', 'none')}")

        if Confirm.ask("\nToggle dry run?", default=False):
            data["dry_run"] = not data.get("dry_run", True)
            console.print(f"  → {data['dry_run']}")
        if Confirm.ask("Toggle auto-leave?", default=False):
            data["auto_leave"] = not data.get("auto_leave", False)
        if Confirm.ask("Change loop delay?", default=False):
            data["loop_delay_seconds"] = IntPrompt.ask("Seconds", default=data.get("loop_delay_seconds", 600))
        if Confirm.ask("Toggle enabled?", default=False):
            data["enabled"] = not data.get("enabled", True)
            console.print(f"  → enabled={data['enabled']}")

        self._save_campaign(name, data)
        console.print("[green]Saved.[/green]")

    async def _campaign_run(self, path: Path) -> None:
        data = self._load_campaign(path)
        name = path.stem

        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        if not data.get("dry_run", True):
            if not Confirm.ask(f"[red]LIVE run '{name}'?[/red]", default=False):
                return

        console.print(f"\n[bold cyan]Running '{name}'...[/bold cyan] [dim]Ctrl+C to stop[/dim]")
        await self._run_campaign_loop(data, name)

    async def _run_campaign_loop(self, data: dict, name: str) -> None:
        variants = data.get("message_variants", [])
        variator = MessageVariator(variants=variants) if variants else None
        loop_delay = data.get("loop_delay_seconds", 600)
        dry = data.get("dry_run", True)
        source_chat = data["source_chat_id"]
        source_msg_id = data["source_message_id"]
        target_ids = set(data.get("target_group_ids", []))

        if not self._reach_cache:
            await self._load_reach_cache()

        cycle = 0
        stop = False

        def _stop(signum=None, frame=None):
            nonlocal stop
            stop = True

        old = signal.signal(signal.SIGINT, _stop)

        try:
            while not stop:
                cycle += 1
                source_msg = None
                sent = 0
                failed = 0
                reach = 0

                for i, tid in enumerate(sorted(target_ids)):
                    if stop:
                        break
                    gw = self.gateways[i % len(self.gateways)]

                    if await self.store.already_sent(source_chat, source_msg_id, tid):
                        continue

                    if dry:
                        sent += 1
                        reach += self._reach_cache.get(tid, 0)
                        continue

                    await self._pace(gw)

                    if source_msg is None:
                        source_msg = await gw.get_message(source_chat, source_msg_id)
                        if source_msg is None:
                            console.print("[red]Source message gone.[/red]")
                            stop = True
                            break

                    text = variator.next() if variator else None
                    try:
                        await gw.copy_as_new(tid, source_msg, text_override=text)
                        await self.store.mark_sent(source_chat, source_msg_id, tid)
                        sent += 1
                        reach += self._reach_cache.get(tid, 0)
                    except Exception as e:
                        cls = e.__class__.__name__
                        if "Flood" in cls:
                            await asyncio.sleep(getattr(e, "seconds", 30))
                        failed += 1

                self._total_reach += reach
                console.print(
                    f"[{name}][{cycle}] sent={sent} failed={failed} reach={reach:,} total_reach={self._total_reach:,}"
                )

                if not stop:
                    await asyncio.sleep(loop_delay)
        finally:
            signal.signal(signal.SIGINT, old)

        console.print(f"[yellow]'{name}' stopped after {cycle} cycles.[/yellow]")

    async def _campaign_schedule(self, path: Path) -> None:
        data = self._load_campaign(path)
        name = path.stem

        console.print(f"\nSchedule for [cyan]{name}[/cyan]")
        current = data.get("schedule", "")
        console.print(f"  Current: {'[dim]none[/dim]' if not current else f'[green]{current}[/green]'}")

        console.print(
            "\n  [1] every N minutes\n"
            "  [2] every N hours\n"
            "  [3] daily at HH:MM\n"
            "  [4] disable schedule\n"
        )
        c = Prompt.ask(">", choices=["1", "2", "3", "4"], default="4")

        if c == "1":
            n = IntPrompt.ask("Every N minutes", default=60)
            data["schedule"] = f"every {n}m"
        elif c == "2":
            n = IntPrompt.ask("Every N hours", default=1)
            data["schedule"] = f"every {n}h"
        elif c == "3":
            t = Prompt.ask("At time (UTC HH:MM)", default="14:00")
            data["schedule"] = f"at {t}"
        else:
            data["schedule"] = ""

        self._save_campaign(name, data)
        console.print(f"[green]Schedule: {data['schedule'] or 'disabled'}[/green]")

    async def _cron_loop(self) -> None:
        while self._running:
            try:
                for f in self._list_campaign_files():
                    try:
                        data = self._load_campaign(f)
                    except Exception:
                        continue

                    schedule = data.get("schedule", "")
                    if not schedule or not data.get("enabled", True):
                        continue

                    if not self._should_fire(schedule, f):
                        continue

                    if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
                        await self._connect_accounts()
                    if not self.gateways:
                        continue

                    console.print(f"\n[cyan][cron] firing campaign '{f.stem}' ({schedule})[/cyan]")
                    await self._run_campaign_loop(data, f.stem)
                    f.touch()
            except Exception:
                pass

            await asyncio.sleep(30)

    def _should_fire(self, schedule: str, path: Path) -> bool:
        now = time.time()
        mtime = path.stat().st_mtime if path.exists() else 0

        if schedule.startswith("every "):
            rest = schedule[6:]
            if rest.endswith("m"):
                secs = int(rest[:-1]) * 60
            elif rest.endswith("h"):
                secs = int(rest[:-1]) * 3600
            else:
                return False
            return now - mtime >= secs

        elif schedule.startswith("at "):
            t_str = schedule[3:].strip()
            try:
                h, m = map(int, t_str.split(":"))
            except ValueError:
                return False

            now_dt = datetime.now(timezone.utc)
            target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if now_dt < target:
                return False
            last_run = datetime.fromtimestamp(mtime, tz=timezone.utc)
            return last_run < target <= now_dt

        return False

    # ══════════════════════════════════════════════════════════
    # Health check
    # ══════════════════════════════════════════════════════════

    async def _health_check(self) -> None:
        if not self.gateways or not any(g.client.is_connected() for g in self.gateways):
            await self._connect_accounts()
        if not self.gateways:
            return

        gw = self.gateways[0]
        records = await self.store.get_recent_sent(limit=100)
        if not records:
            console.print("[dim]No messages sent yet. Run a dispatch first.[/dim]")
            return

        me = await gw.get_me()
        await self._load_reach_cache()

        console.print(f"\n[cyan]Checking {len(records)} sent messages...[/cyan]\n")

        alive = 0
        deleted = 0
        reach_alive = 0
        total_reach_checked = 0
        reactions_total = 0
        rows = []

        from telethon import types as tt

        for rec in records:
            target_id = rec["target_chat_id"]
            try:
                latest = await gw.get_latest_message(target_id)
            except Exception:
                continue

            members = self._reach_cache.get(target_id, 0)
            total_reach_checked += members

            if latest and getattr(latest, "from_id", None):
                fid = latest.from_id
                if isinstance(fid, tt.PeerUser) and fid.user_id == me.id:
                    status = "✓ alive"
                    style = "green"
                    alive += 1
                    reach_alive += members

                    rcount = 0
                    rx = getattr(latest, "reactions", None)
                    if rx:
                        try:
                            rcount = getattr(rx, "count", 0) or 0
                        except Exception:
                            pass
                    reactions_total += rcount
                    react_str = f" ({rcount} reactions)" if rcount else ""
                    rows.append((target_id, f"[{style}]{status}[/{style}]{react_str}", f"{members:,}"))
                    continue

            status = "✗ deleted"
            style = "red"
            deleted += 1
            rows.append((target_id, f"[{style}]{status}[/{style}]", f"{members:,}"))

        if not rows:
            console.print("[dim]Could not check any messages (groups may be unreachable).[/dim]")
            return

        table = Table(title="Message Health")
        table.add_column("Group ID", style="cyan")
        table.add_column("Status", style="white")
        table.add_column("Members", style="magenta", justify="right")

        for row in rows:
            table.add_row(str(row[0]), row[1], row[2])

        console.print(table)
        console.print()
        console.print(
            f"[green]Alive: {alive}[/green]  [red]Deleted: {deleted}[/red]  "
            f"Reach alive: [cyan]{reach_alive:,}[/cyan] / {total_reach_checked:,}  "
            f"Reactions: {reactions_total}"
        )

    # ══════════════════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════════════════

    def _load_saved_state(self) -> None:
        cfg = Path("data/session_state.json")
        if not cfg.exists():
            return
        try:
            d = json.loads(cfg.read_text())
            self.accounts = [
                AccountConfig(
                    phone_number=a["phone"], api_id=a["api_id"], api_hash=a["api_hash"],
                    label=a.get("label", ""), session_path=a.get("session_path", ""),
                )
                for a in d.get("accounts", [])
            ]
            self.source_chat_id = d.get("source_chat_id")
            self.source_chat_title = d.get("source_chat_title", "")
            self.source_msg_id = d.get("source_msg_id")
            self.source_msg_text = d.get("source_msg_text", "")
            self.source_has_media = d.get("source_has_media", False)
            self.variants = d.get("variants", [])
            self.interval = d.get("interval", 8)
            self.per_hour = d.get("per_hour", 40)
            self._total_reach = d.get("total_reach", 0)
            self._proxy = _parse_proxy(d.get("proxy", ""))
            self._active_list = d.get("active_list", "default")
            self.target_file = self._group_lists_dir / f"{self._active_list}.txt"
            if self.target_file.exists():
                self.target_ids = load_whitelist_ids(self.target_file)
        except Exception:
            pass

    def _save_state(self) -> None:
        cfg = Path("data/session_state.json")
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "accounts": [
                {
                    "phone": a.phone_number, "api_id": a.api_id, "api_hash": a.api_hash,
                    "label": a.label, "session_path": a.session_path,
                }
                for a in self.accounts
            ],
            "source_chat_id": self.source_chat_id,
            "source_chat_title": self.source_chat_title,
            "source_msg_id": self.source_msg_id,
            "source_msg_text": self.source_msg_text,
            "source_has_media": self.source_has_media,
            "variants": self.variants,
            "interval": self.interval,
            "per_hour": self.per_hour,
            "total_reach": self._total_reach,
            "proxy": f"{self._proxy.scheme()}://{self._proxy.host}:{self._proxy.port}" if self._proxy else "",
            "active_list": self._active_list,
        }, indent=2))

    def _save_accounts_state(self) -> None:
        self._save_state()
