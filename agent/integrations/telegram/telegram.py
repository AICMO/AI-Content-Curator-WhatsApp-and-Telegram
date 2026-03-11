#!/usr/bin/env python3
"""Telegram AI Content Curator — single entry point.

Usage:
  python telegram.py --read                                    # Last 1 day
  python telegram.py --read --start-date 2026-03-01            # From date to now
  python telegram.py --read --start-date 2026-03-01 --end-date 2026-03-05  # Date range
  python telegram.py --read --channel @my_channel --resolve-links  # Read specific channel + resolve links
  python telegram.py --post                                    # Publish digest
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import errors
from telethon.sessions import StringSession
from telethon.tl.types import Channel

MESSAGES_TMP = Path("/tmp/telegram_messages.json")
LLM_RESPONSE_TMP = Path("/tmp/llm_response.txt")
TELEGRAM_MSG_LIMIT = 4096


def get_telegram_client():
    from telethon import TelegramClient

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    session_string = os.environ.get("TELEGRAM_SESSION_STRING")

    missing = []
    if not api_id:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not session_string:
        missing.append("TELEGRAM_SESSION_STRING")

    if missing:
        print(f"Error: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    return TelegramClient(StringSession(session_string), int(api_id), api_hash)


# ============================================================
# READ: --read
# ============================================================

TG_LINK_RE = re.compile(r"https://t\.me/([a-zA-Z0-9_]+)/(\d+)")


async def _resolve_links(client, collected):
    """Scan collected messages for t.me links and fetch original content."""
    all_links = []
    for msg in collected:
        links = TG_LINK_RE.findall(msg.get("text", ""))
        for channel_username, msg_id_str in links:
            all_links.append((msg, channel_username, int(msg_id_str)))

    if not all_links:
        print("No t.me links found to resolve.")
        return

    print(f"\nResolving {len(all_links)} t.me links...")
    resolved_count = 0

    for msg, channel_username, msg_id in all_links:
        try:
            entity = await client.get_entity(channel_username)
            original = await client.get_messages(entity, ids=msg_id)
            if original and (original.text or original.raw_text):
                resolved = {
                    "url": f"https://t.me/{channel_username}/{msg_id}",
                    "channel": channel_username,
                    "text": original.text or original.raw_text,
                }
                msg.setdefault("resolved_links", []).append(resolved)
                resolved_count += 1
            await asyncio.sleep(0.5)
        except errors.FloodWaitError as e:
            print(f"  FloodWait: sleeping {e.seconds}s resolving {channel_username}/{msg_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"  Could not resolve https://t.me/{channel_username}/{msg_id}: {e}")

    print(f"Resolved {resolved_count}/{len(all_links)} links")


async def cmd_read(start_date: str = None, end_date: str = None,
                   channel: str = None, resolve_links: bool = False):
    client = get_telegram_client()
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Session is not authorized. Run setup_session.py first.")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username or 'no_username'})")

    if start_date:
        cutoff = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cutoff_end = (
            datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            if end_date
            else datetime.now(timezone.utc)
        )
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        cutoff_end = datetime.now(timezone.utc)

    # Build channel list: single channel (--channel) or all subscribed
    if channel:
        try:
            entity = await client.get_entity(channel)
            channels = [(entity, getattr(entity, "title", channel))]
        except Exception as e:
            print(f"ERROR: Could not resolve channel {channel}: {e}")
            await client.disconnect()
            return
        print(f"Reading channel {channel} since {cutoff.strftime('%Y-%m-%d %H:%M UTC')}...\n")
    else:
        publish_channel = os.environ.get("TELEGRAM_PUBLISH_CHANNEL", "").lstrip("@").lower()
        channel_list = []
        async for dialog in client.iter_dialogs():
            if isinstance(dialog.entity, Channel) and dialog.entity.broadcast:
                username = (getattr(dialog.entity, "username", None) or "").lower()
                if username and username == publish_channel:
                    continue
                channel_list.append(dialog)
        channels = [(d.entity, d.title) for d in channel_list]
        print(f"Scanning {len(channels)} channels since {cutoff.strftime('%Y-%m-%d %H:%M UTC')}...\n")

    collected = []
    total_read = 0

    for entity, title in channels:
        channel_collected = 0
        username = getattr(entity, "username", None)

        try:
            async for message in client.iter_messages(entity, limit=100):
                msg_date = message.date.replace(tzinfo=timezone.utc)
                if msg_date < cutoff:
                    break
                if msg_date > cutoff_end:
                    continue

                total_read += 1

                if not message.text and not message.raw_text:
                    continue

                text = message.text or message.raw_text
                if len(text.strip()) < 20:
                    continue

                collected.append({
                    "channel_title": title,
                    "channel_username": username,
                    "message_id": message.id,
                    "date": message.date.isoformat(),
                    "text": text,
                    "url": f"https://t.me/{username}/{message.id}" if username else None,
                })
                channel_collected += 1

        except errors.FloodWaitError as e:
            print(f"  FloodWait: sleeping {e.seconds}s for {title}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"  Error reading {title}: {e}")

        if channel_collected:
            print(f"  {title}: {channel_collected} messages")

        await asyncio.sleep(1)

    if resolve_links:
        await _resolve_links(client, collected)

    await client.disconnect()

    MESSAGES_TMP.write_text(json.dumps(collected, indent=2, ensure_ascii=False) + "\n")
    print(f"\nExtracted {len(collected)} from {total_read} read across {len(channels)} channels")
    print(f"Written to {MESSAGES_TMP}")


# ============================================================
# PUBLISH: --post
# ============================================================

def _split_message(text: str) -> list[str]:
    """Split text into chunks that fit Telegram's message limit."""
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    parts = []
    while text:
        if len(text) <= TELEGRAM_MSG_LIMIT:
            parts.append(text)
            break
        split_at = text.rfind('\n', 0, TELEGRAM_MSG_LIMIT)
        if split_at == -1:
            split_at = TELEGRAM_MSG_LIMIT
        parts.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return parts


async def cmd_post():
    if not LLM_RESPONSE_TMP.exists():
        print(f"Error: {LLM_RESPONSE_TMP} not found. Run the LLM pipeline first.")
        sys.exit(1)

    digest_text = LLM_RESPONSE_TMP.read_text().strip()
    if not digest_text:
        print("LLM response is empty, nothing to publish.")
        return

    channel_id = os.environ.get("TELEGRAM_PUBLISH_CHANNEL")
    if not channel_id:
        print("Error: TELEGRAM_PUBLISH_CHANNEL environment variable not set")
        sys.exit(1)

    client = get_telegram_client()
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Session is not authorized.")
        await client.disconnect()
        return

    try:
        if channel_id.startswith("@"):
            entity = await client.get_entity(channel_id)
        else:
            entity = await client.get_entity(int(channel_id))
    except Exception as e:
        print(f"ERROR: Could not resolve {channel_id}: {e}")
        await client.disconnect()
        return

    parts = _split_message(digest_text)
    print(f"Publishing {len(parts)} message(s) to {channel_id}")

    for part in parts:
        try:
            await client.send_message(entity, part, link_preview=False)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"ERROR posting to {channel_id}: {e}")
            break

    await client.disconnect()
    print("Done.")


# ============================================================
# LIST CHANNELS: --list-channels
# ============================================================

async def cmd_list_channels():
    client = get_telegram_client()
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Session is not authorized. Run setup_session.py first.")
        await client.disconnect()
        return

    channels = []
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, Channel) and dialog.entity.broadcast:
            channels.append(dialog)

    print(f"Found {len(channels)} channels:\n")
    for d in sorted(channels, key=lambda x: x.title.lower()):
        username = getattr(d.entity, "username", None)
        handle = f"@{username}" if username else "(no username)"
        print(f"  {d.title}  {handle}")

    await client.disconnect()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Telegram AI Content Curator")
    parser.add_argument("--read", action="store_true", help="Read channels → /tmp/telegram_messages.json")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD, default: last 24h)")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD, default: now)")
    parser.add_argument("--channel", type=str, help="Read from a specific channel (e.g. @my_channel)")
    parser.add_argument("--resolve-links", action="store_true", help="Resolve t.me links in messages")
    parser.add_argument("--post", action="store_true", help="Publish /tmp/llm_response.txt to channel")
    parser.add_argument("--list-channels", action="store_true", help="List all subscribed broadcast channels")
    args = parser.parse_args()

    if not any([args.read, args.post, args.list_channels]):
        parser.print_help()
        sys.exit(1)

    if args.list_channels:
        asyncio.run(cmd_list_channels())
    elif args.read:
        asyncio.run(cmd_read(start_date=args.start_date, end_date=args.end_date,
                             channel=args.channel, resolve_links=args.resolve_links))
    elif args.post:
        asyncio.run(cmd_post())


if __name__ == "__main__":
    main()
