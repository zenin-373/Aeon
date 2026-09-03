#!/usr/bin/env python3
"""Core download + subtitle remux for /hs (hstream.moe)."""

from __future__ import annotations

import contextlib
import html as html_lib
import re
import subprocess
import sys
import time
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


def _session(cookies_file: Path | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://hstream.moe/",
            "Origin": "https://hstream.moe",
        }
    )
    if cookies_file and cookies_file.is_file():
        for line in cookies_file.read_text(errors="ignore").splitlines():
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7 and "hstream.moe" in cols[0]:
                s.cookies.set(
                    cols[5], cols[6], domain="hstream.moe", path=cols[2] or "/"
                )
    return s


def resolve_stream_urls(
    page_url: str,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[str]:
    """Resolve progressive/DASH URLs via live session + /player/api (needs XSRF)."""

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    s = _session(cookies_file)
    try:
        r = s.get(page_url, timeout=30)
        if r.status_code != 200:
            log(f"page HTTP {r.status_code}")
            return []
        html = r.text
    except Exception as e:
        log(f"page fetch failed: {e}")
        return []

    m = re.search(
        r'id=["\']e_id["\'][^>]*value=["\']([^"\']+)["\']'
        r'|value=["\']([^"\']+)["\'][^>]*id=["\']e_id["\']',
        html,
        re.IGNORECASE,
    )
    e_id = (m.group(1) or m.group(2)) if m else None
    if not e_id:
        log("no e_id on page")
        return []

    xsrf = s.cookies.get("XSRF-TOKEN")
    token = unquote(xsrf) if xsrf else ""
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": token,
        "Referer": page_url,
        "Origin": "https://hstream.moe",
        "Accept": "application/json",
    }
    try:
        resp = s.post(
            "https://hstream.moe/player/api",
            headers=headers,
            json={"episode_id": str(e_id)},
            timeout=30,
        )
        if resp.status_code != 200:
            log(f"player API HTTP {resp.status_code}: {resp.text[:120]}")
            return []
        data = resp.json()
    except Exception as e:
        log(f"player API failed: {e}")
        return []

    stream_url = data.get("stream_url") or data.get("streamUrl") or ""
    domains = data.get("stream_domains") or data.get("streamDomains") or []
    if isinstance(domains, str):
        domains = [domains]
    if not stream_url or not domains:
        log("player API missing stream fields")
        return []

    domain = str(domains[0])
    if not domain.startswith("http"):
        domain = "https://" + domain.lstrip("/")
    base = f"{domain.rstrip('/')}/{stream_url.strip('/')}"

    candidates = [
        f"{base}/x264.720p.mp4",
        f"{base}/av1.1080p.webm",
        f"{base}/720/manifest.mpd",
        f"{base}/1080/manifest.mpd",
        f"{base}/av1.720p.webm",
    ]
    log(f"resolved {len(candidates)} stream(s)")
    return candidates


def _download_http(
    url: str,
    dest_file: Path,
    progress: ProgressCallback | None = None,
) -> Path:
    """Direct progressive download (mp4/webm) with Referer."""

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://hstream.moe/",
        "Origin": "https://hstream.moe",
    }
    log(f"HTTP download: {url.rsplit('/', maxsplit=1)[-1]}")
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        last = 0.0
        with open(dest_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if progress and (now - last > 1.2 or (total and done >= total)):
                    last = now
                    pct = (100.0 * done / total) if total else 0.0
                    progress(
                        f"📥 <b>Download</b>\n"
                        f"<code>{dest_file.name}</code>\n"
                        f"{_progress_bar(pct)} <b>{pct:.1f}%</b>\n"
                        f"Processed: {_human_bytes(done)}\n"
                        f"Size: {_human_bytes(total) if total else '—'}\n"
                        f"Tool: http"
                    )
    if not dest_file.exists() or dest_file.stat().st_size < 1000:
        raise RuntimeError(f"HTTP download too small/failed: {url}")
    return dest_file


def download_video(
    url: str,
    dest: Path,
    cookies_file: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    """Prefer player-API progressive files; yt-dlp only as secondary."""

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    dest.mkdir(parents=True, exist_ok=True)
    log(f"Downloading: {url}")

    streams = resolve_stream_urls(url, cookies_file=cookies_file, progress=progress)
    last_err: Exception | None = None

    for s in streams:
        if not (s.endswith((".mp4", ".webm"))):
            continue
        name = s.rstrip("/").split("/")[-1]
        slug = url.rstrip("/").split("/")[-1]
        out = dest / f"{slug}-{name}"
        try:
            h = requests.head(
                s,
                headers={"Referer": "https://hstream.moe/"},
                timeout=20,
                allow_redirects=True,
            )
            if h.status_code >= 400:
                continue
            return _download_http(s, out, progress=progress)
        except Exception as e:
            last_err = e
            log(f"HTTP fail {name}: {e}")
            with contextlib.suppress(Exception):
                out.unlink(missing_ok=True)
            continue

    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError(f"Download failed (no yt-dlp): {last_err}") from e

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
    try_urls = [u for u in streams if u.endswith(".mpd")] + [url]
    last_update = [0.0]
    last_filename = [""]

    def hook(d: dict) -> None:
        if not progress:
            return
        if d.get("status") == "downloading":
            now = time.time()
            if now - last_update[0] < 1.2:
                return
            last_update[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (100.0 * done / total) if total else 0.0
            name = d.get("filename") or last_filename[0] or "download"
            last_filename[0] = name
            progress(
                f"📥 <b>Download</b>\n<code>{Path(name).name}</code>\n"
                f"{_progress_bar(pct)} <b>{pct:.1f}%</b>\n"
                f"Processed: {_human_bytes(done)}\n"
                f"Size: {_human_bytes(total) if total else '—'}\n"
                f"Tool: yt-dlp"
            )

    for try_url in try_urls:
        ydl_opts = {
            "format": "best",
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
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        }
        if cookies_file and cookies_file.exists():
            ydl_opts["cookiefile"] = str(cookies_file)
        try:
            log(f"  yt-dlp: {try_url[:90]}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([try_url])
            last_err = None
            break
        except Exception as e:
            last_err = e
            continue

    if last_err is not None:
        raise RuntimeError(f"Download failed: {last_err}") from last_err

    files = [
        p
        for p in dest.glob("*")
        if p.is_file()
        and p.suffix.lower() not in {".ass", ".part", ".ytdl", ".temp"}
    ]
    if not files:
        raise FileNotFoundError("No video file produced.")
    return max(files, key=lambda p: p.stat().st_ctime)


def download_subtitle(sub_url: str, sub_path: Path) -> bool:
    try:
        with requests.get(
            sub_url,
            stream=True,
            timeout=30,
            headers={"Referer": "https://hstream.moe/"},
        ) as r:
            if r.status_code != 200:
                return False
            with open(sub_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return sub_path.exists() and sub_path.stat().st_size > 10
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

    s = _session(cookies_file)
    html = ""
    try:
        r = s.get(page_url, timeout=30)
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
        xsrf = s.cookies.get("XSRF-TOKEN")
        token = unquote(xsrf) if xsrf else ""
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": token,
            "Referer": page_url,
            "Origin": "https://hstream.moe",
        }
        resp = s.post(
            "https://hstream.moe/player/api",
            headers=headers,
            json={"episode_id": str(e_id)},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        stream_url = data.get("stream_url") or ""
        domains = data.get("stream_domains") or []
        if isinstance(domains, str):
            domains = [domains]
        if not stream_url or not domains:
            return None
        domain = str(domains[0])
        if not domain.startswith("http"):
            domain = "https://" + domain.lstrip("/")
        return f"{domain.rstrip('/')}/{stream_url.strip('/')}/eng.ass"
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
    s = _session(cookies_file)
    try:
        r = s.get(series_url, timeout=30)
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
            for y in (year, "2026", "2025", "2024", "2008", "2023"):
                for slug in candidates:
                    alt = ".".join(w.capitalize() for w in slug_part.split("-"))
                    for su in (
                        f"{host}/{y}/{slug}/E{ep_num:02d}/eng.ass",
                        f"{host}/{y}/{alt}/E{ep_num:02d}/eng.ass",
                    ):
                        if download_subtitle(su, sub_path):
                            sub_ok = True
                            break
                    if sub_ok:
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
