from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot import TelegramBot


def _make_bot(tmp_path, monkeypatch, dqueue):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123")
    return TelegramBot(
        dqueue=dqueue,
        formats=[{"id": "mp4", "qualities": [{"id": "best"}, {"id": "best_remux"}]}],
        state_dir=str(tmp_path),
        enabled=True,
        default_playlist_item_limit=0,
        default_chapter_template="%(title)s - %(section_number)02d - %(section_title)s.%(ext)s",
        stall_timeout_seconds=180,
        hard_timeout_seconds=7200,
        max_urls_per_message=10,
    )


def _make_update(text: str, chat_id: int = 123):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=SimpleNamespace(text=text),
    )


@pytest.mark.asyncio
async def test_telegram_single_video_queues_download(tmp_path, monkeypatch):
    dqueue = SimpleNamespace(add=AsyncMock(return_value={"status": "ok"}))
    bot = _make_bot(tmp_path, monkeypatch, dqueue)
    bot._send_message = AsyncMock()

    await bot._text_message_handler(_make_update("https://youtu.be/Mb6H7trzMfI"), None)

    dqueue.add.assert_awaited_once_with(
        "https://youtu.be/Mb6H7trzMfI",
        "video",
        "auto",
        "mp4",
        "best",
        "",
        "",
        0,
        True,
        False,
        "%(title)s - %(section_number)02d - %(section_title)s.%(ext)s",
        "en",
        "prefer_manual",
        [],
        {},
    )
    bot._send_message.assert_awaited_once_with(123, "Queued 1 link(s) with current chat config.")


@pytest.mark.asyncio
async def test_telegram_batch_video_message_queues_each_url(tmp_path, monkeypatch):
    dqueue = SimpleNamespace(add=AsyncMock(return_value={"status": "ok"}))
    bot = _make_bot(tmp_path, monkeypatch, dqueue)
    bot._send_message = AsyncMock()
    text = (
        "Queue these:\n"
        "https://youtu.be/Mb6H7trzMfI\n"
        "https://streamingcommunityz.ooo/it/watch/6119?e=39896"
    )

    await bot._text_message_handler(_make_update(text), None)

    assert dqueue.add.await_count == 2
    queued_urls = [call.args[0] for call in dqueue.add.await_args_list]
    assert queued_urls == [
        "https://youtu.be/Mb6H7trzMfI",
        "https://streamingcommunityz.ooo/it/watch/6119?e=39896",
    ]
    bot._send_message.assert_awaited_once_with(123, "Queued 2 link(s) with current chat config.")
