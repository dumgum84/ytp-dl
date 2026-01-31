#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from typing import Optional, List, Tuple

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
    "Chrome/124.0.0.0 Safari/537.36",
)

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
DEFAULT_OUT_DIR = os.environ.get("YTPDL_DOWNLOAD_DIR", "/root")

# Keep error payloads readable (your web UI prints these).
_MAX_ERR_LINES = 80
_MAX_ERR_CHARS = 4000


# =========================
# Shell helpers
# =========================
def _run_argv_capture(argv: List[str]) -> Tuple[int, str]:
    res = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return res.returncode, (res.stdout or "")


def _run_argv(argv: List[str], check: bool = True) -> str:
    rc, out = _run_argv_capture(argv)
    if check and rc != 0:
        cmd = " ".join(shlex.quote(p) for p in argv)
        raise RuntimeError(f"Command failed: {cmd}\n{out}")
    return out


def _tail(out: str) -> str:
    lines = (out or "").splitlines()
    tail_lines = lines[-_MAX_ERR_LINES:]
    txt = "\n".join(tail_lines)
    if len(txt) > _MAX_ERR_CHARS:
        txt = txt[-_MAX_ERR_CHARS:]
    return txt.strip()


def _is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


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
        if "Connected" in (res.stdout or ""):
            return True
        time.sleep(1)
    return False


# =========================
# yt-dlp helpers
# =========================
def _common_flags() -> List[str]:
    # --no-playlist prevents accidental channel/playlist pulls (and disk blowups)
    return [
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "exp=1:30",
        "--user-agent", MODERN_UA,
        "--no-cache-dir",
        "--ignore-config",
        "--embed-metadata",
        "--sleep-interval", "1",
    ]


def _extract_final_path(stdout: str, out_dir: str) -> Optional[str]:
    """
    Robustly derive the final output file path from yt-dlp output.

    Priority:
      1) --print after_move:filepath lines (absolute paths)
      2) [Merger] Merging formats into "..."
      3) Any Destination: lines that still exist
      4) Newest non-temp file in out_dir
    """
    candidates: List[str] = []
    out_dir = os.path.abspath(out_dir)

    for raw in (stdout or "").splitlines():
        line = (raw or "").strip()
        if not line:
            continue

        # 1) --print after_move:filepath (usually an absolute path)
        if os.path.isabs(line) and line.startswith(out_dir):
            candidates.append(line.strip("'\""))
            continue

        # 2) Merger line: ... into "path"
        if "Merging formats into" in line and "\"" in line:
            try:
                merged = line.split("Merging formats into", 1)[1].strip()
                if merged.startswith("\"") and merged.endswith("\""):
                    merged = merged[1:-1]
                else:
                    if merged.startswith("\""):
                        merged = merged.split("\"", 2)[1]
                if merged:
                    if not os.path.isabs(merged):
                        merged = os.path.join(out_dir, merged)
                    candidates.append(merged.strip("'\""))
            except Exception:
                pass
            continue

        # 3) Destination lines (download/extractaudio)
        if "Destination:" in line:
            try:
                p = line.split("Destination:", 1)[1].strip().strip("'\"")
                if p and not os.path.isabs(p):
                    p = os.path.join(out_dir, p)
                if p:
                    candidates.append(p)
            except Exception:
                pass
            continue

        # already downloaded
        if "] " in line and " has already been downloaded" in line:
            try:
                p = (
                    line.split("] ", 1)[1]
                    .split(" has already been downloaded", 1)[0]
                    .strip()
                    .strip("'\"")
                )
                if p and not os.path.isabs(p):
                    p = os.path.join(out_dir, p)
                if p:
                    candidates.append(p)
            except Exception:
                pass

    # Prefer existing, newest candidate (reverse traversal)
    for p in reversed(candidates):
        if p and os.path.exists(p):
            return p

    # 4) Fallback: newest non-temp file in out_dir
    try:
        best_path = None
        best_mtime = -1.0
        for name in os.listdir(out_dir):
            if name.endswith((".part", ".ytdl", ".tmp")):
                continue
            full = os.path.join(out_dir, name)
            if not os.path.isfile(full):
                continue
            mt = os.path.getmtime(full)
            if mt > best_mtime:
                best_mtime = mt
                best_path = full
        if best_path:
            return best_path
    except Exception:
        pass

    return None


def _download_with_format(
    url: str,
    out_dir: str,
    fmt: str,
    merge_output_format: Optional[str] = None,
    extract_mp3: bool = False,
) -> str:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    out_tpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    argv = [
        YTDLP_BIN,
        "-f", fmt,
        *(_common_flags()),
        "--output", out_tpl,
        # Ensure we can reliably pick the final output path.
        "--print", "after_move:filepath",
    ]

    if extract_mp3:
        # Force audio extraction to MP3 (requires ffmpeg)
        argv.extend(["--extract-audio", "--audio-format", "mp3"])

    # Only force merge container when we actually want MP4 output.
    if merge_output_format:
        argv.extend(["--merge-output-format", merge_output_format])

    argv.append(url)

    rc, out = _run_argv_capture(argv)
    path = _extract_final_path(out, out_dir)

    if path and os.path.exists(path):
        return os.path.abspath(path)

    tail = _tail(out)
    if rc != 0:
        raise RuntimeError(f"yt-dlp failed (format: {fmt})\n{tail}")
    raise RuntimeError(f"Download completed but output file not found (format: {fmt})\n{tail}")


def _fmt_mp4_apple_safe(cap: int) -> str:
    # Always pick the best Apple-safe MP4/H.264 + M4A/AAC up to cap.
    return (
        f"bv*[height<={cap}][ext=mp4][vcodec~='^(avc1|h264)']"
        f"+ba[ext=m4a][acodec~='^mp4a']"
        f"/b[height<={cap}][ext=mp4][vcodec~='^(avc1|h264)'][acodec~='^mp4a']"
    )


def _fmt_best(cap: int) -> str:
    # Best overall up to cap (can yield webm/mkv/etc).
    return f"bv*[height<={cap}]+ba/b[height<={cap}]"


# =========================
# Public API
# =========================
def download_video(
    url: str,
    resolution: int | None = 1080,
    extension: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
) -> str:
    if not url:
        raise RuntimeError("Missing URL")

    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    validate_environment()

    require_mullvad_login()
    mullvad_connect(MULLVAD_LOCATION)
    if not mullvad_wait_connected():
        raise RuntimeError("Mullvad connection failed")

    try:
        mode = (extension or "mp4").lower().strip()

        if mode == "mp3":
            # bestaudio -> ffmpeg -> mp3 (post-processed by yt-dlp)
            return _download_with_format(
                url=url,
                out_dir=out_dir,
                fmt="bestaudio",
                merge_output_format=None,
                extract_mp3=True,
            )

        cap = int(resolution or 1080)

        if mode == "best":
            # Try best first (may produce webm/mkv/etc).
            try:
                return _download_with_format(
                    url=url,
                    out_dir=out_dir,
                    fmt=_fmt_best(cap),
                    merge_output_format=None,
                    extract_mp3=False,
                )
            except Exception:
                # If best fails for any reason, fall back to Apple-safe MP4.
                return _download_with_format(
                    url=url,
                    out_dir=out_dir,
                    fmt=_fmt_mp4_apple_safe(cap),
                    merge_output_format="mp4",
                    extract_mp3=False,
                )

        # Default / "mp4" mode: force Apple-safe MP4 up to cap.
        return _download_with_format(
            url=url,
            out_dir=out_dir,
            fmt=_fmt_mp4_apple_safe(cap),
            merge_output_format="mp4",
            extract_mp3=False,
        )

    finally:
        if _mullvad_present():
            _run_argv(["mullvad", "disconnect"], check=False)
