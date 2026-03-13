import asyncio
import contextvars
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ytdl import DownloadQueue, DownloadInfo

log = logging.getLogger("telegram_bot")


URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
TRAILING_PUNCTUATION = ".,;:!?)]}>'\""


@dataclass
class WatchedDownload:
    chats: set[int] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)
    stall_notified: set[int] = field(default_factory=set)
    timeout_notified: set[int] = field(default_factory=set)


class TelegramBot:
    def __init__(
        self,
        dqueue: DownloadQueue,
        formats: list[dict[str, Any]],
        state_dir: str,
        enabled: bool,
        default_playlist_item_limit: int,
        default_chapter_template: str,
        stall_timeout_seconds: int,
        hard_timeout_seconds: int,
        max_urls_per_message: int,
    ):
        self.dqueue = dqueue
        self.formats = formats
        self.state_dir = Path(state_dir)
        self.enabled = enabled
        self.default_playlist_item_limit = default_playlist_item_limit
        self.default_chapter_template = default_chapter_template
        self.stall_timeout_seconds = stall_timeout_seconds
        self.hard_timeout_seconds = hard_timeout_seconds
        self.max_urls_per_message = max_urls_per_message
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.allowed_chat_ids = self._parse_allowed_chat_ids(
            os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        )
        self.config_path = self.state_dir / "telegram_bot_config.json"

        self._app: Application | None = None
        self._monitor_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._chat_config: dict[str, dict[str, Any]] = {}
        self._watched_downloads: dict[str, WatchedDownload] = {}
        self._current_chat_id: contextvars.ContextVar[int | None] = (
            contextvars.ContextVar("telegram_current_chat_id", default=None)
        )

    async def start(self):
        if not self.enabled:
            log.info("Telegram bot disabled")
            return
        if not self.bot_token:
            log.error("Telegram bot enabled but TELEGRAM_BOT_TOKEN is missing")
            return
        if not self.allowed_chat_ids:
            log.error("Telegram bot enabled but TELEGRAM_ALLOWED_CHAT_IDS is empty")
            return

        self._load_config()

        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CommandHandler("start", self._start_command))
        self._app.add_handler(CommandHandler("config", self._config_command))
        self._app.add_handler(
            CallbackQueryHandler(self._config_callback, pattern=r"^cfg:")
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._text_message_handler)
        )

        await self._app.initialize()
        await self._app.start()
        if self._app.updater is None:
            log.error("Telegram updater is unavailable")
            return
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._monitor_task = asyncio.create_task(self._monitor_downloads())
        log.info("Telegram bot started")

    async def stop(self):
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        if self._app is None:
            return

        if self._app.updater is not None:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None
        log.info("Telegram bot stopped")

    async def on_added(self, dl: DownloadInfo):
        chat_id = self._current_chat_id.get()
        if chat_id is None:
            return
        async with self._lock:
            watched = self._watched_downloads.get(dl.url)
            if watched is None:
                watched = WatchedDownload()
                self._watched_downloads[dl.url] = watched
            watched.chats.add(chat_id)
            watched.started_at = time.monotonic()
            watched.last_progress_at = watched.started_at

    async def on_updated(self, dl: DownloadInfo):
        async with self._lock:
            watched = self._watched_downloads.get(dl.url)
            if watched is None:
                return
            if dl.status in ("downloading", "preparing"):
                watched.last_progress_at = time.monotonic()

    async def on_completed(self, dl: DownloadInfo):
        async with self._lock:
            watched = self._watched_downloads.pop(dl.url, None)

        if watched is None:
            return

        if dl.status == "finished":
            filename = getattr(dl, "filename", None)
            suffix = f"\nFile: {filename}" if filename else ""
            text = f"✅ Download complete: {dl.title}{suffix}"
        else:
            error_msg = dl.msg or dl.error or "Download failed"
            text = f"❌ Download failed: {dl.title}\n{error_msg}"

        for chat_id in watched.chats:
            await self._send_message(chat_id, text)

    async def on_canceled(self, url: str):
        async with self._lock:
            self._watched_downloads.pop(url, None)

    def _parse_allowed_chat_ids(self, raw_ids: str) -> set[int]:
        result = set()
        for value in raw_ids.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                result.add(int(value))
            except ValueError:
                log.warning(
                    f"Ignoring invalid TELEGRAM_ALLOWED_CHAT_IDS entry: {value}"
                )
        return result

    def _get_chat_config(self, chat_id: int) -> dict[str, Any]:
        key = str(chat_id)
        config = self._chat_config.get(key)
        if config is not None:
            return config

        defaults = {
            "format": "mp4",
            "quality": "best",
            "folder": "",
            "custom_name_prefix": "",
            "playlist_item_limit": self.default_playlist_item_limit,
            "auto_start": True,
            "split_by_chapters": False,
            "chapter_template": self.default_chapter_template,
        }
        self._chat_config[key] = defaults
        self._save_config()
        return defaults

    def _load_config(self):
        if not self.config_path.exists():
            self._chat_config = {}
            return
        try:
            self._chat_config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(self._chat_config, dict):
                raise ValueError("Top-level config is not an object")
        except Exception as exc:
            log.error(f"Failed to read Telegram bot config: {exc}")
            self._chat_config = {}

    def _save_config(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._chat_config, indent=2, sort_keys=True), encoding="utf-8"
        )
        temp_path.replace(self.config_path)

    def _format_config_text(self, chat_id: int) -> str:
        config = self._get_chat_config(chat_id)
        return (
            "Current download config:\n"
            f"- Format: {config['format']}\n"
            f"- Quality: {config['quality']}\n"
            f"- Split by chapters: {'on' if config['split_by_chapters'] else 'off'}\n"
            f"- Playlist item limit: {config['playlist_item_limit']}"
        )

    def _get_format_qualities(self, format_id: str) -> list[str]:
        for item in self.formats:
            if item.get("id") == format_id:
                return [
                    quality["id"]
                    for quality in item.get("qualities", [])
                    if "id" in quality
                ]
        return ["best"]

    def _build_main_config_keyboard(self, chat_id: int) -> InlineKeyboardMarkup:
        config = self._get_chat_config(chat_id)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Format: {config['format']}", callback_data="cfg:menu:format"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Quality: {config['quality']}",
                        callback_data="cfg:menu:quality",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Split Chapters: {'on' if config['split_by_chapters'] else 'off'}",
                        callback_data="cfg:toggle:split",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Playlist Limit: {config['playlist_item_limit']}",
                        callback_data="cfg:menu:limit",
                    )
                ],
            ]
        )

    def _build_format_keyboard(self) -> InlineKeyboardMarkup:
        rows = []
        for item in self.formats:
            fmt = item.get("id")
            if not fmt:
                continue
            rows.append(
                [InlineKeyboardButton(fmt, callback_data=f"cfg:set:format:{fmt}")]
            )
        rows.append([InlineKeyboardButton("Back", callback_data="cfg:menu:main")])
        return InlineKeyboardMarkup(rows)

    def _build_quality_keyboard(self, format_id: str) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(quality, callback_data=f"cfg:set:quality:{quality}")]
            for quality in self._get_format_qualities(format_id)
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="cfg:menu:main")])
        return InlineKeyboardMarkup(rows)

    def _build_playlist_limit_keyboard(self) -> InlineKeyboardMarkup:
        values = [0, 1, 5, 10, 20]
        rows = [
            [InlineKeyboardButton(str(value), callback_data=f"cfg:set:limit:{value}")]
            for value in values
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="cfg:menu:main")])
        return InlineKeyboardMarkup(rows)

    def _extract_urls(self, text: str) -> list[str]:
        urls = []
        seen = set()
        for match in URL_RE.findall(text):
            normalized = match.rstrip(TRAILING_PUNCTUATION)
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
        return urls

    def _validate_url(self, raw_url: str) -> tuple[bool, str]:
        try:
            parsed = urlsplit(raw_url)
        except Exception:
            return False, "invalid URL format"

        if parsed.scheme not in ("http", "https"):
            return False, "only http/https URLs are allowed"
        if not parsed.netloc:
            return False, "URL host is missing"

        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False, "URL host is empty"
        if host in {"localhost"} or host.endswith(".local"):
            return False, "local network hosts are not allowed"

        try:
            ip = ipaddress.ip_address(host)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False, "private/local IP targets are not allowed"
        except ValueError:
            pass

        return True, ""

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = self._get_authorized_chat_id(update)
        if chat_id is None:
            return
        text = (
            "Hi! Send one or more links and I will queue them for download.\n"
            "Use /config to set default format/quality for this chat."
        )
        await self._send_message(chat_id, text)

    async def _config_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = self._get_authorized_chat_id(update)
        if chat_id is None:
            return
        text = self._format_config_text(chat_id)
        await self._send_message(
            chat_id,
            text,
            reply_markup=self._build_main_config_keyboard(chat_id),
        )

    async def _config_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        if query is None or query.data is None or query.message is None:
            return
        chat_id = self._get_authorized_chat_id(update)
        if chat_id is None:
            return

        parts = query.data.split(":")
        await query.answer()
        if len(parts) < 3:
            return

        action = parts[1]
        target = parts[2]
        config = self._get_chat_config(chat_id)

        if action == "menu":
            if target == "main":
                await query.edit_message_text(
                    self._format_config_text(chat_id),
                    reply_markup=self._build_main_config_keyboard(chat_id),
                )
                return
            if target == "format":
                await query.edit_message_text(
                    "Select format", reply_markup=self._build_format_keyboard()
                )
                return
            if target == "quality":
                await query.edit_message_text(
                    "Select quality",
                    reply_markup=self._build_quality_keyboard(config["format"]),
                )
                return
            if target == "limit":
                await query.edit_message_text(
                    "Select playlist limit",
                    reply_markup=self._build_playlist_limit_keyboard(),
                )
                return

        if action == "toggle" and target == "split":
            config["split_by_chapters"] = not bool(config.get("split_by_chapters"))
            self._save_config()
            await query.edit_message_text(
                self._format_config_text(chat_id),
                reply_markup=self._build_main_config_keyboard(chat_id),
            )
            return

        if action == "set" and len(parts) >= 4:
            value = parts[3]
            if target == "format":
                config["format"] = value
                available = self._get_format_qualities(value)
                if config["quality"] not in available:
                    config["quality"] = available[0]
            elif target == "quality":
                if value in self._get_format_qualities(config["format"]):
                    config["quality"] = value
            elif target == "limit":
                try:
                    config["playlist_item_limit"] = int(value)
                except ValueError:
                    pass
            self._save_config()
            await query.edit_message_text(
                self._format_config_text(chat_id),
                reply_markup=self._build_main_config_keyboard(chat_id),
            )

    async def _text_message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        chat_id = self._get_authorized_chat_id(update)
        if chat_id is None:
            return

        text = update.effective_message.text if update.effective_message else ""
        urls = self._extract_urls(text or "")
        if not urls:
            return

        if len(urls) > self.max_urls_per_message:
            await self._send_message(
                chat_id,
                f"Too many links in one message ({len(urls)}). Maximum allowed: {self.max_urls_per_message}.",
            )
            urls = urls[: self.max_urls_per_message]

        valid_urls = []
        rejected = []
        for url in urls:
            is_valid, reason = self._validate_url(url)
            if is_valid:
                valid_urls.append(url)
            else:
                rejected.append((url, reason))

        if rejected:
            rejected_text = "\n".join(f"- {url} ({reason})" for url, reason in rejected)
            await self._send_message(
                chat_id, f"Ignored invalid links:\n{rejected_text}"
            )

        if not valid_urls:
            return

        config = self._get_chat_config(chat_id)
        queued_count = 0
        errors = []
        for url in valid_urls:
            token = self._current_chat_id.set(chat_id)
            try:
                status = await self.dqueue.add(
                    url,
                    config["quality"],
                    config["format"],
                    config["folder"],
                    config["custom_name_prefix"],
                    config["playlist_item_limit"],
                    config["auto_start"],
                    config["split_by_chapters"],
                    config["chapter_template"],
                )
            finally:
                self._current_chat_id.reset(token)

            if status.get("status") == "error":
                errors.append(f"- {url}: {status.get('msg', 'unknown error')}")
            else:
                queued_count += 1

        if queued_count > 0:
            await self._send_message(
                chat_id, f"Queued {queued_count} link(s) with current chat config."
            )
        if errors:
            error_text = "\n".join(errors)
            await self._send_message(chat_id, f"Some links failed:\n{error_text}")

    def _get_authorized_chat_id(self, update: Update) -> int | None:
        chat = update.effective_chat
        if chat is None:
            return None
        if chat.id not in self.allowed_chat_ids:
            log.warning(f"Rejected Telegram update from unauthorized chat {chat.id}")
            return None
        return chat.id

    async def _monitor_downloads(self):
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            messages: list[tuple[int, str]] = []
            async with self._lock:
                for url, watched in self._watched_downloads.items():
                    since_progress = now - watched.last_progress_at
                    elapsed = now - watched.started_at
                    for chat_id in watched.chats:
                        if (
                            since_progress > self.stall_timeout_seconds
                            and chat_id not in watched.stall_notified
                        ):
                            watched.stall_notified.add(chat_id)
                            messages.append(
                                (
                                    chat_id,
                                    f"⚠️ Download seems stalled for {int(since_progress)}s:\n{url}",
                                )
                            )

                        if (
                            elapsed > self.hard_timeout_seconds
                            and chat_id not in watched.timeout_notified
                        ):
                            watched.timeout_notified.add(chat_id)
                            messages.append(
                                (
                                    chat_id,
                                    f"⏱️ Download is taking longer than expected ({int(elapsed)}s):\n{url}",
                                )
                            )

            for chat_id, text in messages:
                await self._send_message(chat_id, text)

    async def _send_message(
        self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ):
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup
            )
        except TelegramError as exc:
            log.error(f"Failed to send Telegram message to {chat_id}: {exc}")
