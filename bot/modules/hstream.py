"""
/hs — hstream.moe episode leech.

Posts ONLY to Config.HSTREAM_CHANNEL (when set).
Does not use LEECH_DUMP_CHAT and does not affect /leech or /mirror.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bot import LOGGER
from bot.core.config_manager import Config
from bot.core.telegram_manager import TgClient
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.ext_utils.hstream_extractor import (
    episode_url_to_series_url,
    process_url,
    scrape_series_info,
)
from bot.helper.ext_utils.hstream_thumb import download_poster_thumb, resolve_doc_thumb
from bot.helper.telegram_helper.message_utils import edit_message, send_message

URL_RE = re.compile(r"https?://(?:www\.)?hstream\.moe/hentai/[\w\-]+/?", re.I)
_executor = ThreadPoolExecutor(max_workers=2)


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or "file"


def _ep_num(url: str) -> str:
    token = url.rstrip("/").split("/")[-1]
    m = re.search(r"-(\d+)$", token)
    return m.group(1) if m else "?"


def _rename(path: Path, title: str, ep: str) -> Path:
    ext = path.suffix or ".mkv"
    new_name = f"{_sanitize(title)} - {ep}{ext}"
    target = path.with_name(new_name)
    if target.resolve() != path.resolve():
        if target.exists():
            target.unlink()
        path.rename(target)
    return target


def _parse_channel(raw: str):
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


async def _safe_edit(msg, text: str):
    try:
        await edit_message(msg, text)
    except Exception:
        pass


@new_task
async def hstream_leech(_, message):
    """
    /hs <hstream.moe episode url(s)>
    Download + optional Eng sub remux, upload to HSTREAM_CHANNEL only.
    """
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    if message.reply_to_message:
        body += " " + (
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )

    urls = URL_RE.findall(body)
    if not urls:
        await send_message(
            message,
            "Usage: <code>/hs https://hstream.moe/hentai/title-1</code>\n"
            "Or reply to a message that contains episode link(s).\n\n"
            "Uploads go to <b>HSTREAM_CHANNEL</b> only (bot settings → Config). "
            "Does not affect /leech or /mirror.",
        )
        return

    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    dest_chat = _parse_channel(Config.HSTREAM_CHANNEL)
    if dest_chat is None:
        dest_chat = message.chat.id
        channel_note = "HSTREAM_CHANNEL not set → uploading here"
    else:
        channel_note = f"Upload target: <code>{Config.HSTREAM_CHANNEL}</code>"

    status = await send_message(
        message,
        f"🚀 <b>HStream</b> — {len(urls)} link(s)\n{channel_note}",
    )

    uid = message.from_user.id if message.from_user else 0
    work = Path(f"/tmp/hstream/{uid}")
    work.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for u in urls:
        key = episode_url_to_series_url(u)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(u)

    ep_global = 0
    total = len(urls)

    for series_key in order:
        series_urls = groups[series_key]

        def _scrape(u=series_urls[0]):
            return scrape_series_info(u)

        try:
            info = await loop.run_in_executor(_executor, _scrape)
        except Exception as e:
            LOGGER.warning("HStream series scrape failed: %s", e)
            from bot.helper.ext_utils.hstream_extractor import SeriesInfo

            info = SeriesInfo(series_url=series_key)

        anime_title = info.title or series_key.rstrip("/").split("/")[-1]
        series_caption = f"<b>📖 {anime_title}</b>"
        if info.year:
            series_caption += f"\n📅 {info.year}"
        if info.episodes:
            series_caption += f"\n📑 Episodes: {info.episodes}"

        thumb_dir = work / "_thumbs" / series_key.rstrip("/").split("/")[-1]
        series_thumb = None
        if info.poster_url:
            try:
                series_thumb = await loop.run_in_executor(
                    _executor,
                    lambda: download_poster_thumb(info.poster_url, thumb_dir),
                )
            except Exception:
                series_thumb = None

        try:
            if info.poster_url:
                await TgClient.bot.send_photo(
                    chat_id=dest_chat,
                    photo=info.poster_url,
                    caption=series_caption,
                )
            else:
                await TgClient.bot.send_message(dest_chat, series_caption)
        except Exception as e:
            LOGGER.warning("HStream poster post failed: %s", e)

        for url in series_urls:
            ep_global += 1
            idx = ep_global

            def progress_cb(msg: str, _i=idx, _t=total):
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status, f"<b>[{_i}/{_t}]</b>\n{msg}"),
                    loop,
                )

            try:
                final_path: Path = await loop.run_in_executor(
                    _executor,
                    lambda u=url: process_url(u, work, progress=progress_cb),
                )
            except Exception as e:
                LOGGER.exception("HStream failed %s", url)
                await _safe_edit(
                    status, f"❌ [{idx}/{total}] failed\n<code>{url}</code>\n{e}"
                )
                continue

            ep = _ep_num(url)
            final_path = _rename(final_path, anime_title, ep)
            caption = final_path.name

            await _safe_edit(
                status,
                f"📤 [{idx}/{total}] uploading…\n<code>{final_path.name}</code>",
            )

            thumb = None
            try:
                thumb = await loop.run_in_executor(
                    _executor,
                    lambda: resolve_doc_thumb(final_path, series_thumb, thumb_dir),
                )
            except Exception:
                thumb = None

            try:
                kwargs = dict(
                    chat_id=dest_chat,
                    document=str(final_path),
                    file_name=final_path.name,
                    caption=caption,
                )
                if thumb:
                    kwargs["thumb"] = thumb
                await TgClient.bot.send_document(**kwargs)
            except Exception as e:
                LOGGER.exception("HStream upload failed")
                await send_message(message, f"⚠️ Upload failed: {e}")
            finally:
                try:
                    final_path.unlink(missing_ok=True)
                except Exception:
                    pass

    await _safe_edit(status, f"🎉 HStream done — {total} episode(s).")
