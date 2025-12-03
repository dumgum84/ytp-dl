#!/usr/bin/env python3
from __future__ import annotations
import os
import shlex
import shutil
import subprocess
import time
from typing import Optional, List

# =========================
# Config / constants
# =========================
VENV_PATH = os.environ.get("YTPDL_VENV", "/opt/yt-dlp-mullvad/venv")
YTDLP_BIN = os.path.join(VENV_PATH, "bin", "yt-dlp")
MULLVAD_LOCATION = os.environ.get("YTPDL_MULLVAD_LOCATION", "us")
MODERN_UA = os.environ.get(
    "YTPDL_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# =========================
# Shell helpers
# =========================
def _run_argv(argv: List[str], check: bool = True) -> str:
    res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and res.returncode != 0:
        cmd = " ".join(shlex.quote(p) for p in argv)
        raise RuntimeError(f"Command failed: {cmd}\n{res.stdout}")
    return res.stdout

# =========================
# Environment / Mullvad
# =========================
def validate_environment() -> None:
    if not os.path.exists(YTDLP_BIN):
        raise RuntimeError(f"yt-dlp not found at {YTDLP_BIN}")
    if shutil.which(FFMPEG_BIN) is None:
        raise RuntimeError("ffmpeg not found on PATH")

def _mullvad_present() -> bool:
    return shutil.which("mullvad") is not None

def mullvad_logged_in() -> bool:
    if not _mullvad_present():
        return False
    res = subprocess.run(
        ["mullvad", "account", "get"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return "not logged in" not in (res.stdout or "").lower()

def require_mullvad_login() -> None:
    if _mullvad_present() and not mullvad_logged_in():
        raise RuntimeError("Mullvad not logged in. Run: mullvad account login <ACCOUNT>")

def mullvad_connect(location: Optional[str] = None) -> None:
    if not _mullvad_present():
        return
    loc = (location or MULLVAD_LOCATION).strip()
    _run_argv(["mullvad", "disconnect"], check=False)
    if loc:
        _run_argv(["mullvad", "relay", "set", "location", loc], check=False)
    _run_argv(["mullvad", "connect"], check=False)

def mullvad_wait_connected(timeout: int = 20) -> bool:
    if not _mullvad_present():
        return True
    for _ in range(timeout):
        res = subprocess.run(
            ["mullvad", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if "Connected" in res.stdout:
            return True
        time.sleep(1)
    return False

# =========================
# yt-dlp helpers
# =========================
def _extract_downloaded_filename(stdout: str) -> Optional[str]:
    for line in (stdout or "").splitlines():
        if "[download] Destination:" in line:
            return line.split("Destination:", 1)[1].strip()
        if "] " in line and " has already been downloaded" in line:
            return (
                line.split("] ", 1)[1]
                .split(" has already been downloaded")[0]
                .strip()
                .strip("'\"")
            )
    return None

def _common_flags() -> List[str]:
    return [
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "exp=1:30",
        "--user-agent", MODERN_UA,
        "--no-cache-dir",
        "--ignore-config",
        "--embed-metadata",
        "--sleep-interval", "1",
    ]

def _download_with_format(url: str, out_tpl: str, fmt: str) -> str:
    argv = [YTDLP_BIN, "-f", fmt, *(_common_flags()), "--output", out_tpl, url]
    out = _run_argv(argv, check=False)
    path = _extract_downloaded_filename(out)
    if not path or not os.path.exists(path):
        raise RuntimeError(f"Failed to download format: {fmt}")
    return os.path.abspath(path)

# =========================
# Main download logic
# =========================
def _download_best_video(url: str, out_dir: str, cap: int = 1080) -> str:
    out_tpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    # 1. Exact 1080p H.264 + AAC in MP4 (perfect, no remux)
    try:
        return _download_with_format(
            url,
            out_tpl,
            "bv*[height=1080][vcodec~='^(avc1|h264)'][ext=mp4]"
            "+ba[acodec~='^mp4a'][ext=m4a]"
            "/b[height=1080][vcodec~='^(avc1|h264)'][ext=mp4]",
        )
    except Exception:
        pass

    # 2. Exact 1080p any codec/container
    try:
        return _download_with_format(url, out_tpl, "bv*[height=1080]+ba/b[height=1080]")
    except Exception:
        pass

    # 3. Best available <= cap
    return _download_with_format(
        url, out_tpl, f"bv*[height<={cap}]+ba/b[height<={cap}]"
    )

def _download_best_audio(url: str, out_dir: str) -> str:
    out_tpl = os.path.join(out_dir, "%(title)s.audio.%(ext)s")
    return _download_with_format(url, out_tpl, "bestaudio")

def _merge_av(video_path: str, audio_path: str) -> str:
    base, ext = os.path.splitext(video_path)
    temp = base + ".merged" + ext
    argv = [
        FFMPEG_BIN,
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-c",
        "copy",
        "-map",
        "0:v",
        "-map",
        "1:a",
        temp,
    ]
    _run_argv(argv, check=True)
    os.replace(temp, video_path)
    try:
        os.remove(audio_path)
    except Exception:
        pass
    return video_path

# =========================
# Public API
# =========================
def download_video(
    url: str,
    resolution: int | None = 1080,
    extension: Optional[str] = None,
    out_dir: str = "/root",
) -> str:
    if not url:
        raise RuntimeError("Missing URL")
    os.makedirs(out_dir, exist_ok=True)

    validate_environment()
    require_mullvad_login()
    mullvad_connect(MULLVAD_LOCATION)
    if not mullvad_wait_connected():
        raise RuntimeError("Mullvad connection failed")

    try:
        cap = int(resolution or 1080)

        if extension and extension.lower() == "mp3":
            out_tpl = os.path.join(out_dir, "%(title)s.%(ext)s")
            argv = [
                YTDLP_BIN,
                "-x",
                "--audio-format",
                "mp3",
                *(_common_flags()),
                "--output",
                out_tpl,
                url,
            ]
            out = _run_argv(argv, check=False)
            path = _extract_downloaded_filename(out)
            if not path or not os.path.exists(path):
                raise RuntimeError("MP3 download failed")
            return os.path.abspath(path)

        # Video + audio (merged)
        video_path = _download_best_video(url, out_dir, cap)
        audio_path = _download_best_audio(url, out_dir)
        return _merge_av(video_path, audio_path)

    finally:
        if _mullvad_present():
            _run_argv(["mullvad", "disconnect"], check=False)
