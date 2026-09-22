#!/usr/bin/env python3
# downloader.py (VPS) - playlist support, MP3 cover art/metadata, hard kill timeout

from __future__ import annotations

import os
import re
import shutil
import subprocess
import signal
import threading
import zipfile
from collections import deque
from typing import Callable, Deque, List, Optional

# =========================
# Config / constants
# =========================
VENV_PATH = os.environ.get("YTPDL_VENV", "/opt/yt-dlp-mullvad/venv")
YTDLP_BIN = os.path.join(VENV_PATH, "bin", "yt-dlp")
VPN_HELPER = os.environ.get("YTPDL_VPN_HELPER", "/usr/local/sbin/ytpdl-vpn")

MODERN_UA = os.environ.get(
    "YTPDL_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36",
)

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
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
    r"\[PornHub\].*(?:HTTP Error 410|HTTP Error 403|Unable to extract title|Redirection detected)",
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
# Isolated Mullvad/WireGuard namespace
# =========================
# The host itself never joins Mullvad.  The installer creates a dedicated
# network namespace and exposes it through YTPDL_VPN_HELPER.  Every yt-dlp
# process runs inside that namespace; Gunicorn, SSH and R2 stay on the host's
# normal network.  The helper uses a host-level flock, so VPN connect/rotate
# operations are serialized across ALL Gunicorn worker processes.
_vpn_thread_lock = threading.Lock()


def _vpn_helper_present() -> bool:
    return os.path.isfile(VPN_HELPER) and os.access(VPN_HELPER, os.X_OK)


def _vpn_command(action: str, *, timeout: int = 45) -> tuple[int, str]:
    try:
        res = subprocess.run(
            [VPN_HELPER, action],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return res.returncode, (res.stdout or "")
    except Exception as e:
        return 1, str(e)


def _vpn_is_connected() -> bool:
    if not _vpn_helper_present():
        return False
    rc, _ = _vpn_command("status", timeout=10)
    return rc == 0


def _ensure_vpn() -> None:
    """Ensure the isolated yt-dlp namespace has a working Mullvad tunnel.

    The Python lock prevents duplicate work from threads in this process; the
    helper itself also takes a filesystem lock so separate Gunicorn workers
    cannot race each other while creating or reconfiguring `ytpdlwg`.
    """
    if _vpn_is_connected():
        return
    with _vpn_thread_lock:
        if _vpn_is_connected():
            return
        rc, out = _vpn_command("ensure", timeout=60)
        if rc != 0:
            raise RuntimeError(f"Mullvad namespace connection failed\n{_tail(out)}")


def _rotate_vpn() -> None:
    """Rotate only the isolated yt-dlp tunnel; host networking is untouched."""
    with _vpn_thread_lock:
        rc, out = _vpn_command("rotate", timeout=60)
        if rc != 0:
            raise RuntimeError(f"Mullvad namespace rotation failed\n{_tail(out)}")


def _vpn_exec_argv(argv: List[str]) -> List[str]:
    """Wrap a command so it executes inside the isolated VPN namespace."""
    return [VPN_HELPER, "exec", "--", *argv]


# =========================
# Shell helpers
# =========================
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
# Environment / VPN namespace
# =========================
def validate_environment() -> None:
    if not os.path.exists(YTDLP_BIN):
        raise RuntimeError(f"yt-dlp not found at {YTDLP_BIN}")
    if shutil.which(FFMPEG_BIN) is None:
        raise RuntimeError("ffmpeg not found on PATH")
    if not _vpn_helper_present():
        raise RuntimeError(
            f"VPN namespace helper not found or not executable at {VPN_HELPER}. "
            "Run the VPS installer first."
        )


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
        "--ignore-config",
        "--embed-metadata",
    ]
    flags.append("--yes-playlist" if playlist else "--no-playlist")
    # --sleep-interval spaces out sequential requests to dodge rate limits; that
    # only matters when a run makes many requests (a playlist). For a single
    # video it's just a flat 1s of dead time before the download starts, so it's
    # scoped to the playlist path. --no-cache-dir was removed entirely: caching
    # lets yt-dlp reuse YouTube's extracted nsig/player function across requests
    # (keyed by player version, not IP — safe across VPN rotation) instead of
    # re-deriving it every time, which is the slow part of extraction.
    if playlist:
        flags.extend(["--sleep-interval", "1"])
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
            _vpn_exec_argv([
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
            ]),
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
    write_metadata: bool = False,
    finalize_progress_path: Optional[str] = None,
) -> List[str]:
    out_dir = os.path.abspath(out_dir)
    out_tpl = os.path.join(out_dir, "%(title)s.%(ext)s")

    argv = [
        YTDLP_BIN,
        "-f", fmt,
        *(_common_flags(playlist=playlist)),
        "--output", out_tpl,
        # Emit the resolved format_id before downloading so the frontend can size
        # its progress slots: "137+140" = a DASH video+audio merge (two download
        # phases), a bare id like "18" = one combined stream. --print output
        # prints even though --print implies --quiet — which is why this works
        # where yt-dlp's own "Downloading N format(s)" line (quiet-suppressed) does
        # not.
        "--print", "before_dl:[dl_streams] %(format_id)s",
        "--progress",
        "--newline",
        "--no-color",
    ]

    if finalize_progress_path:
        # Duration is kept internal and paired with FFmpeg's machine-readable
        # -progress output so the parent process can emit genuine finalization
        # percentages for MP3 conversion and video merge/remux work.
        argv.extend([
            "--print", "before_dl:[finalize_duration] %(duration)s",
        ])

    # Media Session metadata (lock-screen). Controlled per-request via the
    # `metadata` API field (write_metadata here; default off, opt-in). Adds a [meta]
    # print (title/artist, keyed by filepath so playlists correlate per track)
    # and writes a sidecar JPG thumbnail for ALL formats. --write-thumbnail
    # writes a SEPARATE file and does NOT remux the video container (unlike
    # --embed-thumbnail), so it's safe for mp4/webm. The JPG lands next to the
    # media file; api.py serves it via R2 or /api/fetch.
    if write_metadata:
        argv.extend([
            "--print",
            "after_move:[meta] %(filepath)s\t%(title)s\t%(artist,uploader,creator,channel)s",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
        ])

    # Keep the traditional bare final path for single-file jobs. For playlists,
    # emit a tagged internal completion event after metadata so optional callers
    # can track the completed path without exposing the server path.
    if playlist:
        argv.extend(["--print", "after_move:[file_done] %(filepath)s"])
    else:
        argv.extend(["--print", "after_move:filepath"])

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
        # tag without touching the container format. This is independent of the
        # Media Session feature above (it embeds art into the file itself).
        argv.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",           # cover art in ID3
        ])
        if finalize_progress_path:
            # Scope -progress to the actual audio conversion only. Thumbnail and
            # metadata FFmpeg work stay out of the user-facing finalize stream.
            argv.extend([
                "--postprocessor-args",
                f"ExtractAudio+ffmpeg:-progress {finalize_progress_path} -nostats",
            ])
        # Ensure the embedded art is JPG. When metadata is on this is already
        # present globally; add it here too so MP3 cover art is still converted
        # when metadata is off. yt-dlp de-dupes repeated flags, so it's harmless.
        if not write_metadata:
            argv.extend(["--convert-thumbnails", "jpg"])
    else:
        # Video: no --embed-thumbnail to avoid unwanted container changes.
        if merge_output_format:
            argv.extend(["--merge-output-format", merge_output_format])
        if finalize_progress_path:
            # yt-dlp may finalize video by merging separate video/audio streams,
            # remuxing containers, or (less commonly) converting video. Attach
            # FFmpeg -progress only to those media postprocessors so unrelated
            # thumbnail/metadata work cannot generate misleading percentages.
            for pp_name in ("Merger", "VideoRemuxer", "VideoConvertor"):
                argv.extend([
                    "--postprocessor-args",
                    f"{pp_name}+ffmpeg:-progress {finalize_progress_path} -nostats",
                ])

    # Sites with no dedicated extractor fall back to yt-dlp's [generic]
    # extractor. When such a site sits behind Cloudflare, the default TLS
    # fingerprint earns a 403 anti-bot challenge; impersonation via curl_cffi
    # clears it. Scoped to the generic extractor, so sites with their own
    # extractor (YouTube, etc.) are unaffected — this is a no-op for them.
    if _CURL_CFFI_AVAILABLE:
        argv.extend(["--extractor-args", "generic:impersonate"])

    # PornHub blocks non-browser TLS fingerprints (HTTP 410/403 errors).
    # Requires curl_cffi: included via yt-dlp[default,curl-cffi] in requirements.txt
    if _PH_URL_RE.search(url) and _CURL_CFFI_AVAILABLE:
        argv.extend(["--impersonate", "chrome"])

    argv.append(url)
    return argv


# =========================
# Embedded cover art
# =========================
def _square_embedded_cover(mp3_path: str) -> None:
    """
    Center-crop the cover art embedded in an MP3 to a square, in place.

    yt-dlp's --embed-thumbnail writes the source thumbnail as-is, so YouTube's
    16:9 frame ends up as the file's album art — which players like Apple Music
    render letterboxed. This rewrites only the ID3 APIC frame with a squared
    JPEG; the audio frames and container are never touched (mutagen edits just
    the tag region, so there is no re-encode and no quality loss).

    Best-effort: missing deps, no embedded art, an already-square cover, or any
    failure leaves the file unchanged. Cover art is non-essential and must never
    break a download.
    """
    try:
        import io
        from PIL import Image
        from mutagen.id3 import ID3, APIC
    except Exception:
        return
    try:
        tags = ID3(mp3_path)
    except Exception:
        return  # no ID3 tag / unreadable — nothing to do
    pics = tags.getall("APIC")
    if not pics:
        return
    try:
        im = Image.open(io.BytesIO(pics[0].data)).convert("RGB")
        w, h = im.size
        if w == h:
            return  # already square — leave the file as-is
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        buf = io.BytesIO()
        im.crop((left, top, left + side, top + side)).save(buf, "JPEG", quality=90)
        data = buf.getvalue()
    except Exception:
        return
    try:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
        tags.save(mp3_path, v2_version=3)
    except Exception:
        pass


# =========================
# FFmpeg finalization progress
# =========================
def _parse_ffmpeg_clock(value: str) -> Optional[float]:
    """Parse FFmpeg HH:MM:SS.microseconds progress time into seconds."""
    try:
        hours, minutes, seconds = (value or "").strip().split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _monitor_finalize_fifo(
    *,
    fifo_path: str,
    duration_state: dict,
    stop_event: threading.Event,
    on_line: Callable[[str], None],
    progress_state: Optional[dict] = None,
) -> None:
    """Translate FFmpeg ``-progress`` records into ``[finalize]`` SSE lines.

    The FIFO is opened read/write and non-blocking by this process. Keeping a
    local write descriptor open prevents EOF between sequential playlist tracks,
    while still allowing sequential FFmpeg postprocessors to open the FIFO for
    writing immediately.
    """
    fd: Optional[int] = None
    try:
        fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
        buf = ""
        active = False
        last_pct = -1.0
        state = progress_state if progress_state is not None else {}
        state.update({"seen": False, "active": False, "last_pct": -1.0})

        def emit(pct: float) -> None:
            nonlocal last_pct
            pct = max(0.0, min(100.0, pct))
            # FFmpeg normally reports twice per second. Avoid duplicate records
            # while still keeping two-decimal precision for genuine movement.
            if pct < 100.0 and last_pct >= 0.0 and pct < last_pct + 0.10:
                return
            last_pct = pct
            state["seen"] = True
            state["last_pct"] = pct
            try:
                on_line(f"[finalize] {pct:.2f}%")
            except Exception:
                # Progress reporting is best-effort and must never break the
                # actual media finalization if the client disappears mid-job.
                pass

        # Once stop_event is set, make one final non-blocking drain of anything
        # FFmpeg already wrote before exiting. The old `while not stop_event...`
        # loop could stop before consuming the terminal `progress=end` record.
        while True:
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                if stop_event.is_set():
                    break
                stop_event.wait(0.10)
                continue
            except OSError:
                break

            if not chunk:
                if stop_event.is_set():
                    break
                stop_event.wait(0.10)
                continue

            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                raw, buf = buf.split("\n", 1)
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if key in {"out_time_us", "out_time_ms", "out_time"}:
                    if not active:
                        active = True
                        state["active"] = True
                        last_pct = -1.0
                        emit(0.0)

                    elapsed: Optional[float] = None
                    if key in {"out_time_us", "out_time_ms"}:
                        try:
                            # FFmpeg's machine-readable progress values are in
                            # microseconds (out_time_us is explicit; out_time_ms
                            # is the legacy name for the same unit).
                            elapsed = float(value) / 1_000_000.0
                        except ValueError:
                            elapsed = None
                    else:
                        elapsed = _parse_ffmpeg_clock(value)

                    try:
                        duration = float(duration_state.get("seconds") or 0.0)
                    except (TypeError, ValueError):
                        duration = 0.0

                    if elapsed is not None and duration > 0.0:
                        # Reserve exactly 100% for FFmpeg's progress=end event;
                        # extractor duration metadata can be slightly rounded.
                        emit(min(99.90, (elapsed / duration) * 100.0))

                elif key == "progress" and value == "end":
                    if not active:
                        active = True
                        state["active"] = True
                        last_pct = -1.0
                        emit(0.0)
                    emit(100.0)
                    active = False
                    state["active"] = False
                    state["last_pct"] = 100.0
                    last_pct = -1.0
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


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
    write_metadata: bool = False,
    on_file_done: Optional[Callable[[str], None]] = None,
) -> "str | List[str]":
    """
    Streams yt-dlp stdout line-by-line via on_line.

    Returns:
      str       — path to single downloaded file   (playlist=False)
      List[str] — ordered paths completed in this invocation (playlist=True)

    When playlist=True and on_file_done is provided, yt-dlp emits a tagged
    after_move event for each fully post-processed track and invokes the callback.
    The production API keeps this hook lightweight; R2 transfer happens only
    after the collection download phase has finished.
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    finalize_fifo: Optional[str] = None
    finalize_stop: Optional[threading.Event] = None
    finalize_thread: Optional[threading.Thread] = None
    finalize_duration = {"seconds": 0.0}
    finalize_progress_state = {"seen": False, "active": False, "last_pct": -1.0}

    # One private FIFO per yt-dlp invocation. Failure to create it simply
    # disables finalization progress reporting; the download itself still proceeds.
    candidate = os.path.join(
        out_dir,
        f".ytpdl-finalize-{os.getpid()}-{threading.get_ident()}-{id(finalize_duration):x}.fifo",
    )
    try:
        if os.path.exists(candidate):
            os.unlink(candidate)
        os.mkfifo(candidate, 0o600)
        finalize_fifo = candidate
        finalize_stop = threading.Event()
        finalize_thread = threading.Thread(
            target=_monitor_finalize_fifo,
            kwargs={
                "fifo_path": finalize_fifo,
                "duration_state": finalize_duration,
                "stop_event": finalize_stop,
                "on_line": on_line,
                "progress_state": finalize_progress_state,
            },
            daemon=True,
        )
        finalize_thread.start()
    except OSError:
        finalize_fifo = None
        finalize_stop = None
        finalize_thread = None

    argv = _build_ytdlp_argv(
        url=url, out_dir=out_dir, fmt=fmt,
        merge_output_format=merge_output_format,
        extract_mp3=extract_mp3, playlist=playlist,
        archive_path=archive_path,
        write_metadata=write_metadata,
        finalize_progress_path=finalize_fifo,
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
    rc: Optional[int] = None
    try:
        proc = subprocess.Popen(
            _vpn_exec_argv(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except Exception:
        if finalize_stop is not None:
            finalize_stop.set()
        if finalize_thread is not None:
            finalize_thread.join(timeout=1.0)
        if finalize_fifo:
            try:
                os.unlink(finalize_fifo)
            except OSError:
                pass
        raise
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
            tail_lines.append(s)

            if s.startswith("[finalize_duration] "):
                raw_duration = s[len("[finalize_duration] "):].strip()
                try:
                    parsed_duration = float(raw_duration)
                    finalize_duration["seconds"] = parsed_duration if parsed_duration > 0 else 0.0
                except (TypeError, ValueError):
                    finalize_duration["seconds"] = 0.0
                continue

            # Playlist item completion is an internal hook, not a public SSE
            # event. yt-dlp emits it only after post-processing/moving is complete.
            if playlist and s.startswith("[file_done] "):
                p = s[len("[file_done] "):].strip().strip("'\"")
                if p and not os.path.isabs(p):
                    p = os.path.join(out_dir, p)
                if p:
                    p = os.path.abspath(p)
                    _maybe_add_candidate(p)
                    if extract_mp3 and os.path.exists(p):
                        _square_embedded_cover(p)
                    if on_file_done is not None and os.path.exists(p):
                        on_file_done(p)
                continue

            on_line(s)

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
                        _rotate_vpn()
                else:
                    # Single video path — rotate and let yt-dlp fail naturally.
                    _rotate_vpn()

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
    except Exception:
        # If a per-file callback fails, do not leave yt-dlp running in the
        # background with nobody draining its stdout pipe.
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    finally:
        stop_killer.set()
        try:
            proc.stdout.close()
        except Exception:
            pass
        if finalize_stop is not None:
            finalize_stop.set()
        if finalize_thread is not None:
            finalize_thread.join(timeout=1.0)

        # Successful yt-dlp completion guarantees that any FFmpeg postprocessor
        # it waited on also completed. If a finalization pass started but its
        # terminal progress record was still missed, close that real operation
        # at 100% instead of leaving the public log stranded below completion.
        if (
            rc == 0
            and finalize_progress_state.get("seen")
            and finalize_progress_state.get("active")
            and (finalize_thread is None or not finalize_thread.is_alive())
        ):
            try:
                on_line("[finalize] 100.00%")
            except Exception:
                pass
            finalize_progress_state["active"] = False
            finalize_progress_state["last_pct"] = 100.0

        if finalize_fifo:
            try:
                os.unlink(finalize_fifo)
            except OSError:
                pass

    # ---- Playlist mode ----
    if playlist:
        # Completion-hook callers may mutate files after each tagged after_move
        # event, so for that optional mode the captured events are authoritative.
        if on_file_done is not None:
            found = list(candidates)
            if not found and rc != 0:
                raise RuntimeError(
                    f"yt-dlp failed (playlist, format: {fmt})\n{_tail(chr(10).join(tail_lines))}"
                )
            return found

        # Local-ZIP mode: scan the entire output directory so we capture files downloaded in
        # PREVIOUS retry runs too.  Those were recorded in --download-archive
        # and silently skipped by yt-dlp this run, so they never appeared in
        # `candidates` — meaning a naive candidates-only approach would omit
        # them from the ZIP on every retry.
        # Sidecar thumbnails (.jpg from --write-thumbnail/--convert-thumbnails,
        # plus any un-converted .webp/.png) are NOT tracks — exclude them so they
        # never end up in the track list, the ZIP, or the playback queue. They're
        # still uploaded as Media Session artwork separately (api.py finds each
        # sidecar by its media file's stem, independently of this scan).
        _SKIP_EXTS = (".part", ".ytdl", ".tmp", ".zip", ".json", ".txt", ".filelist.txt",
                      ".jpg", ".jpeg", ".png", ".webp")
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
            tail_error = _tail("\n".join(tail_lines))
            raise RuntimeError(
                f"yt-dlp failed (playlist, format: {fmt})\n{tail_error}"
            )
        if extract_mp3:
            for _f in found:
                _square_embedded_cover(_f)
        return found

    # ---- Single file mode ----
    for p in reversed(candidates):
        if p and os.path.exists(p):
            ap = os.path.abspath(p)
            if extract_mp3:
                _square_embedded_cover(ap)
            return ap

    tail_txt = "\n".join(tail_lines)
    final_path = _extract_final_path_from_tail(tail_txt, out_dir)
    if final_path and os.path.exists(final_path):
        ap = os.path.abspath(final_path)
        if extract_mp3:
            _square_embedded_cover(ap)
        return ap

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
    write_metadata: bool = False,
    on_file_done: Optional[Callable[[str], None]] = None,
    skip_collection_zip: bool = False,
) -> str:
    """
    Download every track in a playlist.

    By default this preserves standalone/local behavior and returns a real ZIP
    on disk. API callers with R2 enabled can set skip_collection_zip=True so
    the media files remain local while api.py streams the final ZIP directly
    from those local files into R2, avoiding a second full-size local copy.
    on_file_done remains available as a lightweight completion hook for callers.

    On 429/bot-detection, kills yt-dlp immediately and raises RuntimeError
    so Render's existing retry loop fires and rotates the isolated Mullvad IP.
    A --download-archive file in out_dir ensures already-downloaded tracks
    are skipped on retry — no duplicate downloads.
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    validate_environment()
    _ensure_vpn()

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
            _rotate_vpn()
            _orig_on_line("[info] Rate limited — rotating VPN IP and retrying")
        _orig_on_line(line)

    on_line = _capturing_on_line

    # Preserve a logical cumulative list for optional completion-hook callers.
    _completed_files: List[str] = []
    _completed_seen: set[str] = set()

    def _track_done(path: str) -> None:
        ap = os.path.abspath(path)
        if ap not in _completed_seen:
            _completed_seen.add(ap)
            _completed_files.append(ap)
        if on_file_done is not None:
            on_file_done(ap)

    _completion_cb = _track_done if on_file_done is not None else None

    if mode == "mp3":
        files = _download_with_format_stream(
            url=url, out_dir=out_dir, fmt="bestaudio/best",
            merge_output_format=None, extract_mp3=True,
            on_line=on_line, playlist=True, write_metadata=write_metadata,
            abort_event=abort_event, archive_path=archive_path,
            on_file_done=_completion_cb,
        )
    elif mode == "best":
        try:
            files = _download_with_format_stream(
                url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                merge_output_format=None, extract_mp3=False,
                on_line=on_line, playlist=True, write_metadata=write_metadata,
                abort_event=abort_event, archive_path=archive_path,
                on_file_done=_completion_cb,
            )
        except Exception:
            try:
                files = _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                    merge_output_format="mp4", extract_mp3=False,
                    on_line=on_line, playlist=True, write_metadata=write_metadata,
                    abort_event=abort_event, archive_path=archive_path,
                    on_file_done=_completion_cb,
                )
            except Exception:
                files = _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                    merge_output_format="mp4", extract_mp3=False,
                    on_line=on_line, playlist=True, write_metadata=write_metadata,
                    abort_event=abort_event, archive_path=archive_path,
                    on_file_done=_completion_cb,
                )
    else:  # mp4
        try:
            files = _download_with_format_stream(
                url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                merge_output_format="mp4", extract_mp3=False,
                on_line=on_line, playlist=True, write_metadata=write_metadata,
                abort_event=abort_event, archive_path=archive_path,
                on_file_done=_completion_cb,
            )
        except Exception:
            files = _download_with_format_stream(
                url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                merge_output_format="mp4", extract_mp3=False,
                on_line=on_line, playlist=True, write_metadata=write_metadata,
                abort_event=abort_event, archive_path=archive_path,
                on_file_done=_completion_cb,
            )

    if on_file_done is not None:
        files = list(_completed_files)

    # If the IP was rate-limited, raise so Render's retry loop fires and
    # cycles Mullvad. The archive file means the retry picks up where we left off.
    if abort_event.is_set():
        raise RuntimeError(
            "Rate limited by YouTube — retrying with fresh VPN IP. "
            "Already-downloaded tracks will be skipped on retry."
        )

    if not files and on_file_done is None:
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
                    on_line=on_line, playlist=True, write_metadata=write_metadata,
                    abort_event=fill_abort, archive_path=archive_path,
                    on_file_done=_completion_cb,
                )
            elif mode == "best":
                try:
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                        merge_output_format=None, extract_mp3=False,
                        on_line=on_line, playlist=True, write_metadata=write_metadata,
                        abort_event=fill_abort, archive_path=archive_path,
                        on_file_done=_completion_cb,
                    )
                except Exception:
                    try:
                        files = _download_with_format_stream(
                            url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                            merge_output_format="mp4", extract_mp3=False,
                            on_line=on_line, playlist=True, write_metadata=write_metadata,
                            abort_event=fill_abort, archive_path=archive_path,
                            on_file_done=_completion_cb,
                        )
                    except Exception:
                        files = _download_with_format_stream(
                            url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                            merge_output_format="mp4", extract_mp3=False,
                            on_line=on_line, playlist=True, write_metadata=write_metadata,
                            abort_event=fill_abort, archive_path=archive_path,
                            on_file_done=_completion_cb,
                        )
            else:
                try:
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                        merge_output_format="mp4", extract_mp3=False,
                        on_line=on_line, playlist=True, write_metadata=write_metadata,
                        abort_event=fill_abort, archive_path=archive_path,
                        on_file_done=_completion_cb,
                    )
                except Exception:
                    files = _download_with_format_stream(
                        url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                        merge_output_format="mp4", extract_mp3=False,
                        on_line=on_line, playlist=True, write_metadata=write_metadata,
                        abort_event=fill_abort, archive_path=archive_path,
                        on_file_done=_completion_cb,
                    )
        except RuntimeError:
            raise  # propagate rate-limit kills to Render's retry system
        except Exception as e:
            _orig_on_line(f"[info] Playlist pass {_pass + 1} error: {e} — keeping {len(files)} tracks")
            break
        if on_file_done is not None:
            files = list(_completed_files)
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

    # The ZIP name remains the job's primary result. Local/standalone mode
    # creates it here. R2 collection mode keeps the source media files local
    # and lets api.py stream them directly into an R2 multipart ZIP, avoiding
    # a second full-size local copy.
    zip_path = os.path.join(out_dir, f"{stem}.zip")
    if not skip_collection_zip and on_file_done is None:
        _create_zip(files, zip_path)

    return zip_path

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
    write_metadata: bool = False,
    on_file_done: Optional[Callable[[str], None]] = None,
    skip_collection_zip: bool = False,
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
    _ensure_vpn()

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
            _rotate_vpn()
            on_line("[info] Rate limited — rotating VPN IP and retrying")
        on_line(line)

    def _run(url: str, url_dir: str, is_pl: bool,
             fmt: str, merge_fmt: Optional[str], mp3: bool,
             ab: threading.Event, done_cb: Optional[Callable[[str], None]] = None) -> List[str]:
        archive = os.path.join(url_dir, ".ytdlp-archive") if is_pl else None
        result = _download_with_format_stream(
            url=url, out_dir=url_dir, fmt=fmt,
            merge_output_format=merge_fmt, extract_mp3=mp3,
            on_line=_on_line_intercepted, playlist=is_pl,
            abort_event=ab, archive_path=archive,
            write_metadata=write_metadata,
            on_file_done=(done_cb if is_pl else None),
        )
        paths = [result] if isinstance(result, str) else result
        if done_cb is not None and not is_pl:
            for p in paths:
                if p:
                    done_cb(p)
        return paths

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

        _url_completed: List[str] = []
        _url_seen: set[str] = set()

        def _url_done(path: str) -> None:
            ap = os.path.abspath(path)
            if ap not in _url_seen:
                _url_seen.add(ap)
                _url_completed.append(ap)
            if on_file_done is not None:
                on_file_done(ap)

        _url_cb = _url_done if on_file_done is not None else None

        ua = threading.Event()
        if effective_mode == "mp3":
            files = _run(url, url_dir, is_pl, "bestaudio/best", None, True, ua, _url_cb)
        elif effective_mode == "best":
            try:
                files = _run(url, url_dir, is_pl, _fmt_best(cap), None, False, ua, _url_cb)
            except Exception:
                try:
                    files = _run(url, url_dir, is_pl, _fmt_mp4_apple_safe(cap), "mp4", False, ua, _url_cb)
                except Exception:
                    files = _run(url, url_dir, is_pl, "bestvideo+bestaudio/best", "mp4", False, ua, _url_cb)
        else:
            try:
                files = _run(url, url_dir, is_pl, _fmt_mp4_apple_safe(cap), "mp4", False, ua, _url_cb)
            except Exception:
                files = _run(url, url_dir, is_pl, "bestvideo+bestaudio/best", "mp4", False, ua, _url_cb)

        if on_file_done is not None:
            files = list(_url_completed)

        if ua.is_set():
            raise RuntimeError("Rate limited — retrying with fresh VPN IP.")

        # Playlist fill-in passes (mirrors download_playlist logic).
        if is_pl and files:
            for _pass in range(1, _passes):
                prev = len(files)
                pa = threading.Event()
                try:
                    if effective_mode == "mp3":
                        files = _run(url, url_dir, True, "bestaudio/best", None, True, pa, _url_cb)
                    elif effective_mode == "best":
                        try:
                            files = _run(url, url_dir, True, _fmt_best(cap), None, False, pa, _url_cb)
                        except Exception:
                            try:
                                files = _run(url, url_dir, True, _fmt_mp4_apple_safe(cap), "mp4", False, pa, _url_cb)
                            except Exception:
                                files = _run(url, url_dir, True, "bestvideo+bestaudio/best", "mp4", False, pa, _url_cb)
                    else:
                        try:
                            files = _run(url, url_dir, True, _fmt_mp4_apple_safe(cap), "mp4", False, pa, _url_cb)
                        except Exception:
                            files = _run(url, url_dir, True, "bestvideo+bestaudio/best", "mp4", False, pa, _url_cb)
                except RuntimeError:
                    raise
                except Exception:
                    break
                if on_file_done is not None:
                    files = list(_url_completed)
                if pa.is_set():
                    raise RuntimeError("Rate limited during playlist pass — retrying.")
                if len(files) <= prev:
                    break

        all_files.extend(files)

    if not all_files and on_file_done is None:
        raise RuntimeError("No files could be downloaded from the provided URLs.")

    # The ZIP name remains the primary result. Local mode creates it here;
    # R2 collection mode leaves it virtual while api.py streams the retained
    # local media files directly into R2. Codec differences between URLs are
    # not an issue.
    zip_path = os.path.join(out_dir, "multi_url.zip")
    if not skip_collection_zip and on_file_done is None:
        _create_zip(all_files, zip_path)

    return zip_path
# =========================
def download_video(
    *,
    url: str,
    resolution: int | None = 1080,
    extension: Optional[str] = None,
    out_dir: str = DEFAULT_OUT_DIR,
    on_line: Callable[[str], None],
    write_metadata: bool = False,
    on_file_done: Optional[Callable[[str], None]] = None,
    skip_collection_zip: bool = False,
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
                out_dir=out_dir, on_line=on_line, write_metadata=write_metadata,
                on_file_done=on_file_done, skip_collection_zip=skip_collection_zip,
            )
        url = urls[0]  # single URL with stray trailing comma

    if is_playlist_url(url):
        return download_playlist(
            url=url, resolution=resolution, extension=extension,
            out_dir=out_dir, on_line=on_line, write_metadata=write_metadata,
            on_file_done=on_file_done, skip_collection_zip=skip_collection_zip,
        )

    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    validate_environment()
    _ensure_vpn()

    mode = (extension or "mp4").lower().strip()
    cap = int(resolution or 1080)

    if mode == "mp3":
        return _download_with_format_stream(
            url=url, out_dir=out_dir, fmt="bestaudio/best",
            merge_output_format=None, extract_mp3=True, on_line=on_line,
            write_metadata=write_metadata,
        )
    if mode == "best":
        try:
            return _download_with_format_stream(
                url=url, out_dir=out_dir, fmt=_fmt_best(cap),
                merge_output_format=None, extract_mp3=False, on_line=on_line,
                write_metadata=write_metadata,
            )
        except Exception:
            try:
                return _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
                    merge_output_format="mp4", extract_mp3=False, on_line=on_line,
                    write_metadata=write_metadata,
                )
            except Exception:
                return _download_with_format_stream(
                    url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
                    merge_output_format="mp4", extract_mp3=False, on_line=on_line,
                    write_metadata=write_metadata,
                )
    try:
        return _download_with_format_stream(
            url=url, out_dir=out_dir, fmt=_fmt_mp4_apple_safe(cap),
            merge_output_format="mp4", extract_mp3=False, on_line=on_line,
            write_metadata=write_metadata,
        )
    except Exception:
        return _download_with_format_stream(
            url=url, out_dir=out_dir, fmt="bestvideo+bestaudio/best",
            merge_output_format="mp4", extract_mp3=False, on_line=on_line,
            write_metadata=write_metadata,
        )
