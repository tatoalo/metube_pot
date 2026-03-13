# MeTube w/POT support

![Build Status](https://github.com/tatoalo/metube_pot/actions/workflows/main.yml/badge.svg)
![Docker Pulls](https://img.shields.io/docker/pulls/tatoalo/metube_pot.svg)

A fork of [MeTube](https://github.com/alexta69/metube) with **YouTube PO Token (Proof-of-Origin)** support and **Jellyfin integration**.

## Features

### POT (Proof-of-Origin) Support
Bypasses YouTube's "Sign in to confirm you're not a bot" restrictions by integrating with [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).

### Jellyfin Integration
Automatically generates `.nfo` metadata files for downloaded videos, enabling Jellyfin to properly index and display Video title and description and other basic metadata.

### Telegram Bot
Optional Telegram bot support to queue downloads by sending links directly in chat.

Features:
- `/config` menu to set default download format/quality per chat
- One or multiple links per message
- Basic URL sanitization (only valid public `http/https` URLs)
- Completion feedback (`✅` on success, `❌` with error on failure)
- Stall and long-running download notifications

Environment variables:

```env
TELEGRAM_BOT_ENABLED=true
TELEGRAM_BOT_TOKEN=<your_bot_token>
TELEGRAM_ALLOWED_CHAT_IDS=123456789,-100987654321
TELEGRAM_STALL_TIMEOUT_SECONDS=180
TELEGRAM_HARD_TIMEOUT_SECONDS=7200
TELEGRAM_MAX_URLS_PER_MESSAGE=10
```

Notes:
- `TELEGRAM_ALLOWED_CHAT_IDS` is required when bot is enabled.
- Chat IDs can be private chats or groups/supergroups.
- Bot configuration is persisted in `STATE_DIR/telegram_bot_config.json`.

## Run using Docker Compose

```yaml
services:
  metube:
    image: ghcr.io/tatoalo/metube_pot
    container_name: metube
    [...]
```
