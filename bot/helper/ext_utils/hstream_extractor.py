#!/usr/bin/env python3
"""Core download + subtitle remux for /hs (hstream.moe)."""

from __future__ import annotations

import contextlib
import html as html_lib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import requests

ProgressCallback = Callable[[str], None]


@dataclass
class SeriesInfo:
    title: str = ""
    title_jp: str = ""
    year: str = ""
    release_date: str = ""
    upload_date: str = ""
    studio: str = ""
    tags: list[str] = field(default_factory=list)
    episodes: int | None = None
    description: str = ""
    poster_url: str = ""
    series_url: str = ""
    status: str = ""


def ensure_dependencies(progress: ProgressCallback | None = None) -> None:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    with contextlib.suppress(Exception):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "-U",
                "yt-dlp",
                "requests",
                "hanime-plugin",
            ],
            check=False,
            capture_output=True,
        )


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def _progress_bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100.0)
    return "●" * filled + "○" * (width - filled)


def _cookies_header(cookies_file: Path | None) -> str | None:
    if not cookies_file or not cookies_file.is_file():
        return None
    parts = []
    for line in cookies_file.read_text(errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7 and "hstream.moe" in cols[0]:
            parts.append(f"{cols[5]}={cols[6]}")
    return "; ".join(parts) if parts else None


def resolve_stream_urls(
    page_url: str,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[str]:
    """Direct media URLs via /player/api — stock yt-dlp cannot extract hstream.moe."""

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    ch = _cookies_header(cookies_file)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://hstream.moe/",
    }
    if ch:
        headers["Cookie"] = ch

    html = ""
    try:
        r = requests.get(page_url, headers=headers, timeout=30)
        if r.status_code == 200:
            html = r.text
    except Exception as e:
        log(f"page fetch failed: {e}")
        return []

    m = re.search(
        r'id=["\']e_id["\'][^>]*value=["\']([^"\']+)["\']'
        r'|value=["\']([^"\']+)["\'][^>]*id=["\']e_id["\']',
        html or "",
        re.IGNORECASE,
    )
    e_id = (m.group(1) or m.group(2)) if m else None
    if not e_id:
        log("no e_id on page")
        return []

    api = dict(headers)
    api["Content-Type"] = "application/json"
    api["X-Requested-With"] = "XMLHttpRequest"
    if ch:
        for part in ch.split(";"):
            part = part.strip()
            if part.upper().startswith("XSRF-TOKEN="):
                api["X-XSRF-TOKEN"] = unquote(part.split("=", 1)[1])
                break

    try:
        resp = requests.post(
            "https://hstream.moe/player/api",
            headers=api,
            json={"episode_id": e_id},
            timeout=30,
        )
        if resp.status_code != 200:
            resp = requests.post(
                "https://hstream.moe/player/api",
                headers=api,
                data={"episode_id": e_id},
                timeout=30,
            )
        if resp.status_code != 200:
            log(f"player API HTTP {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:
        log(f"player API failed: {e}")
        return []

    stream_url = data.get("stream_url") or data.get("streamUrl") or ""
    domains = data.get("stream_domains") or data.get("streamDomains") or []
    resolution = str(data.get("resolution") or "720p").lower()
    if isinstance(domains, str):
        domains = [domains]
    if not stream_url or not domains:
        log("player API missing stream fields")
        return []

    domain = str(domains[0])
    if not domain.startswith("http"):
        domain = "https://" + domain.lstrip("/")
    base = f"{domain.rstrip('/')}/{stream_url.strip('/')}"

    candidates: list[str] = []
    if "4k" in resolution or "2160" in resolution or "1080" in resolution:
        candidates += [
            f"{base}/av1.1080p.webm",
            f"{base}/1080/manifest.mpd",
            f"{base}/x264.720p.mp4",
            f"{base}/720/manifest.mpd",
        ]
    else:
        candidates += [
            f"{base}/x264.720p.mp4",
            f"{base}/720/manifest.mpd",
            f"{base}/av1.1080p.webm",
            f"{base}/1080/manifest.mpd",
        ]
    log(f"resolved {len(candidates)} stream candidate(s)")
    return candidates


def download_video(
    url: str,
    dest: Path,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download via hanime-plugin and/or player-API direct streams."""

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    import time

    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("yt-dlp is required") from e

    with contextlib.suppress(Exception):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "-U",
                "hanime-plugin",
                "yt-dlp",
            ],
            check=False,
            capture_output=True,
        )

    output_template = str(dest / "%(title)s.%(ext)s")
    format_tries = [
        "best",
        "bestvideo*+bestaudio/best",
        "best[height<=1080]",
        "best[height<=720]",
    ]

    last_update = [0.0]
    last_filename = [""]

    def hook(d: dict) -> None:
        if not progress:
            return
        status = d.get("status")
        if status == "downloading":
            now = time.time()
            if now - last_update[0] < 1.2 and d.get("downloaded_bytes", 0) > 0:
                return
            last_update[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            pct = (100.0 * done / total) if total else 0.0
            name = d.get("filename") or last_filename[0] or "download"
            last_filename[0] = name
            short = Path(name).name if name else "download"
            eta_s = (
                f"{int(eta)}s"
                if isinstance(eta, (int, float)) and eta is not None
                else "—"
            )
            bar = _progress_bar(pct)
            progress(
                f"📥 <b>Download</b>\n<code>{short}</code>\n"
                f"{bar} <b>{pct:.2f}%</b>\n"
                f"Processed: {_human_bytes(done)}\n"
                f"Size: {_human_bytes(total) if total else '—'}\n"
                f"Speed: {_human_bytes(speed)}/s\n"
                f"ETA: {eta_s}\n"
                f"Tool: yt-dlp"
            )
        elif status == "finished":
            name = d.get("filename") or last_filename[0] or "file"
            last_filename[0] = name
            progress(f"✅ Download finished\n<code>{Path(name).name}</code>")

    last_err: Exception | None = None
    log(f"Downloading: {url}")

    try_urls: list[str] = [url]
    try:
        for s in resolve_stream_urls(
            url, cookies_file=cookies_file, progress=progress
        ):
            if s not in try_urls:
                try_urls.append(s)
    except Exception as e:
        log(f"stream resolve skipped: {e}")

    for try_url in try_urls:
        for fmt in format_tries:
            ydl_opts = {
                "format": fmt,
                "outtmpl": output_template,
                "noplaylist": True,
                "retries": 5,
                "fragment_retries": 5,
                "concurrent_fragment_downloads": 8,
                "progress_hooks": [hook],
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "http_headers": {
                    "Referer": "https://hstream.moe/",
                    "Origin": "https://hstream.moe",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            }
            if shutil.which("aria2c"):
                ydl_opts["external_downloader"] = "aria2c"
                ydl_opts["external_downloader_args"] = {
                    "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
                }
            if cookies_file and cookies_file.exists():
                ydl_opts["cookiefile"] = str(cookies_file)
            try:
                short = try_url if len(try_url) < 90 else try_url[:87] + "..."
                log(f"  trying {short} · format={fmt}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([try_url])
                last_err = None
                break
            except Exception as e:
                last_err = e
                continue
        if last_err is None:
            break

    if last_err is not None:
        raise RuntimeError(f"Download failed: {last_err}") from last_err

    files = [
        p
        for p in dest.glob("*")
        if p.is_file()
        and p.suffix.lower() not in {".ass", ".part", ".ytdl", ".temp"}
    ]
    if not files:
        raise FileNotFoundError("No video file produced by yt-dlp.")
    return max(files, key=lambda p: p.stat().st_ctime)


def download_subtitle(sub_url: str, sub_path: Path) -> bool:
    try:
        with requests.get(sub_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                return False
            with open(sub_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception:
        return False


def resolve_subtitle_url(
    page_url: str,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> str | None:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    ch = _cookies_header(cookies_file)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://hstream.moe/",
    }
    if ch:
        headers["Cookie"] = ch
    html = ""
    try:
        r = requests.get(page_url, headers=headers, timeout=30)
        if r.status_code == 200:
            html = r.text
            found = []
            for pat in [
                r'href=["\'](https?://[^"\']+?/eng\.ass)["\']',
                r'href=["\'](https?://[^"\']+?\.ass)["\']',
            ]:
                for m in re.finditer(pat, html, re.IGNORECASE):
                    if m.group(1) not in found:
                        found.append(m.group(1))
            for u in found:
                if "eng.ass" in u.lower():
                    return u
            if found:
                return found[0]
    except Exception as e:
        log(f"page scrape failed: {e}")
    try:
        m = re.search(
            r'id=["\']e_id["\'][^>]*value=["\']([^"\']+)["\']|value=["\']([^"\']+)["\'][^>]*id=["\']e_id["\']',
            html or "",
            re.IGNORECASE,
        )
        e_id = (m.group(1) or m.group(2)) if m else None
        if not e_id:
            return None
        api = dict(headers)
        api["Content-Type"] = "application/json"
        api["X-Requested-With"] = "XMLHttpRequest"
        if ch:
            for part in ch.split(";"):
                part = part.strip()
                if part.upper().startswith("XSRF-TOKEN="):
                    api["X-XSRF-TOKEN"] = unquote(part.split("=", 1)[1])
                    break
        resp = requests.post(
            "https://hstream.moe/player/api",
            headers=api,
            json={"episode_id": e_id},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        stream_url = data.get("stream_url") or data.get("streamUrl") or ""
        domains = data.get("stream_domains") or data.get("streamDomains") or []
        if isinstance(domains, str):
            domains = [domains]
        if not stream_url or not domains:
            return None
        domain = domains[0]
        if not str(domain).startswith("http"):
            domain = "https://" + str(domain).lstrip("/")
        return f"{str(domain).rstrip('/')}/{stream_url.strip('/')}/eng.ass"
    except Exception:
        return None


def remux_to_mkv(video_path: Path, sub_path: Path, output_mkv: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(sub_path),
            "-map",
            "0",
            "-map",
            "1",
            "-c",
            "copy",
            "-metadata:s:s:0",
            "language=eng",
            str(output_mkv),
        ],
        check=True,
        capture_output=True,
    )


def series_folder_name(url: str) -> str:
    token = url.rstrip("/").split("/")[-1]
    name = re.sub(r"-\d+$", "", token)
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip() or "unknown"


def episode_url_to_series_url(episode_url: str) -> str:
    base = episode_url.rstrip("/").split("?")[0]
    return re.sub(r"-\d+$", "", base)


def scrape_series_info(
    episode_or_series_url: str,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> SeriesInfo:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    info = SeriesInfo()
    series_url = episode_url_to_series_url(episode_or_series_url)
    info.series_url = series_url
    ch = _cookies_header(cookies_file)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://hstream.moe/",
    }
    if ch:
        headers["Cookie"] = ch
    try:
        r = requests.get(series_url, headers=headers, timeout=30)
        if r.status_code != 200:
            return info
        page = r.text
    except Exception:
        return info

    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.IGNORECASE | re.DOTALL)
    if m:
        info.title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not info.title:
        m = re.search(r'property="og:title"\s+content="([^"]+)"', page)
        if m:
            info.title = re.sub(
                r"\s*-\s*Watch All.*$", "", html_lib.unescape(m.group(1))
            ).strip()
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", page)
    if dates:
        info.year = min(set(dates))[:4]
    m = re.search(r"Episodes\s*\((\d+)\)", page, re.IGNORECASE)
    if m:
        info.episodes = int(m.group(1))
        info.status = "Completed"
    covers = re.findall(
        r'((?:https://hstream\.moe)?/images/hentai/[^"\']+/cover[^"\']+\.webp)',
        page,
        re.IGNORECASE,
    )
    if covers:
        u = covers[0]
        info.poster_url = u if u.startswith("http") else f"https://hstream.moe{u}"
    if not info.poster_url:
        m = re.search(r'property="og:image"\s+content="([^"]+)"', page)
        if m:
            info.poster_url = m.group(1)
    log(f"Series info: {info.title or series_url}")
    return info


def process_url(
    url: str,
    dest: Path,
    series_slug: str | None = None,
    year: str = "2024",
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    folder = dest / series_folder_name(url)
    folder.mkdir(parents=True, exist_ok=True)
    video_path = download_video(
        url, folder, cookies_file=cookies_file, progress=progress
    )
    base_name = video_path.stem
    final_mkv = folder / f"{base_name}.mkv"
    if video_path.suffix.lower() == ".mkv":
        return video_path
    ep_match = re.search(r"-(\d+)/?$", url.rstrip("/"))
    if not ep_match:
        return video_path
    ep_num = int(ep_match.group(1))
    slug_part = re.sub(r"-\d+$", "", url.rstrip("/").split("/")[-1])
    sub_path = folder / f"{base_name}.ass"
    sub_ok = False
    log("Resolving subtitle…")
    live_sub = resolve_subtitle_url(
        url, cookies_file=cookies_file, progress=progress
    )
    if live_sub and download_subtitle(live_sub, sub_path):
        sub_ok = True
    if not sub_ok:
        candidates = [slug_part, ".".join(slug_part.split("-"))]
        hosts = [
            "https://oppai-str.shoujo-h.org",
            "https://imoto-str.ane-h.xyz",
            "https://shinobu-str.rorikon-h.xyz",
        ]
        for host in hosts:
            for y in (year, "2026", "2025", "2024", "2023"):
                for slug in candidates:
                    sub_url = f"{host}/{y}/{slug}/E{ep_num:02d}/eng.ass"
                    if download_subtitle(sub_url, sub_path):
                        sub_ok = True
                        break
                if sub_ok:
                    break
            if sub_ok:
                break
    if sub_ok:
        log("Remuxing → MKV…")
        remux_to_mkv(video_path, sub_path, final_mkv)
        sub_path.unlink(missing_ok=True)
        if video_path != final_mkv and video_path.exists():
            video_path.unlink()
        return final_mkv
    return video_path
