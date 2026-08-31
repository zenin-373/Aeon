#!/usr/bin/env python3
"""Document / video thumbnail helpers (Aeon-style leech thumbs)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("hstream-tg")

def download_poster_thumb(poster_url: str, dest_dir: Path) -> Optional[Path]:
    if not poster_url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = dest_dir / "poster_raw"
    out = dest_dir / "thumb.jpg"
    try:
        with requests.get(poster_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                return None
            with open(raw, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(raw),
                "-vf", "scale=320:-1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        raw.unlink(missing_ok=True)
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Poster thumb failed: %s", e)
    return None


def extract_video_thumb(video_path: Path, dest_dir: Path) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{video_path.stem}_thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "5",
                "-i", str(video_path),
                "-vf", "scale=320:-1",
                "-vframes", "1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Video thumb extract failed for %s: %s", video_path.name, e)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video_path),
                "-vf", "scale=320:-1",
                "-vframes", "1",
                "-q:v", "5",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception as e:
        logger.warning("Video thumb fallback failed: %s", e)
    return None


def resolve_doc_thumb(
    video_path: Path,
    series_thumb: Optional[Path],
    work_dir: Path,
) -> Optional[str]:
    if series_thumb and series_thumb.exists():
        return str(series_thumb)
    thumb = extract_video_thumb(video_path, work_dir)
    return str(thumb) if thumb else None
