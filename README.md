# Telegram Campaign Toolkit

Telethon campaign management, scheduling, CLI and dashboard tooling.

Solo-developed by [Vojislav Beric](https://github.com/Goodworkid0).

## What it does

- Campaign definitions, scheduling, per-account configuration and persistent SQLite state.
- Typer/Rich command-line tools and a Streamlit dashboard.
- Dry-run execution and confirmation controls for sending.

## Stack

Python · Telethon · Typer · Rich · SQLite · Streamlit

## Getting started

Use Python 3.11 or newer. Supply credentials for your own Telegram account and destinations where you have permission to publish.

```sh
cp .env.example .env
# Fill in your Telegram API configuration.
./tg setup
./tg --help
./tg check-config
```

Use dry-run execution when checking a campaign. The CLI entry point is `telegram-ad-v2`; the source package is `src/telegram_ad_v2/`. Session files, recipient lists and campaign state are local runtime data and are excluded.

## Project status

Initial public source snapshot. Python syntax and source manifests were checked for publication; full application, infrastructure and external-service integration tests have not been run for this release. Local credentials, databases, sessions, uploaded content and machine-specific deployment files are excluded.

## Attribution

This repository contains the local v2 campaign toolkit. An earlier project used [dropout1337/Telegram-AdBot](https://github.com/dropout1337/Telegram-AdBot); that archived upstream implementation is not included here.

## License

Original project code is available under the [MIT License](LICENSE). External dependencies retain their respective licences.
