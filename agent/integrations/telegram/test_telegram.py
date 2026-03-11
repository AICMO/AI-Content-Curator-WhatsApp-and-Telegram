"""Tests for telegram.py."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from telegram import _split_message, TG_LINK_RE, TELEGRAM_MSG_LIMIT


# ============================================================
# _split_message
# ============================================================

class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert _split_message("hello") == ["hello"]

    def test_exact_limit(self):
        msg = "x" * TELEGRAM_MSG_LIMIT
        assert _split_message(msg) == [msg]

    def test_splits_on_newline(self):
        line = "a" * 2000
        msg = f"{line}\n{line}\n{line}"
        parts = _split_message(msg)
        assert len(parts) == 2
        assert all(len(p) <= TELEGRAM_MSG_LIMIT for p in parts)

    def test_splits_long_line_at_limit(self):
        msg = "a" * (TELEGRAM_MSG_LIMIT + 100)
        parts = _split_message(msg)
        assert len(parts) == 2
        assert parts[0] == "a" * TELEGRAM_MSG_LIMIT
        assert parts[1] == "a" * 100

    def test_empty_message(self):
        assert _split_message("") == [""]


# ============================================================
# TG_LINK_RE
# ============================================================

class TestTgLinkRegex:
    def test_matches_standard_link(self):
        text = "Check this https://t.me/some_channel/123 out"
        matches = TG_LINK_RE.findall(text)
        assert matches == [("some_channel", "123")]

    def test_multiple_links(self):
        text = "https://t.me/ch1/1 and https://t.me/ch2/2"
        matches = TG_LINK_RE.findall(text)
        assert matches == [("ch1", "1"), ("ch2", "2")]

    def test_no_links(self):
        text = "no links here"
        assert TG_LINK_RE.findall(text) == []

    def test_ignores_non_tme_links(self):
        text = "https://example.com/channel/123"
        assert TG_LINK_RE.findall(text) == []

    def test_link_with_underscores(self):
        text = "https://t.me/my_cool_channel/456"
        matches = TG_LINK_RE.findall(text)
        assert matches == [("my_cool_channel", "456")]


# ============================================================
# get_telegram_client
# ============================================================

class TestGetTelegramClient:
    def test_missing_all_vars(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit):
                from telegram import get_telegram_client
                get_telegram_client()

    def test_missing_some_vars(self):
        with patch.dict("os.environ", {"TELEGRAM_API_ID": "123"}, clear=True):
            with pytest.raises(SystemExit):
                from telegram import get_telegram_client
                get_telegram_client()

    def test_success(self):
        env = {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "abc", "TELEGRAM_SESSION_STRING": "session_data"}
        with patch.dict("os.environ", env, clear=True):
            with patch("telethon.TelegramClient") as mock_cls, \
                 patch("telegram.StringSession") as mock_ss:
                from telegram import get_telegram_client
                mock_cls.return_value = MagicMock()
                client = get_telegram_client()
                assert client is not None
                mock_ss.assert_called_once_with("session_data")


# ============================================================
# _resolve_links
# ============================================================

class TestResolveLinks:
    def test_no_links(self):
        from telegram import _resolve_links
        client = AsyncMock()
        collected = [{"text": "no links here"}]
        asyncio.run(_resolve_links(client, collected))
        assert "resolved_links" not in collected[0]

    def test_resolves_link(self):
        from telegram import _resolve_links
        client = AsyncMock()
        entity = MagicMock()
        client.get_entity.return_value = entity
        original_msg = MagicMock()
        original_msg.text = "Original content"
        original_msg.raw_text = "Original content"
        client.get_messages.return_value = original_msg

        collected = [{"text": "Check https://t.me/test_channel/42 out"}]
        asyncio.run(_resolve_links(client, collected))

        assert len(collected[0]["resolved_links"]) == 1
        assert collected[0]["resolved_links"][0]["channel"] == "test_channel"
        assert collected[0]["resolved_links"][0]["text"] == "Original content"

    def test_handles_error(self):
        from telegram import _resolve_links
        client = AsyncMock()
        client.get_entity.side_effect = Exception("not found")

        collected = [{"text": "Check https://t.me/private_ch/1 out"}]
        asyncio.run(_resolve_links(client, collected))
        assert "resolved_links" not in collected[0]

    def test_handles_flood_wait(self):
        from telethon import errors as tl_errors
        from telegram import _resolve_links
        client = AsyncMock()
        flood_err = tl_errors.FloodWaitError(request=None, capture=0)
        flood_err.seconds = 0
        client.get_entity.side_effect = flood_err

        collected = [{"text": "https://t.me/ch/1"}]
        asyncio.run(_resolve_links(client, collected))
        assert "resolved_links" not in collected[0]

    def test_skips_empty_message(self):
        from telegram import _resolve_links
        client = AsyncMock()
        client.get_entity.return_value = MagicMock()
        original_msg = MagicMock()
        original_msg.text = None
        original_msg.raw_text = None
        client.get_messages.return_value = original_msg

        collected = [{"text": "https://t.me/ch/1"}]
        asyncio.run(_resolve_links(client, collected))
        assert "resolved_links" not in collected[0]


# ============================================================
# cmd_read
# ============================================================

class TestCmdRead:
    def _make_message(self, text, msg_id=1, date=None):
        msg = MagicMock()
        msg.text = text
        msg.raw_text = text
        msg.id = msg_id
        msg.date = date or datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        return msg

    def _make_channel_entity(self, username="test_ch", title="Test Channel"):
        entity = MagicMock()
        entity.username = username
        entity.broadcast = True
        entity.title = title
        return entity

    @patch("telegram.get_telegram_client")
    def test_unauthorized(self, mock_get_client):
        from telegram import cmd_read
        client = AsyncMock()
        client.is_user_authorized.return_value = False
        mock_get_client.return_value = client
        asyncio.run(cmd_read())
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_channel_resolve_error(self, mock_get_client):
        from telegram import cmd_read
        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me
        client.get_entity.side_effect = Exception("not found")
        mock_get_client.return_value = client

        asyncio.run(cmd_read(channel="@nonexistent"))
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_read_specific_channel(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        msg = self._make_message("This is a long enough test message for filtering", msg_id=1)
        client.iter_messages = MagicMock(return_value=AsyncIterator([msg]))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert len(data) == 1
        assert data[0]["channel_username"] == "test_ch"

    @patch("telegram.get_telegram_client")
    def test_read_skips_short_messages(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        short_msg = self._make_message("short", msg_id=1)
        client.iter_messages = MagicMock(return_value=AsyncIterator([short_msg]))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert len(data) == 0

    @patch("telegram.get_telegram_client")
    def test_read_skips_empty_messages(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        empty_msg = MagicMock()
        empty_msg.text = None
        empty_msg.raw_text = None
        empty_msg.id = 1
        empty_msg.date = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        client.iter_messages = MagicMock(return_value=AsyncIterator([empty_msg]))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert len(data) == 0

    @patch("telegram.get_telegram_client")
    def test_read_default_24h(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        # No dialogs
        client.iter_dialogs = MagicMock(return_value=AsyncIterator([]))
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": ""}):
            asyncio.run(cmd_read())

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert data == []

    @patch("telegram.get_telegram_client")
    def test_read_with_resolve_links(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        msg = self._make_message(
            "Check https://t.me/source_ch/99 this is long enough text", msg_id=1
        )
        client.iter_messages = MagicMock(return_value=AsyncIterator([msg]))

        original = MagicMock()
        original.text = "Original source content"
        original.raw_text = "Original source content"
        client.get_messages.return_value = original

        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch", resolve_links=True,
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert len(data) == 1
        assert "resolved_links" in data[0]

    @patch("telegram.get_telegram_client")
    def test_read_handles_flood_wait(self, mock_get_client, tmp_path):
        from telethon import errors as tl_errors
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        flood_err = tl_errors.FloodWaitError(request=None, capture=0)
        flood_err.seconds = 0

        client.iter_messages = MagicMock(return_value=AsyncIteratorRaise(flood_err))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert data == []

    @patch("telegram.get_telegram_client")
    def test_read_handles_generic_error(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity
        client.iter_messages = MagicMock(return_value=AsyncIteratorRaise(RuntimeError("fail")))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert data == []

    @patch("telegram.get_telegram_client")
    def test_read_skips_messages_outside_range(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity()
        client.get_entity.return_value = entity

        future_msg = self._make_message(
            "Future message that is long enough to pass",
            msg_id=2,
            date=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        in_range_msg = self._make_message(
            "In range message that is long enough to pass",
            msg_id=1,
            date=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
        )
        client.iter_messages = MagicMock(return_value=AsyncIterator([future_msg, in_range_msg]))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert len(data) == 1
        assert data[0]["message_id"] == 1

    @patch("telegram.get_telegram_client")
    def test_read_no_username_url_is_none(self, mock_get_client, tmp_path):
        from telegram import cmd_read
        import telegram
        telegram.MESSAGES_TMP = tmp_path / "messages.json"

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        me = MagicMock()
        me.first_name = "Test"
        me.username = "testuser"
        client.get_me.return_value = me

        entity = self._make_channel_entity(username=None)
        client.get_entity.return_value = entity

        msg = self._make_message("Long enough message for no username test case", msg_id=1)
        client.iter_messages = MagicMock(return_value=AsyncIterator([msg]))
        mock_get_client.return_value = client

        asyncio.run(cmd_read(
            start_date="2026-03-10", end_date="2026-03-10",
            channel="@test_ch",
        ))

        data = json.loads(telegram.MESSAGES_TMP.read_text())
        assert data[0]["url"] is None


# ============================================================
# cmd_post
# ============================================================

class TestCmdPost:
    @patch("telegram.get_telegram_client")
    def test_post_missing_file(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "nonexistent.txt"
        with pytest.raises(SystemExit):
            asyncio.run(telegram.cmd_post())

    @patch("telegram.get_telegram_client")
    def test_post_empty_file(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "empty.txt"
        telegram.LLM_RESPONSE_TMP.write_text("")
        asyncio.run(telegram.cmd_post())
        mock_get_client.assert_not_called()

    def test_post_missing_channel_env(self, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit):
                asyncio.run(telegram.cmd_post())

    @patch("telegram.get_telegram_client")
    def test_post_unauthorized(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")

        client = AsyncMock()
        client.is_user_authorized.return_value = False
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": "@ch"}):
            asyncio.run(telegram.cmd_post())
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_post_resolve_entity_error(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        client.get_entity.side_effect = Exception("not found")
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": "@ch"}):
            asyncio.run(telegram.cmd_post())
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_post_success_at_handle(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        entity = MagicMock()
        client.get_entity.return_value = entity
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": "@my_channel"}):
            asyncio.run(telegram.cmd_post())
        client.send_message.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_post_success_numeric_id(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        entity = MagicMock()
        client.get_entity.return_value = entity
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": "123456"}):
            asyncio.run(telegram.cmd_post())
        client.get_entity.assert_called_with(123456)

    @patch("telegram.get_telegram_client")
    def test_post_send_error_breaks(self, mock_get_client, tmp_path):
        import telegram
        telegram.LLM_RESPONSE_TMP = tmp_path / "response.txt"
        telegram.LLM_RESPONSE_TMP.write_text("digest content")

        client = AsyncMock()
        client.is_user_authorized.return_value = True
        entity = MagicMock()
        client.get_entity.return_value = entity
        client.send_message.side_effect = Exception("send failed")
        mock_get_client.return_value = client

        with patch.dict("os.environ", {"TELEGRAM_PUBLISH_CHANNEL": "@ch"}):
            asyncio.run(telegram.cmd_post())
        client.disconnect.assert_called_once()


# ============================================================
# cmd_list_channels
# ============================================================

class TestCmdListChannels:
    @patch("telegram.get_telegram_client")
    def test_unauthorized(self, mock_get_client):
        from telegram import cmd_list_channels
        client = AsyncMock()
        client.is_user_authorized.return_value = False
        mock_get_client.return_value = client
        asyncio.run(cmd_list_channels())
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_lists_channels(self, mock_get_client):
        from telegram import cmd_list_channels
        from telethon.tl.types import Channel

        client = AsyncMock()
        client.is_user_authorized.return_value = True

        ch = MagicMock(spec=Channel)
        ch.broadcast = True
        ch.username = "test_ch"
        dialog = MagicMock()
        dialog.entity = ch
        dialog.title = "Test Channel"

        client.iter_dialogs = MagicMock(return_value=AsyncIterator([dialog]))
        mock_get_client.return_value = client

        asyncio.run(cmd_list_channels())
        client.disconnect.assert_called_once()

    @patch("telegram.get_telegram_client")
    def test_lists_channel_no_username(self, mock_get_client):
        from telegram import cmd_list_channels
        from telethon.tl.types import Channel

        client = AsyncMock()
        client.is_user_authorized.return_value = True

        ch = MagicMock(spec=Channel)
        ch.broadcast = True
        ch.username = None
        dialog = MagicMock()
        dialog.entity = ch
        dialog.title = "Private Channel"

        client.iter_dialogs = MagicMock(return_value=AsyncIterator([dialog]))
        mock_get_client.return_value = client

        asyncio.run(cmd_list_channels())


# ============================================================
# main (CLI)
# ============================================================

class TestMain:
    def test_no_args_exits(self):
        from telegram import main
        with patch("sys.argv", ["telegram.py"]):
            with pytest.raises(SystemExit):
                main()

    @patch("telegram.asyncio.run")
    def test_read_calls_cmd_read(self, mock_run):
        from telegram import main
        with patch("sys.argv", ["telegram.py", "--read"]):
            main()
        mock_run.assert_called_once()

    @patch("telegram.asyncio.run")
    def test_post_calls_cmd_post(self, mock_run):
        from telegram import main
        with patch("sys.argv", ["telegram.py", "--post"]):
            main()
        mock_run.assert_called_once()

    @patch("telegram.asyncio.run")
    def test_list_channels_calls_cmd(self, mock_run):
        from telegram import main
        with patch("sys.argv", ["telegram.py", "--list-channels"]):
            main()
        mock_run.assert_called_once()


# ============================================================
# Helpers
# ============================================================

class AsyncIterator:
    """Helper to mock async for loops."""
    def __init__(self, items):
        self.items = list(items)
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.index]
        self.index += 1
        return item


class AsyncIteratorRaise:
    """Async iterator that raises on first iteration."""
    def __init__(self, exc):
        self.exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self.exc
