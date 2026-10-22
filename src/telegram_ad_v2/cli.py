from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table


from .config import get_settings
from .dispatcher import dispatch_once, load_whitelist_ids
from .logging_setup import configure_logging
from .scheduler import SendScheduler
from .storage import StateStore
from .telegram_client import TelegramGateway
from .group_joiner import GroupJoiner
from .account_manager import AccountManager
from .campaign_engine import CampaignEngine
from .message_variator import MessageVariator
from .interactive import Session

app = typer.Typer(help="Telegram Ad v2 - the best ad tool for Telegram")
console = Console()
logger = logging.getLogger(__name__)


def _gateway(settings=None):
    s = settings or get_settings()
    return TelegramGateway(
        api_id=s.api_id,
        api_hash=s.api_hash,
        phone_number=s.phone_number,
        session_dir=s.session_dir,
        proxy=None,
    )


# ── existing commands ──────────────────────────────────────────────


@app.command()
def check_config(verbose: bool = typer.Option(False, "--verbose")):
    configure_logging(verbose)
    settings = get_settings()
    console.print("Config loaded successfully")
    console.print(f"Session dir: {settings.session_dir}")
    console.print(f"SQLite path: {settings.sqlite_path}")
    if settings.account_json_env_is_valid():
        accounts = settings.get_accounts()
        console.print(f"Multi-account mode: {len(accounts)} accounts")
        for acc in accounts:
            proxy_str = f"{acc.proxy.host}:{acc.proxy.port}" if acc.proxy else "direct"
            console.print(f"  [{acc.label}] {acc.phone_number} via {proxy_str}")


@app.command()
def list_chats(verbose: bool = typer.Option(False, "--verbose")):
    configure_logging(verbose)

    async def _run():
        gateway = _gateway()
        await gateway.connect()
        try:
            chats = await gateway.list_chats()
            table = Table(title="Telegram chats")
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Type", style="magenta")
            for chat in chats:
                title = getattr(chat, "title", "<no-title>")
                table.add_row(str(chat.id), title, chat.__class__.__name__)
            console.print(table)
        finally:
            await gateway.disconnect()

    asyncio.run(_run())


@app.command()
def dispatch(
    source_chat_id: int = typer.Option(..., help="Chat ID containing the source message."),
    source_message_id: int = typer.Option(..., help="Message ID to forward."),
    whitelist_file: Path = typer.Option(
        Path("scripts/allowed_groups.txt"),
        help="File containing allowed target chat IDs (one per line).",
    ),
    dry_run: bool = typer.Option(True, help="Only simulate sends."),
    confirm_send: bool = typer.Option(False, help="Required when dry_run is false."),
    verbose: bool = typer.Option(False, "--verbose"),
):
    configure_logging(verbose)

    if not whitelist_file.exists():
        raise typer.BadParameter(f"Whitelist file not found: {whitelist_file}")
    if not dry_run and not confirm_send:
        raise typer.BadParameter("Use --confirm-send when dry_run is false.")

    async def _run():
        settings = get_settings()
        gateway = _gateway(settings)
        store = StateStore(settings.sqlite_path)
        await store.init()

        scheduler = SendScheduler(
            send_interval_seconds=settings.send_interval_seconds,
            jitter_min_seconds=settings.jitter_min_seconds,
            jitter_max_seconds=settings.jitter_max_seconds,
            max_messages_per_hour=settings.max_messages_per_hour,
            max_messages_per_day=settings.max_messages_per_day,
            active_hours_start=settings.active_hours_start,
            active_hours_end=settings.active_hours_end,
        )

        allowed = load_whitelist_ids(whitelist_file)
        await gateway.connect()
        try:
            stats = await dispatch_once(
                gateway=gateway,
                store=store,
                scheduler=scheduler,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                allowed_target_chat_ids=allowed,
                dry_run=dry_run,
            )
        finally:
            await gateway.disconnect()

        console.print(
            f"attempted={stats.attempted} sent={stats.sent} skipped={stats.skipped} failed={stats.failed}"
        )

    asyncio.run(_run())


@app.command("import-groups")
def import_groups(
    source_file: Path = typer.Option(
        Path("../assets/groups.txt"),
        help="Legacy list with Telegram links/usernames (one per line).",
    ),
    output_file: Path = typer.Option(
        Path("scripts/allowed_groups.txt"),
        help="Destination whitelist file with resolved chat IDs.",
    ),
    verbose: bool = typer.Option(False, "--verbose"),
):
    configure_logging(verbose)

    if not source_file.exists():
        raise typer.BadParameter(f"Source file not found: {source_file}")

    async def _run():
        gateway = _gateway()
        await gateway.connect()
        resolved_ids: set[int] = set()
        failed: list[str] = []

        try:
            for line in source_file.read_text(encoding="utf-8").splitlines():
                candidate = line.strip()
                if not candidate or candidate.startswith("#"):
                    continue
                try:
                    chat_id = await gateway.resolve_chat_id(candidate)
                    resolved_ids.add(chat_id)
                except Exception as exc:
                    logger.warning("failed to resolve '%s': %s", candidate, exc)
                    failed.append(candidate)
        finally:
            await gateway.disconnect()

        output_file.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(str(chat_id) for chat_id in sorted(resolved_ids))
        output_file.write_text(f"{body}\n" if body else "", encoding="utf-8")
        console.print(f"Resolved IDs: {len(resolved_ids)}")
        console.print(f"Failed entries: {len(failed)}")
        console.print(f"Written: {output_file}")

    asyncio.run(_run())


# ── group joining ───────────────────────────────────────────────────


@app.command("join-groups")
def join_groups(
    groups_file: Path = typer.Option(
        Path("../assets/groups.txt"),
        help="File with Telegram invite links/usernames (one per line).",
    ),
    max_joins: int = typer.Option(50, help="Max groups to join in this session."),
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Join groups from a file of Telegram invite links."""
    configure_logging(verbose)
    settings = get_settings()

    async def _run():
        gateway = _gateway(settings)
        store = StateStore(settings.sqlite_path)
        await store.init()
        await gateway.connect()

        try:
            joiner = GroupJoiner(gateway, max_joins_per_session=max_joins)
            stats = await joiner.join_from_file(groups_file)
        finally:
            await gateway.disconnect()

        console.print(
            f"attempted={stats.attempted} joined={stats.joined} already_member={stats.already_member} failed={stats.failed}"
        )

    asyncio.run(_run())


@app.command("join-from-file")
def join_from_file(
    groups_file: Path = typer.Option(
        Path("../assets/groups.txt"),
        help="File with Telegram invite links/usernames (one per line).",
    ),
    account_index: int = typer.Option(0, help="Which account to use (0=first)."),
    max_joins: int = typer.Option(50, help="Max groups to join in this session."),
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Join groups using a specific account from the multi-account config."""
    configure_logging(verbose)
    settings = get_settings()
    accounts = settings.get_accounts()

    if account_index >= len(accounts):
        console.print(f"[red]Account index {account_index} out of range (0-{len(accounts)-1})[/red]")
        raise typer.Exit(1)

    acc = accounts[account_index]

    async def _run():
        gateway = TelegramGateway(
            api_id=acc.api_id,
            api_hash=acc.api_hash,
            phone_number=acc.phone_number,
            session_dir=settings.session_dir,
            proxy=acc.proxy,
            session_path=acc.session_path,
        )
        store = StateStore(settings.sqlite_path)
        await store.init()
        await gateway.connect()

        try:
            joiner = GroupJoiner(gateway, max_joins_per_session=max_joins)
            stats = await joiner.join_from_file(groups_file)
        finally:
            await gateway.disconnect()

        console.print(
            f"[{acc.label}] attempted={stats.attempted} joined={stats.joined} already_member={stats.already_member} failed={stats.failed}"
        )

    asyncio.run(_run())


@app.command("leave-groups")
def leave_groups(
    groups_file: Path = typer.Option(
        Path("scripts/allowed_groups.txt"),
        help="File with chat IDs to leave (one per line).",
    ),
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Leave groups listed in a file (one chat ID per line)."""
    configure_logging(verbose)
    settings = get_settings()

    async def _run():
        gateway = _gateway(settings)
        await gateway.connect()
        try:
            ids = load_whitelist_ids(groups_file)
            joiner = GroupJoiner(gateway)
            left = await joiner.leave_groups(list(ids))
            console.print(f"Left {left}/{len(ids)} groups")
        finally:
            await gateway.disconnect()

    asyncio.run(_run())


@app.command("leave-all")
def leave_all(verbose: bool = typer.Option(False, "--verbose")):
    """Leave ALL megagroups."""
    configure_logging(verbose)
    settings = get_settings()

    async def _run():
        gateway = _gateway(settings)
        await gateway.connect()
        try:
            joiner = GroupJoiner(gateway)
            left = await joiner.leave_all_megagroups()
            console.print(f"Left {left} groups")
        finally:
            await gateway.disconnect()

    asyncio.run(_run())


# ── campaigns ───────────────────────────────────────────────────────


@app.command("campaign-create")
def campaign_create(
    name: str = typer.Option(..., help="Campaign name."),
    source_chat_id: int = typer.Option(..., help="Source chat ID with the ad message."),
    source_message_id: int = typer.Option(..., help="Message ID to forward."),
    target_groups_file: Path = typer.Option(
        Path("scripts/allowed_groups.txt"),
        help="File with target chat IDs (one per line).",
    ),
    account_labels: str = typer.Option("", help="Comma-separated account labels (default=first account)."),
    dry_run: bool = typer.Option(True, help="Dry run mode."),
    auto_leave: bool = typer.Option(False, help="Auto-leave groups after sending."),
    leave_delay: int = typer.Option(300, help="Seconds to wait before leaving after send."),
    loop_delay: int = typer.Option(300, help="Seconds between campaign cycles."),
    message_variants: str = typer.Option("", help="Comma-separated message variants for spinning."),
):
    """Create a campaign definition file."""
    settings = get_settings()
    settings.campaigns_dir.mkdir(parents=True, exist_ok=True)

    ids = set()
    if target_groups_file.exists():
        ids = load_whitelist_ids(target_groups_file)

    variants = [v.strip() for v in message_variants.split(",") if v.strip()] if message_variants else []
    labels = [a.strip() for a in account_labels.split(",") if a.strip()] if account_labels else []

    campaign_data = {
        "source_chat_id": source_chat_id,
        "source_message_id": source_message_id,
        "target_group_ids": sorted(ids),
        "account_labels": labels,
        "enabled": True,
        "dry_run": dry_run,
        "auto_leave": auto_leave,
        "leave_delay_seconds": leave_delay,
        "loop_delay_seconds": loop_delay,
        "message_variants": variants,
    }

    campaign_file = settings.campaigns_dir / f"{name}.json"
    campaign_file.write_text(json.dumps(campaign_data, indent=2), encoding="utf-8")

    console.print(f"[green]Campaign '{name}' created at {campaign_file}[/green]")
    console.print(f"  Targets: {len(ids)} groups")
    console.print(f"  Dry run: {dry_run}")
    console.print(f"  Auto-leave: {auto_leave}")
    if variants:
        console.print(f"  Message variants: {len(variants)}")


@app.command("campaign-list")
def campaign_list():
    """List all campaign definitions."""
    settings = get_settings()
    campaigns = settings.load_campaigns()

    if not campaigns:
        console.print("[yellow]No campaigns found.[/yellow]")
        return

    table = Table(title="Campaigns")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Targets", style="magenta")
    table.add_column("Dry Run", style="yellow")
    table.add_column("Auto Leave", style="red")
    table.add_column("Variants", style="blue")
    table.add_column("Enabled", style="white")

    for c in campaigns:
        table.add_row(
            c.name,
            f"{c.source_chat_id}:{c.source_message_id}",
            str(len(c.target_group_ids)),
            str(c.dry_run),
            str(c.auto_leave),
            str(len(c.message_variants)),
            str(c.enabled),
        )

    console.print(table)


@app.command("campaign-run")
def campaign_run(
    name: str = typer.Option(..., help="Campaign name to run."),
    gateway_label: str = typer.Option("", help="Account label to use (empty=first connected)."),
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Run a campaign in a continuous loop. Ctrl+C to stop."""
    configure_logging(verbose)
    settings = get_settings()
    campaigns = settings.load_campaigns()

    campaign = None
    for c in campaigns:
        if c.name == name:
            campaign = c
            break

    if campaign is None:
        console.print(f"[red]Campaign '{name}' not found.[/red]")
        raise typer.Exit(1)

    if not campaign.enabled:
        console.print(f"[yellow]Campaign '{name}' is disabled.[/yellow]")
        raise typer.Exit(1)

    async def _run():
        mgr = AccountManager()
        accounts = settings.get_accounts()

        if gateway_label:
            accounts = [a for a in accounts if a.label == gateway_label]
            if not accounts:
                console.print(f"[red]No account with label '{gateway_label}'[/red]")
                return

        store = StateStore(settings.sqlite_path)
        await store.init()

        connected = await mgr.connect_all(accounts)
        if not connected:
            console.print("[red]Failed to connect any account[/red]")
            return

        label = next(iter(connected.keys()))

        engine = CampaignEngine(mgr, store, settings)

        task = engine.start_campaign(campaign, label)

        def _on_signal(signum, frame):
            console.print("\n[yellow]Stopping campaign...[/yellow]")
            engine.stop_campaign(campaign.name)

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        if task:
            await task

        await mgr.disconnect_all()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command("campaign-stop")
def campaign_stop(name: str = typer.Option(..., help="Campaign name to stop.")):
    """Mark a campaign as stopped (for programmatic control)."""
    settings = get_settings()
    campaign_file = settings.campaigns_dir / f"{name}.json"
    if not campaign_file.exists():
        console.print(f"[red]Campaign '{name}' not found.[/red]")
        raise typer.Exit(1)

    data = json.loads(campaign_file.read_text(encoding="utf-8"))
    data["enabled"] = False
    campaign_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    console.print(f"[yellow]Campaign '{name}' disabled.[/yellow]")


# ── accounts ────────────────────────────────────────────────────────


@app.command("accounts-list")
def accounts_list(verbose: bool = typer.Option(False, "--verbose")):
    """List configured accounts."""
    configure_logging(verbose)
    settings = get_settings()
    accounts = settings.get_accounts()

    table = Table(title="Accounts")
    table.add_column("#", style="cyan")
    table.add_column("Label", style="green")
    table.add_column("Phone", style="magenta")
    table.add_column("Proxy", style="yellow")

    for i, acc in enumerate(accounts):
        proxy_str = "direct" if not acc.proxy else f"{acc.proxy.scheme()}://{acc.proxy.host}:{acc.proxy.port}"
        table.add_row(str(i), acc.label, acc.phone_number, proxy_str)

    console.print(table)


# ── interactive session ─────────────────────────────────────────────


@app.command("interactive")
def interactive(
    verbose: bool = typer.Option(False, "--verbose"),
):
    """Start an interactive session (REPL) with menus for everything."""
    configure_logging(verbose)

    async def _run():
        session = Session()
        await session.start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


# ── reset ───────────────────────────────────────────────────────────


@app.command("variate")
def variate(
    template: str = typer.Option(..., help="Message template with {option1|option2} spinning."),
    count: int = typer.Option(5, help="Number of variants to generate."),
    noise: bool = typer.Option(True, help="Add zero-width noise characters."),
    unicode_vary: bool = typer.Option(True, help="Vary unicode homoglyphs."),
):
    """Test message variation/spinning."""
    variator = MessageVariator(
        variants=[template],
        enable_noise=noise,
        enable_unicode=unicode_vary,
    )

    console.print(f"[bold]Template:[/bold] {template}")
    console.print()
    for i in range(count):
        variant = variator.next()
        console.print(f"  [{i+1}] {variant!r}")
        console.print(f"       {variant}")


@app.command("reset")
def reset(
    confirm: bool = typer.Option(False, "--confirm", help="Required to actually reset."),
):
    """Clear all sent-message state and campaign tracking."""
    if not confirm:
        console.print("[yellow]Use --confirm to actually reset all state.[/yellow]")
        raise typer.Exit(1)

    settings = get_settings()

    async def _run():
        store = StateStore(settings.sqlite_path)
        await store.init()
        await store.clear_all()
        console.print("[green]All state cleared.[/green]")

    asyncio.run(_run())
