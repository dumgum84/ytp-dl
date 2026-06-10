#!/usr/bin/env python3
# downloader.py (VPS) - playlist support, MP3 cover art/metadata, hard kill timeout

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import signal
import threading
import zipfile
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

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
    "Chrome/148.0.0.0 Safari/537.36",
)

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_TIMEOUT_S = int(os.environ.get("YTPDL_FFMPEG_TIMEOUT_S", "1800"))
DEFAULT_OUT_DIR = os.environ.get("YTPDL_DOWNLOAD_DIR", "/root")

JOB_TIMEOUT_S = int(os.environ.get("YTPDL_JOB_TIMEOUT_S", "1800"))
PLAYLIST_JOB_TIMEOUT_S = int(os.environ.get("YTPDL_PLAYLIST_JOB_TIMEOUT_S", "21600"))

_MAX_ERR_LINES = 80
_MAX_ERR_CHARS = 4000

# Matches YouTube rate-limit and bot-detection errors that mean the current
# Mullvad IP is blocked and won't recover without a VPN cycle.
_BOT_RX = re.compile(
    r"sign in to confirm you.re not a bot"
    r"|HTTP Error 429"
    r"|Too Many Requests",
    re.IGNORECASE,
)

# PornHub-specific IP-flagging patterns that should trigger Mullvad rotation.
# Scoped separately to avoid spurious rotations on other sites where 403/410
# can mean deleted content or auth required.
_PH_BOT_RX = re.compile(
    r"\[PornHub\].*(?:HTTP Error 410|HTTP Error 403|Unable to extract title)",
    re.IGNORECASE,
)


# SoundCloud is audio-only — used by download_multi_url for per-URL
# format forcing without relying on the Render-side SC override.
_SC_URL_RE = re.compile(r"soundcloud\.com", re.IGNORECASE)

# PornHub blocks non-browser TLS fingerprints (HTTP 410/403).
# --impersonate chrome (via curl_cffi) works around this.
_PH_URL_RE = re.compile(r"pornhub\.com", re.IGNORECASE)
try:
    import curl_cffi as _curl_cffi  # noqa: F401
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

# =========================
# Mullvad state (module-level, shared across all threads)
# =========================
_mullvad_lock = threading.Lock()
_mullvad_connected = False


def _mullvad_is_actually_connected() -> bool:
    """Check live Mullvad status — guards against external disconnects."""
    if not _mullvad_present():
        return True
    try:
        res = subprocess.run(
            ["mullvad", "status"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5,
        )
        return "Connected" in (res.stdout or "")
    except Exception:
        return False


def _ensure_mullvad() -> None:
    """Connect to Mullvad only if not already connected. Thread-safe.
    Re-validates live status so external disconnects (reboot, daemon restart)
    are caught rather than silently using an unprotected connection."""
    global _mullvad_connected
    if _mullvad_connected and _mullvad_is_actually_connected():
        return
    with _mullvad_lock:
        if _mullvad_connected and _mullvad_is_actually_connected():
            return
        require_mullvad_login()
        mullvad_connect(MULLVAD_LOCATION)
        if not mullvad_wait_connected():
            raise RuntimeError("Mullvad connection failed")
        _mullvad_connected = True


def _rotate_mullvad() -> None:
    """Rotate Mullvad IP (disconnect → connect). Called only on bot detection. Thread-safe."""
    global _mullvad_connected
    with _mullvad_lock:
        mullvad_connect(MULLVAD_LOCATION)
        if not mullvad_wait_connected():
            raise RuntimeError("Mullvad reconnection failed")
        _mullvad_connected = True


# =========================
# Shell helpers
# =========================
def _run_argv_capture(argv: List[str]) -> Tuple[int, str]:
    res = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=FFMPEG_TIMEOUT_S,
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
    txt = "\n".join(lines[-_MAX_ERR_LINES:])
    if len(txt) > _MAX_ERR_CHARS:
        txt = txt[-_MAX_ERR_CHARS:]
    return txt.strip()


# =========================
# Playlist detection
# =========================
_YT_PLAYLIST_RE = re.compile(r"[?&]list=(?!RD|RDMM|FL|LL|WL)", re.IGNORECASE)
_SC_SET_RE      = re.compile(r"soundcloud\.com/[^/?#]+/sets/", re.IGNORECASE)
# Bilibili: /list/UID?sid= (series), /medialist/play|detail/ (favorites),
#            space.bilibili.com/UID/lists/ or /favlist (space playlists),
#            /video/BVxxx?p= (multi-part — any ?p= on a video page)
_BILI_RE        = re.compile(
    r"bilibili\.com/(?:medialist/(?:play|detail)/\w|list/\d|video/[^?#]+[?][^#]*p=\d)"
    r"|space\.bilibili\.com/\d+/(?:lists|favlist)",
    re.IGNORECASE,
)
# Odysee: /$/playlist/ (main share format) and /$/list/ (older format, confirmed real)
_ODYSEE_RE      = re.compile(r"odysee\.com/\$/(playlist|list)/", re.IGNORECASE)


def is_playlist_url(url: str) -> bool:
    """Return True when url is a playlist/set/collection on a supported site."""
    url = (url or "").strip()
    return bool(
        _YT_PLAYLIST_RE.search(url)
        or _SC_SET_RE.search(url)
        or _BILI_RE.search(url)
        or _ODYSEE_RE.search(url)
    )


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
        ["mullvad", "account", "get"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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
            ["mullvad", "status"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        if "Connected" in (res.stdout or ""):
            return True
        time.sleep(1)
    return False


# =========================
# yt-dlp flags
# =========================
def _common_flags(*, playlist: bool = False) -> List[str]:
    """
    Base yt-dlp flags shared by all download modes.

    --embed-thumbnail and --convert-thumbnails are intentionally omitted here.
    They are added only for MP3 extraction (in _build_ytdlp_argv) because
    video containers like webm will be silently remuxed to mkv by yt-dlp when
    thumbnail embedding is requested — an undesirable format change.
    --embed-metadata is safe for all containers and covers title/artist/date.
    """
    flags = [
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "10",
        "--retry-sleep", "exp=1:30",
        "--user-agent", MODERN_UA,
        "--no-cache-dir",
        "--ignore-config",
        "--embed-metadata",
        "--sleep-interval", "1",
    ]
    flags.append("--yes-playlist" if playlist else "--no-playlist")
    return flags


# =========================
# Format selectors
# =========================
def _fmt_mp4_apple_safe(cap: int) -> str:
    # Fallback chain so sites like TikTok that lack strict h264+m4a streams
    # still work under mp4 mode:
    #   1. Strict h264+m4a  (Apple-safe, YouTube default)
    #   2. Any mp4 video + any m4a audio
    #   3. Any single-file mp4 stream
    #   4. Anything available — remuxed to mp4 by --merge-output-format
    return (
        f"bv*[height<={cap}][ext=mp4][vcodec~='^(avc1|h264)']"
        f"+ba[ext=m4a][acodec~='^mp4a']"
        f"/bv*[height<={cap}][ext=mp4]+ba[ext=m4a]"
        f"/b[height<={cap}][ext=mp4]"
        f"/b[height<={cap}]"
    )


def _fmt_best(cap: int) -> str:
    return f"bv*[height<={cap}]+ba/b[height<={cap}]"


# =========================
# Playlist item count helper
# =========================
def _get_url_item_count(url: str) -> int:
    """
    Quickly determine how many downloadable items a URL represents.
    Returns 1 for single videos, N for playlists/sets.
    Uses --flat-playlist with --playlist-items 1 to read playlist_count
    from the first entry's metadata — no media is downloaded.
    Falls back to 1 on any error so downloading always proceeds.
    """
    if not is_playlist_url(url):
        return 1
    try:
        result = subprocess.run(
            [
                YTDLP_BIN,
                "--flat-playlist",
                "--quiet",
                "--print", "%(playlist_count)s",
                "--playlist-items", "1",
                "--yes-playlist",
                "--no-cache-dir",
                "--ignore-config",
                "--retries", "2",
                "--user-agent", MODERN_UA,
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        for line in (result.stdout or "").strip().splitlines():
            line = line.strip()
            if line and line != "NA" and line.isdigit():
                n = int(line)
                if n > 0:
                    return n
    except Exception:
        pass
    return 1  # safe fallback


# =========================
# Path extraction helpers
# =========================
def _extract_final_path_from_tail(stdout: str, out_dir: str) -> Optional[str]:
    candidates: List[str] = []
    out_dir = os.path.abspath(out_dir)

    for raw in (stdout or "").splitlines():
        line = (raw or "").strip()
        if not line:
            continue
        if os.path.isabs(line) and line.startswith(out_dir):
            candidates.append(line.strip("'\""))
            continue
        if "Merging formats into" in line and "\"" in line:
            try:
                merged = line.split("Merging formats into", 1)[1].strip()
                if merged.startswith("\"") and merged.endswith("\""):
                    merged = merged[1:-1]
                elif merged.startswith("\""):
                    merged = merged.split("\"", 2)[1]
                if merged:
                    if not os.path.isabs(merged):
                        merged = os.path.join(out_dir, merged)
                    candidates.append(merged.strip("'\""))
            except Exception:
                pass
            continue
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

    for p in reversed(candidates):
        if p and os.path.exists(p):
            return os.path.abspath(p)

    try:
        best_path, best_mtime = None, -1.0
        for name in os.listdir(out_dir):
            if name.endswith((".part", ".ytdl", ".tmp", ".zip")):
                continue
            full = os.path.join(out_dir, name)
            if not os.path.isfile(full):
                continue
            mt = os.path.getmtime(full)
            if mt > best_mtime:
                best_mtime, best_path = mt, full
        if best_path:
            return os.path.abspath(best_path)
    except Exception:
        pass

    return None


# =========================
# argv builder
# =========================
def _build_ytdlp_argv(
    *,
    url: str,
    out_dir: str,
    fmt: str,
    merge_output_format: Optional[str],
    extract_mp3: bool,
    playlist: bool = False,
    archive_path: Optional[str] = None,
) -> List[str]:
    out_dir = os.path.abspath(out_dir)
    out_tpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    argv = [
        YTDLP_BIN,
        "-f", fmt,
        *(_common_flags(playlist=playlist)),
        "--output", out_tpl,
        "--print", "after_move:filepath",
        "--progress",
        "--newline",
        "--no-color",
    ]

    # For playlists, print the title once so download_playlist can name files.
    if playlist:
        argv.extend(["--print", "before_dl:[playlist_title] %(playlist_title)s"])
        # Emit total track count once per item so the frontend can divide the bar.
        argv.extend(["--print", "before_dl:[playlist_count] %(playlist_count)s"])
        # Prevent titles with "/" from creating subdirectories.
        argv.append("--windows-filenames")
        # Never auto-concat multi-part entries — we ship individual tracks.
        argv.extend(["--concat-playlist", "never"])
        # Continue if one entry in the playlist fails.
        argv.append("--ignore-errors")
        # Track completed video IDs so retries skip already-downloaded tracks.
        if archive_path:
            argv.extend(["--download-archive", archive_path])

    if extract_mp3:
        # MP3 only: safe to embed cover art — yt-dlp writes it as an ID3 APIC
        # tag without touching the container format.
        argv.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",           # cover art in ID3
            "--convert-thumbnails", "jpg", # webp → jpg before embedding
        ])
    else:
        # Video: no --embed-thumbnail to avoid unwanted container changes.
        if merge_output_format:
            argv.extend(["--merge-output-format", merge_output_format])

    # PornHub blocks non-browser TLS fingerprints (HTTP 410/403 errors).
    # Requires curl_cffi: included via yt-dlp[default,curl-cffi] in requirements.txt
    if _PH_URL_RE.search(url) and _CURL_CFFI_AVAILABLE:
        argv.extend(["--impersonate", "chrome"])

    argv.append(url)
    return argv


# =========================
# Core streaming downloader
# =========================
def _download_with_format_stream(
    *,
    url: str,
    out_dir: str,
    fmt: str,
    merge_output_format: Optional[str],
    extract_mp3: bool,
    on_line: Callable[[str], None],
    playlist: bool = False,
    abort_event: Optional[threading.Event] = None,
    archive_path: Optional[str] = None,
) -> "str | List[str]":
    """
    Streams yt-dlp stdout line-by-line via on_line.

    Returns:
      str       — path to single downloaded file   (playlist=False)
      List[str] — ordered paths for all tracks     (playlist=True)
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    argv = _build_ytdlp_argv(
        url=url, out_dir=out_dir, fmt=fmt,
        merge_output_format=merge_output_format,
        extract_mp3=extract_mp3, playlist=playlist,
        archive_path=archive_path,
    )

    tail_lines: Deque[str] = deque(maxlen=_MAX_ERR_LINES)
    candidates: List[str] = []

    def _maybe_add_candidate(p: str) -> None:
        p = (p or "").strip().strip("'\"")
        if not p:
            return
        if not os.path.isabs(p):
            p = os.path.join(out_dir, p)
        if p not in candidates:
            candidates.append(p)

    timeout_s = PLAYLIST_JOB_TIMEOUT_S if playlist else JOB_TIMEOUT_S
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )
    assert proc.stdout is not None

    stop_killer = threading.Event()

    def _killer():
        if timeout_s <= 0:
            return
        if stop_killer.wait(timeout_s):
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    threading.Thread(target=_killer, daemon=True).start()

    try:
        for line in proc.stdout:
            s = (line or "").rstrip("\n")
            if not s:
                continue
            on_line(s)
            tail_lines.append(s)

            # If abort was signaled (e.g. rate-limited), kill yt-dlp immediately
            # rather than waiting for it to exhaust retries on every remaining track.
            if abort_event and abort_event.is_set():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break

            # Bot detection — rotate IP immediately so Render's retry gets a fresh IP.
            if _BOT_RX.search(s) or _PH_BOT_RX.search(s):
                if abort_event is not None:
                    if not abort_event.is_set():
                        abort_event.set()
                        _rotate_mullvad()
                else:
                    # Single video path — rotate and let yt-dlp fail naturally.
                    _rotate_mullvad()

            if os.path.isabs(s) and s.startswith(out_dir):
                _maybe_add_candidate(s)
                continue
            if "Merging formats into" in s and "\"" in s:
                try:
                    merged = s.split("Merging formats into", 1)[1].strip()
                    if merged.startswith("\"") and merged.endswith("\""):
                        merged = merged[1:-1]
                    elif merged.startswith("\""):
                        merged = merged.split("\"", 2)[1]
                    _maybe_add_candidate(merged)
                except Exception:
                    pass
            if "Destination:" in s:
                try:
                    _maybe_add_candidate(s.split("Destination:", 1)[1].strip())
                except Exception:
                    pass

        rc = proc.wait()
    finally:
        stop_killer.set()
        try:
            proc.stdout.close()
        except Exception:
            pass

    # ---- Playlist mode ----
    if playlist:
        # Scan the entire output directory so we capture files downloaded in
        # PREVIOUS retry runs too.  Those were recorded in --download-archive
        # and silently skipped by yt-dlp this run, so they never appeared in
        # `candidates` — meaning a naive candidates-only approach would omit
        # them from the ZIP on every retry.
        _SKIP_EXTS = (".part", ".ytdl", ".tmp", ".zip", ".json", ".txt", ".filelist.txt")
        try:
            dir_entries = []
            for n in os.listdir(out_dir):
                if n.startswith(".") or any(n.endswith(e) for e in _SKIP_EXTS):
                    continue
                full = os.path.join(out_dir, n)
                if os.path.isfile(full):
                    dir_entries.append((os.path.getmtime(full), full))
            # Sort by mtime — preserves download order across retry runs.
            dir_entries.sort(key=lambda x: x[0])
            found = [p for _, p in dir_entries]
        except Exception:
            # Fallback: use only what this run printed
            found = [p for p in candidates if p and os.path.exists(p)]

        if not found and rc != 0:
            raise RuntimeError(
                f"yt-dlp failed (playlist, format: {fmt})\n{_tail('\n'.join(tail_lines))}"
            )
        return found

    # ---- Single file mode ----
    for p in reversed(candidates):
        if p and os.path.exists(p):
            return os.path.abspath(p)

    tail_txt = "\n".join(tail_lines)
    final_path = _extract_final_path_from_tail(tail_txt, out_dir)
    if final_path and os.path.exists(final_path):
        return os.path.abspath(final_path)

    if rc != 0:
        raise RuntimeError(f"yt-dlp failed (format: {fmt})\n{_tail(tail_txt)}")
    raise RuntimeError(f"Download completed but output file not found (format: {fmt})\n{_tail(tail_txt)}")


# =========================
# Filename helpers
# =========================
def _sanitize_filename_stem(title: str) -> str:
    """Make a playlist title into a safe filename stem."""
    if not title or title.upper() == "NA":
        return "playlist"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return safe[:120] or "playlist"


# =========================
# ZIP helper
# =========================
def _create_zip(files: List[str], zip_path: str) -> str:
    """Bundle files into a ZIP. ZIP_STORED avoids wasting CPU on already-compressed media."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for path in files:
            if os.path.isfile(path):
                zf.write(path, os.path.basename(path))
    return zip_path


# =========================
# Playlist downloader
# =========================
def download_playlist(
    *,
    url: str,
    resolution: int | None = 1080,
    extension: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
    on_line: Callable[[str], None],
) -> str:
    """
    Download every track in a playlist.

    Returns the path to a ZIP of the individual tracks — the job's primary
    result. The worker uploads each track and the ZIP to R2.

    On 429/bot-detection, kills yt-dlp immediately and raises RuntimeError
    so Render's existing retry loop fires and cycles the Mullvad IP.
    A --download-archive file in out_dir ensures already-downloaded tracks
    are skipped on retry — no duplicate downloads.
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    validate_environment()
    _ensure_mullvad()

    # Emit total item count before downloading so the frontend can scale
    # the progress bar correctly from the very first track.
    _item_count = _get_url_item_count(url)
    on_line(f"[total_items] {_item_count}")

    mode = (extension or "mp3").lower().strip()
    cap = int(resolution or 1080)

    # Persists across Render retries (same job_dir) so re-runs skip completed tracks.
    archive_path = os.path.join(out_dir, ".ytdlp-archive")

    # Signals yt-dlp to be killed immediately when a rate-limit is detected.
    abort_event = threading.Event()

    _playlist_title: list = []
    _expected_count: list = []
    _orig_on_line = on_line

    def _capturing_on_line(line: str) -> None:
        if line.startswith("[playlist_title] ") and not _playlist_title:
            _playlist_title.append(line[len("[playlist_title] "):].strip())
        if line.startswith("[playlist_count] ") and not _expected_count:
            try:
                _expected_count.append(int(line.split(None, 1)[1].strip()))
            except Exception:
                pass
        if (_BOT_RX.search(line) or _PH_BOT_RX.search(line)) and not abort_event.is_set():
            abort_event.set()
            _rotate_mullvad()
            _orig_on_line("[info] Rate limited — rotating VPN IP and retrying")
        _orig_on_line(line)

    on_line = _capturing_on_line

    try:
        if mode == "mp3":
            files = _download_with_format_stream(
                url=url, out_dir=out_dir, fmt="bestaudio/best",
                merge_output_format=None, extract_mp3=True,
                on_line=on_line, playlist=True,
                abort_event=abort_event, archive_path=archive_path,
            )
        elif mode == "best":
            try:
                files = _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                    merge_output_format=None, extract_mp3=False,
                    on_line=on_line, playlist=True,
                    abort_event=abort_event, archive_path=archive_path,
                )
            except Exception:
                try:
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                        merge_output_format="mp4", extract_mp3=False,
                        on_line=on_line, playlist=True,
                        abort_event=abort_event, archive_path=archive_path,
                    )
                except Exception:
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                        merge_output_format="mp4", extract_mp3=False,
                        on_line=on_line, playlist=True,
                        abort_event=abort_event, archive_path=archive_path,
                    )
        else:  # mp4
            try:
                files = _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                    merge_output_format="mp4", extract_mp3=False,
                    on_line=on_line, playlist=True,
                    abort_event=abort_event, archive_path=archive_path,
                )
            except Exception:
                files = _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                    merge_output_format="mp4", extract_mp3=False,
                    on_line=on_line, playlist=True,
                    abort_event=abort_event, archive_path=archive_path,
                )

        # If the IP was rate-limited, raise so Render's retry loop fires and
        # cycles Mullvad. The archive file means the retry picks up where we left off.
        if abort_event.is_set():
            raise RuntimeError(
                "Rate limited by YouTube — retrying with fresh VPN IP. "
                "Already-downloaded tracks will be skipped on retry."
            )

        if not files:
            raise RuntimeError("No tracks could be downloaded from this playlist.")

        # ---- Playlist fill-in passes ---------------------------------------
        # --ignore-errors causes yt-dlp to silently skip a track once its
        # internal retries are exhausted, so the job exits cleanly with fewer
        # files than expected.  We fix this by re-running yt-dlp on the same
        # playlist URL up to (YTPDL_PLAYLIST_PASSES - 1) extra times.  The
        # --download-archive file records every completed track ID, so each
        # re-run skips already-downloaded tracks instantly and only attempts
        # the ones that failed.  We stop early when either:
        #   (a) file count matches the expected playlist count, or
        #   (b) a pass produced no new files — remaining tracks are
        #       permanently unavailable (private, deleted, geo-blocked).
        # Kept local to the VPS — Render's retry system handles connection-
        # level failures (rate limits, VPS errors); this handles per-track
        # transient failures within an otherwise healthy job.
        _passes = int(os.environ.get("YTPDL_PLAYLIST_PASSES", "5"))
        _expected = _expected_count[0] if _expected_count else None

        for _pass in range(1, _passes):
            if _expected is not None and len(files) >= _expected:
                break
            prev_count = len(files)
            missing = (_expected - prev_count) if _expected else "some"
            _orig_on_line(f"[info] {missing} track(s) missing — playlist pass {_pass + 1}/{_passes}")
            fill_abort = threading.Event()
            try:
                if mode == "mp3":
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt="bestaudio/best",
                        merge_output_format=None, extract_mp3=True,
                        on_line=on_line, playlist=True,
                        abort_event=fill_abort, archive_path=archive_path,
                    )
                elif mode == "best":
                    try:
                        files = _download_with_format_stream(
                            url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                            merge_output_format=None, extract_mp3=False,
                            on_line=on_line, playlist=True,
                            abort_event=fill_abort, archive_path=archive_path,
                        )
                    except Exception:
                        try:
                            files = _download_with_format_stream(
                                url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                                merge_output_format="mp4", extract_mp3=False,
                                on_line=on_line, playlist=True,
                                abort_event=fill_abort, archive_path=archive_path,
                            )
                        except Exception:
                            files = _download_with_format_stream(
                                url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                                merge_output_format="mp4", extract_mp3=False,
                                on_line=on_line, playlist=True,
                                abort_event=fill_abort, archive_path=archive_path,
                            )
                else:
                    try:
                        files = _download_with_format_stream(
                            url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                            merge_output_format="mp4", extract_mp3=False,
                            on_line=on_line, playlist=True,
                            abort_event=fill_abort, archive_path=archive_path,
                        )
                    except Exception:
                        files = _download_with_format_stream(
                            url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                            merge_output_format="mp4", extract_mp3=False,
                            on_line=on_line, playlist=True,
                            abort_event=fill_abort, archive_path=archive_path,
                        )
            except RuntimeError:
                raise  # propagate rate-limit kills to Render's retry system
            except Exception as e:
                _orig_on_line(f"[info] Playlist pass {_pass + 1} error: {e} — keeping {len(files)} tracks")
                break
            if fill_abort.is_set():
                raise RuntimeError(
                    "Rate limited during playlist pass — retrying with fresh VPN IP. "
                    "Already-downloaded tracks will be skipped on retry."
                )
            if len(files) <= prev_count:
                _orig_on_line("[info] No new tracks recovered — remaining tracks are likely unavailable")
                break
            _orig_on_line(f"[info] Playlist pass {_pass + 1}: recovered {len(files) - prev_count} track(s)")
        # ---- End playlist passes -------------------------------------------

        title = _playlist_title[0] if _playlist_title else None
        stem = _sanitize_filename_stem(title)

        # ZIP of individual tracks — returned as primary result.
        # The worker uploads each track to R2 (sequential playback) and the
        # ZIP (download button); Render just records the announced keys.
        zip_path = os.path.join(out_dir, f"{stem}.zip")
        _create_zip(files, zip_path)

        return zip_path

    finally:
        pass  # VPN stays connected — never disconnect mid-service

# =========================
# Multi-URL downloader
# =========================
def download_multi_url(
    *,
    urls: List[str],
    resolution: "int | None" = 1080,
    extension: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
    on_line: Callable[[str], None],
) -> str:
    """
    Download a comma-separated list of URLs into a single ZIP (multi_url.zip).

    Each URL downloads into its own subdirectory to prevent filename
    collisions. Playlist URLs are expanded with the same fill-in passes as
    download_playlist. All collected files are ZIPped as multi_url.zip,
    which is returned as the job's primary result.
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    validate_environment()
    _ensure_mullvad()

    # Emit total item count across all URLs before any downloading starts
    # so the frontend progress bar is scaled correctly from the beginning.
    _total_items = sum(_get_url_item_count(u) for u in urls)
    on_line(f"[total_items] {_total_items}")

    mode = (extension or "mp4").lower().strip()
    cap = int(resolution or 1080)
    _passes = int(os.environ.get("YTPDL_PLAYLIST_PASSES", "5"))

    # Shared rate-limit abort — any URL hitting 429/bot-detection aborts all.
    shared_abort = threading.Event()
    all_files: List[str] = []

    def _on_line_intercepted(line: str) -> None:
        if (_BOT_RX.search(line) or _PH_BOT_RX.search(line)) and not shared_abort.is_set():
            shared_abort.set()
            _rotate_mullvad()
            on_line("[info] Rate limited — rotating VPN IP and retrying")
        on_line(line)

    def _run(url: str, url_dir: str, is_pl: bool,
             fmt: str, merge_fmt: Optional[str], mp3: bool,
             ab: threading.Event) -> List[str]:
        archive = os.path.join(url_dir, ".ytdlp-archive") if is_pl else None
        result = _download_with_format_stream(
            url=url, out_dir=url_dir, fmt=fmt,
            merge_output_format=merge_fmt, extract_mp3=mp3,
            on_line=_on_line_intercepted, playlist=is_pl,
            abort_event=ab, archive_path=archive,
        )
        return [result] if isinstance(result, str) else result

    try:
        for i, url in enumerate(urls):
            if shared_abort.is_set():
                raise RuntimeError(
                    "Rate limited — retrying with fresh VPN IP. Re-submit to continue."
                )

            url_dir = os.path.join(out_dir, f"url_{i:03d}")
            os.makedirs(url_dir, exist_ok=True)
            on_line(f"[info] URL {i + 1}/{len(urls)}: {url}")

            is_pl = is_playlist_url(url)
            # Force mp3 for SoundCloud per-URL regardless of user format choice.
            effective_mode = "mp3" if _SC_URL_RE.search(url) else mode

            ua = threading.Event()
            if effective_mode == "mp3":
                files = _run(url, url_dir, is_pl, "bestaudio/best", None, True, ua)
            elif effective_mode == "best":
                try:
                    files = _run(url, url_dir, is_pl, _fmt_best(cap), None, False, ua)
                except Exception:
                    try:
                        files = _run(url, url_dir, is_pl, _fmt_mp4_apple_safe(cap), "mp4", False, ua)
                    except Exception:
                        files = _run(url, url_dir, is_pl, "bestvideo+bestaudio/best", "mp4", False, ua)
            else:
                try:
                    files = _run(url, url_dir, is_pl, _fmt_mp4_apple_safe(cap), "mp4", False, ua)
                except Exception:
                    files = _run(url, url_dir, is_pl, "bestvideo+bestaudio/best", "mp4", False, ua)

            if ua.is_set():
                raise RuntimeError("Rate limited — retrying with fresh VPN IP.")

            # Playlist fill-in passes (mirrors download_playlist logic).
            if is_pl and files:
                for _pass in range(1, _passes):
                    prev = len(files)
                    pa = threading.Event()
                    try:
                        if effective_mode == "mp3":
                            files = _run(url, url_dir, True, "bestaudio/best", None, True, pa)
                        elif effective_mode == "best":
                            try:
                                files = _run(url, url_dir, True, _fmt_best(cap), None, False, pa)
                            except Exception:
                                try:
                                    files = _run(url, url_dir, True, _fmt_mp4_apple_safe(cap), "mp4", False, pa)
                                except Exception:
                                    files = _run(url, url_dir, True, "bestvideo+bestaudio/best", "mp4", False, pa)
                        else:
                            try:
                                files = _run(url, url_dir, True, _fmt_mp4_apple_safe(cap), "mp4", False, pa)
                            except Exception:
                                files = _run(url, url_dir, True, "bestvideo+bestaudio/best", "mp4", False, pa)
                    except RuntimeError:
                        raise
                    except Exception:
                        break
                    if pa.is_set():
                        raise RuntimeError("Rate limited during playlist pass — retrying.")
                    if len(files) <= prev:
                        break

            all_files.extend(files)

        if not all_files:
            raise RuntimeError("No files could be downloaded from the provided URLs.")

        # ZIP all files — returned as primary result.
        # The worker uploads each track to R2 (sequential playback) and the
        # ZIP (download button); Render just records the announced keys.
        # Codec differences between URLs are not an issue.
        zip_path = os.path.join(out_dir, "multi_url.zip")
        _create_zip(all_files, zip_path)

        return zip_path

    finally:
        pass  # VPN stays connected — never disconnect mid-service
# =========================
def download_video(
    *,
    url: str,
    resolution: int | None = 1080,
    extension: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
    on_line: Callable[[str], None],
) -> str:
    """
    Download a single video/audio URL, or a full playlist.
    Always returns a single path (media file or playlist.zip).
    """
    if not url:
        raise RuntimeError("Missing URL")

    # Multi-URL: two or more comma-separated URLs -> download_multi_url.
    if "," in url:
        urls = [u.strip() for u in url.split(",") if u.strip()]
        if len(urls) > 1:
            return download_multi_url(
                urls=urls, resolution=resolution, extension=extension,
                out_dir=out_dir, on_line=on_line,
            )
        url = urls[0]  # single URL with stray trailing comma

    if is_playlist_url(url):
        return download_playlist(
            url=url, resolution=resolution, extension=extension,
            out_dir=out_dir, on_line=on_line,
        )

    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    validate_environment()
    _ensure_mullvad()

    try:
        mode = (extension or "mp4").lower().strip()
        cap = int(resolution or 1080)

        if mode == "mp3":
            return _download_with_format_stream(
                url=url, out_dir=out_dir, fmt="bestaudio/best",
                merge_output_format=None, extract_mp3=True, on_line=on_line,
            )
        if mode == "best":
            try:
                return _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                    merge_output_format=None, extract_mp3=False, on_line=on_line,
                )
            except Exception:
                try:
                    return _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                        merge_output_format="mp4", extract_mp3=False, on_line=on_line,
                    )
                except Exception:
                    return _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                        merge_output_format="mp4", extract_mp3=False, on_line=on_line,
                    )
        try:
            return _download_with_format_stream(
                url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                merge_output_format="mp4", extract_mp3=False, on_line=on_line,
            )
        except Exception:
            return _download_with_format_stream(
                url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                merge_output_format="mp4", extract_mp3=False, on_line=on_line,
            )
    finally:
        pass  # VPN stays connected — never disconnect mid-service
