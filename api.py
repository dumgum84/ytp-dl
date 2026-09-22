#!/usr/bin/env python3
from __future__ import annotations

import json
import errno
import fcntl
import os
import queue
import shutil
import threading
import time
import zipfile
import mimetypes
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from flask import Flask, Response, jsonify, redirect, request, send_file, stream_with_context

from .downloader import download_video, validate_environment, is_playlist_url

app = Flask(__name__)

BASE_DOWNLOAD_DIR = os.environ.get("YTPDL_JOB_BASE_DIR", "/root/ytpdl_jobs")
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT = max(1, int(os.environ.get("YTPDL_MAX_CONCURRENT", "1")))

# Cross-process concurrency slots. Gunicorn workers are separate processes, so
# threading.BoundedSemaphore would multiply the configured limit by the worker
# count. Each active job instead holds an exclusive flock() on one slot file.
# The kernel releases the lock automatically if a worker dies, so crashed jobs
# cannot permanently leak capacity.
_SLOT_DIR = os.environ.get("YTPDL_SLOT_DIR", "/run/ytpdl-slots")
os.makedirs(_SLOT_DIR, exist_ok=True)

STALE_JOB_TTL_S = int(os.environ.get("YTPDL_STALE_JOB_TTL_S", "3600"))
DONE_TTL_S = int(os.environ.get("YTPDL_DONE_TTL_S", "300"))
MIN_FREE_DISK_MB = int(os.environ.get("YTPDL_MIN_FREE_DISK_MB", "8192"))
CLEANUP_INTERVAL_S = int(os.environ.get("YTPDL_CLEANUP_INTERVAL_S", "60"))
_ACTIVE_JOB_LOCK_DIR = os.environ.get("YTPDL_ACTIVE_LOCK_DIR", "/run/ytpdl-active")
_CLEANUP_LOCK_PATH = os.environ.get("YTPDL_CLEANUP_LOCK_PATH", "/run/lock/ytpdl-cleanup.lock")
R2_ZIP_PART_SIZE_MB = max(5, int(os.environ.get("YTPDL_R2_ZIP_PART_SIZE_MB", "16")))
R2_ZIP_WORKERS = max(1, int(os.environ.get("YTPDL_R2_ZIP_WORKERS", "10")))
os.makedirs(_ACTIVE_JOB_LOCK_DIR, exist_ok=True)
os.makedirs(os.path.dirname(_CLEANUP_LOCK_PATH), exist_ok=True)

_ALLOWED_EXTENSIONS = {"mp3", "mp4", "best"}
_BLOCKED_UAS = ("headless", "python-requests", "curl", "wget")
_R2_CLIENT = None
_R2_CLIENT_LOCK = threading.Lock()

VPS_API_TOKEN = os.environ.get("YTPDL_VPS_API_TOKEN", "").strip()


def _is_blocked_ua() -> bool:
    ua = request.headers.get("User-Agent", "")
    return any(bad in ua.lower() for bad in _BLOCKED_UAS)


def _is_authorized() -> bool:
    if not VPS_API_TOKEN:
        return True
    return request.headers.get("X-YTPDL-Token", "") == VPS_API_TOKEN


def _require_auth():
    """Return a 401 response if the request is not authorized, else None."""
    if not _is_authorized():
        return jsonify(error="Unauthorized"), 401
    return None


def _validate_url(url: str) -> str:
    """Raise ValueError if url is not a safe public http/https URL."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Only http/https URLs are allowed (got '{parsed.scheme or 'empty'}')")
    if not parsed.netloc:
        raise ValueError("URL must include a host")
    return url.strip()


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _sanitize_job_id(job_id: str) -> str:
    job_id = (job_id or "").strip()
    safe = "".join(c for c in job_id if c.isalnum() or c in ("-", "_"))
    return safe or str(int(time.time() * 1000))


def _job_dir(job_id: str) -> str:
    return os.path.join(BASE_DOWNLOAD_DIR, f"ytpdl_{_sanitize_job_id(job_id)}")


def _write_result_meta(job_dir: str, path: str, *, r2_key: str | None = None) -> None:
    try:
        now = int(time.time())
        meta_path = os.path.join(job_dir, "result.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "path": os.path.abspath(path),
                    "filename": os.path.basename(path),
                    "r2_key": r2_key,
                    "ts": now,
                    "expires_at": now + max(0, int(DONE_TTL_S)),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass


def _read_result_meta(job_dir: str) -> dict | None:
    meta_path = os.path.join(job_dir, "result.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _active_job_lock_path(job_dir: str) -> str:
    name = os.path.basename(os.path.abspath(job_dir)) or "job"
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", "."))
    return os.path.join(_ACTIVE_JOB_LOCK_DIR, f"{safe}.lock")


def _try_acquire_active_job_lock(job_dir: str) -> int | None:
    """Hold an exclusive cross-process lock for the full lifetime of one job."""
    fd = os.open(_active_job_lock_path(job_dir), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        os.close(fd)
        return None


def _release_active_job_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def _job_is_active(job_dir: str) -> bool:
    """Probe the external lock inode; active jobs are never eligible for cleanup."""
    fd = os.open(_active_job_lock_path(job_dir), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _try_cleanup_lock() -> int | None:
    """Only one Gunicorn worker performs a cleanup pass at a time."""
    fd = os.open(_CLEANUP_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        os.close(fd)
        return None


def _release_cleanup_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def _schedule_delete_job_dir(job_dir: str, *, after_s: int) -> None:
    def _worker():
        try:
            time.sleep(max(0, int(after_s)))
            # The caller can schedule deletion before its finally block releases
            # the active lock. Wait until the lock is truly free before deleting.
            while _job_is_active(job_dir):
                time.sleep(0.25)
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


def _cleanup_stale_jobs() -> None:
    cleanup_fd = _try_cleanup_lock()
    if cleanup_fd is None:
        return
    now = time.time()
    try:
        for name in os.listdir(BASE_DOWNLOAD_DIR):
            p = os.path.join(BASE_DOWNLOAD_DIR, name)
            if not os.path.isdir(p) or _job_is_active(p):
                continue
            meta = _read_result_meta(p)
            if isinstance(meta, dict):
                exp = meta.get("expires_at")
                try:
                    if exp is not None and now >= float(exp):
                        shutil.rmtree(p, ignore_errors=True)
                        continue
                except Exception:
                    pass
            try:
                if now - os.path.getmtime(p) > STALE_JOB_TTL_S:
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _release_cleanup_lock(cleanup_fd)


def _free_disk_mb() -> float:
    """Return free disk space in MB for BASE_DOWNLOAD_DIR's filesystem."""
    try:
        st = os.statvfs(BASE_DOWNLOAD_DIR)
        return (st.f_bavail * st.f_frsize) / (1024 * 1024)
    except Exception:
        return float("inf")


def _is_enospc(value) -> bool:
    """Return True for a real 'disk full' / ENOSPC condition."""
    if isinstance(value, OSError) and getattr(value, "errno", None) == errno.ENOSPC:
        return True
    text = str(value or "").lower()
    return "no space left on device" in text or "errno 28" in text


def _emergency_cleanup() -> None:
    """Delete only INACTIVE job dirs oldest-first until the reserve is restored."""
    cleanup_fd = _try_cleanup_lock()
    if cleanup_fd is None:
        return
    try:
        dirs = []
        for name in os.listdir(BASE_DOWNLOAD_DIR):
            p = os.path.join(BASE_DOWNLOAD_DIR, name)
            if not os.path.isdir(p) or _job_is_active(p):
                continue
            try:
                dirs.append((os.path.getmtime(p), p))
            except Exception:
                pass
        dirs.sort()  # oldest first
        for _, p in dirs:
            if _free_disk_mb() >= MIN_FREE_DISK_MB:
                break
            if _job_is_active(p):
                continue
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass
    finally:
        _release_cleanup_lock(cleanup_fd)


def _purge_job_payload_keep_meta(job_dir: str) -> None:
    """Free completed R2-backed payload bytes immediately, retaining result.json briefly."""
    try:
        for name in os.listdir(job_dir):
            if name in {"result.json", _COLLECTION_STATE_FILE}:
                continue
            p = os.path.join(job_dir, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def _background_cleanup_worker() -> None:
    """Background thread: runs cleanup every CLEANUP_INTERVAL_S seconds."""
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL_S)
            _cleanup_stale_jobs()
            if _free_disk_mb() < MIN_FREE_DISK_MB:
                _emergency_cleanup()
        except Exception:
            pass


# Every Gunicorn process starts this thread, but the global cleanup flock ensures
# only one worker mutates the job directory tree during any cleanup pass.
threading.Thread(target=_background_cleanup_worker, daemon=True).start()


def _slot_path(index: int) -> str:
    return os.path.join(_SLOT_DIR, f"slot-{index:03d}.lock")


def _try_acquire_job_slot() -> int | None:
    """Acquire one VPS-global download slot and return its open file descriptor.

    flock() is visible across all Gunicorn processes. The descriptor must stay
    open for the full job lifetime; closing it releases the slot.
    """
    for index in range(MAX_CONCURRENT):
        fd = os.open(_slot_path(index), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        except Exception:
            os.close(fd)
            continue

        # Best-effort owner metadata for debugging; the lock itself is the truth.
        try:
            os.ftruncate(fd, 0)
            payload = f"pid={os.getpid()} acquired={int(time.time())}\n".encode()
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            pass
        return fd
    return None


def _release_job_slot(slot_fd: int | None) -> None:
    if slot_fd is None:
        return
    try:
        fcntl.flock(slot_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(slot_fd)
    except Exception:
        pass


def _global_in_use() -> int:
    """Count currently locked slots across every Gunicorn worker."""
    used = 0
    for index in range(MAX_CONCURRENT):
        fd = os.open(_slot_path(index), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                used += 1
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return used

def _r2_enabled() -> bool:
    return _truthy(os.environ.get("YTPDL_R2_UPLOAD", "0"))


def _get_r2_client():
    global _R2_CLIENT
    if _R2_CLIENT is not None:
        return _R2_CLIENT
    endpoint = (os.environ.get("R2_ENDPOINT") or "").strip().rstrip("/")
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    access_key = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    if not endpoint or not bucket or not access_key or not secret_key:
        return None
    with _R2_CLIENT_LOCK:
        if _R2_CLIENT is not None:
            return _R2_CLIENT
        _R2_CLIENT = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.environ.get("AWS_REGION", "auto"),
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
    return _R2_CLIENT


def _guess_content_type(filename: str) -> str:
    ct = mimetypes.guess_type(filename)[0]
    if ct:
        return ct
    low = (filename or "").lower()
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


@dataclass
class _ProgressState:
    total: int
    sent: int = 0
    last_emit_t: float = 0.0
    last_pct_int: int = -1
    lock: Lock = field(default_factory=Lock)


def _make_r2_progress_cb(
    *,
    total_bytes: int,
    on_progress: Callable[[float], None],
    min_interval_s: float = 0.25,
    min_step_pct: int = 1,
) -> Callable[[int], None]:
    st = _ProgressState(total=max(0, int(total_bytes or 0)))

    def cb(bytes_amount: int) -> None:
        if bytes_amount <= 0:
            return
        now = time.monotonic()
        with st.lock:
            st.sent = min(st.total, st.sent + int(bytes_amount))
            if st.total <= 0:
                return
            pct = (st.sent * 100.0) / st.total
            pct_int = int(pct)
            should_emit = (pct_int >= 100 and st.last_pct_int != 100) or (
                (now - st.last_emit_t) >= min_interval_s
                and (pct_int - st.last_pct_int) >= min_step_pct
            )
            if not should_emit:
                return
            st.last_emit_t = now
            st.last_pct_int = pct_int
        on_progress(min(100.0, max(0.0, float(pct))))

    return cb


def _r2_key_for(job_id: str, filename: str) -> str:
    return f"{_sanitize_job_id(job_id)}/{os.path.basename(filename)}"


def _r2_presigned_get(*, key: str, filename: str, as_attachment: bool) -> str | None:
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    client = _get_r2_client()
    if client is None or not bucket or not key:
        return None
    disposition = "attachment" if as_attachment else "inline"
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentType": _guess_content_type(filename),
                "ResponseContentDisposition": f'{disposition}; filename="{os.path.basename(filename)}"',
            },
            ExpiresIn=3600,
        )
    except Exception:
        return None


def _upload_to_r2(
    *,
    local_path: str,
    job_id: str,
    filename: str,
    on_progress: Callable[[float], None] | None = None,
) -> str:
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    client = _get_r2_client()
    if client is None or not bucket:
        raise RuntimeError("R2 not configured (missing endpoint/bucket/keys)")
    key = _r2_key_for(job_id, filename)
    ct = _guess_content_type(filename)
    extra = {"ContentType": ct, "ContentDisposition": f'inline; filename="{filename}"'}
    try:
        total = int(os.path.getsize(local_path))
    except Exception:
        total = 0
    cb = None
    if on_progress is not None and total > 0:
        cb = _make_r2_progress_cb(total_bytes=total, on_progress=on_progress)
    client.upload_file(local_path, bucket, key, ExtraArgs=extra, Callback=cb)
    return key


# ─── Thumbnail (Media Session artwork) helpers ───────────────────────────────

_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _find_sidecar_thumb(media_path: str) -> str | None:
    """
    Locate the sidecar thumbnail yt-dlp wrote next to a media file.

    --write-thumbnail --convert-thumbnails jpg produces "<stem>.jpg" alongside
    "<stem>.<media_ext>". We match on the stem and prefer .jpg, falling back to
    other image extensions in case conversion was skipped (e.g. a source that
    was already jpg). Returns None when no thumbnail exists.
    """
    if not media_path:
        return None
    stem, _ = os.path.splitext(media_path)
    for ext in _THUMB_EXTS:
        cand = stem + ext
        if os.path.isfile(cand):
            return cand
    # Some extractors append the format id, e.g. "<stem>.<fmt>.jpg". Scan the
    # directory for any image file sharing the stem prefix as a last resort.
    try:
        base = os.path.basename(stem)
        d = os.path.dirname(media_path) or "."
        for n in os.listdir(d):
            low = n.lower()
            if low.endswith(_THUMB_EXTS) and n.startswith(base):
                return os.path.join(d, n)
    except Exception:
        pass
    return None


def _square_crop(path: str) -> None:
    """
    Center-crop an image file to a square, in place. Applied to audio cover art
    so the iOS lock screen — which renders the sidecar thumbnail URL — shows a
    clean square instead of a letterboxed 16:9 frame.

    Best-effort: if Pillow is unavailable or the image can't be read, the file
    is left untouched. Artwork is non-essential and must never break a download.
    """
    try:
        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w == h:
                return
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = im.crop((left, top, left + side, top + side))
        cropped.save(path, "JPEG", quality=90)
    except Exception:
        pass


def _upload_thumb_for(*, media_path: str, job_id: str, push) -> None:
    """
    Upload the sidecar thumbnail for a media file to R2 and emit
    [meta_thumb] media=<media_filename>\tkey=<key> so Render can correlate the
    thumbnail to its track and rewrite it into a worker URL for Media Session.

    Best-effort: any failure is silently ignored — artwork is non-essential and
    must never break a download.
    """
    try:
        thumb = _find_sidecar_thumb(media_path)
        if not thumb:
            return
        if media_path.lower().endswith(".mp3"):
            _square_crop(thumb)
        media_name = os.path.basename(media_path)
        thumb_name = os.path.basename(thumb)
        key = _upload_to_r2(local_path=thumb, job_id=job_id, filename=thumb_name)
        push(f"[meta_thumb] media={media_name}\tkey={key}")
    except Exception:
        pass


def _rewrite_meta_line(line: str) -> str | None:
    """
    Convert a raw yt-dlp [meta] print into a clean, path-free SSE line.

    in:  [meta] <abs_filepath>\t<title>\t<artist>
    out: [meta] media=<basename>\ttitle=<title>\tartist=<artist>

    Returns None if the line can't be parsed (so the caller drops it). Fields
    that yt-dlp couldn't fill come through as "NA" and are blanked. The server
    filepath is reduced to its basename so no server paths reach the browser.
    """
    try:
        payload = line[len("[meta] "):]
        parts = payload.split("\t")
        if not parts or not parts[0].strip():
            return None
        media = os.path.basename(parts[0].strip())
        title = (parts[1].strip() if len(parts) > 1 else "")
        artist = (parts[2].strip() if len(parts) > 2 else "")
        if title.upper() == "NA":
            title = ""
        if artist.upper() == "NA":
            artist = ""
        # Tabs already delimit fields; titles/artists with stray tabs are
        # extremely unlikely but collapse them defensively.
        title = title.replace("\t", " ")
        artist = artist.replace("\t", " ")
        return f"[meta] media={media}\ttitle={title}\tartist={artist}"
    except Exception:
        return None


# ─── Collection/R2 helpers ───────────────────────────────────────────────────

# Local-ZIP helper retained for R2-disabled deployments.
def _collect_track_files(job_dir: str, zip_path: str) -> list[str]:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = {os.path.basename(n) for n in zf.namelist()}
    except Exception:
        return []
    out: list[str] = []
    zip_abs = os.path.abspath(zip_path)
    for root, dirs, files in os.walk(job_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name not in members:
                continue
            p = os.path.join(root, name)
            if os.path.abspath(p) != zip_abs:
                out.append(p)
    return sorted(out, key=lambda p: os.path.basename(p).lower())


_COLLECTION_STATE_FILE = ".playlist-state.json"
_MEDIA_SKIP_EXTS = (
    ".part", ".ytdl", ".tmp", ".zip", ".json", ".txt", ".filelist.txt",
    ".jpg", ".jpeg", ".png", ".webp",
)


def _collection_state_path(job_dir: str) -> str:
    return os.path.join(job_dir, _COLLECTION_STATE_FILE)


def _load_collection_state(job_dir: str) -> dict:
    path = _collection_state_path(job_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tracks = data.get("tracks") if isinstance(data, dict) else None
        if isinstance(tracks, list):
            return {"version": 1, "tracks": tracks}
    except Exception:
        pass
    return {"version": 1, "tracks": []}


def _save_collection_state(job_dir: str, state: dict) -> None:
    path = _collection_state_path(job_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _state_track_by_filename(state: dict, filename: str) -> dict | None:
    for item in state.get("tracks") or []:
        if isinstance(item, dict) and item.get("filename") == filename and item.get("r2_key"):
            return item
    return None


def _verify_r2_object(key: str, *, expected_size: int | None = None) -> int:
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    client = _get_r2_client()
    if client is None or not bucket:
        raise RuntimeError("R2 not configured")
    head = client.head_object(Bucket=bucket, Key=key)
    size = int(head.get("ContentLength", 0) or 0)
    if expected_size is not None and size != int(expected_size):
        raise RuntimeError(f"R2 verification failed for {key}: expected {expected_size} bytes, got {size}")
    return size


def _record_uploaded_track(*, job_dir: str, state: dict, media_path: str,
                           r2_key: str, thumb_key: str | None) -> dict:
    """Persist one R2-backed collection item without deleting its local source."""
    name = os.path.basename(media_path)
    size = int(os.path.getsize(media_path)) if os.path.isfile(media_path) else 0
    existing = _state_track_by_filename(state, name)
    item = {
        "filename": name,
        "r2_key": r2_key,
        "size": size,
        "thumb_key": thumb_key,
    }
    if existing is None:
        (state.setdefault("tracks", [])).append(item)
    else:
        existing.update(item)
        item = existing
    _save_collection_state(job_dir, state)
    return item


def _iter_collection_media(job_dir: str) -> list[str]:
    """
    Return finalized collection media still present locally.

    Sort globally by mtime so playlist/multi-URL order follows download order
    rather than filesystem/alphabetical order. Sidecars, archives, temp files,
    manifests and ZIPs are excluded.
    """
    found: list[tuple[float, str]] = []
    for root, dirs, files in os.walk(job_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            low = name.lower()
            if name.startswith(".") or any(low.endswith(ext) for ext in _MEDIA_SKIP_EXTS):
                continue
            p = os.path.join(root, name)
            if not os.path.isfile(p):
                continue
            try:
                mt = os.path.getmtime(p)
            except Exception:
                mt = 0.0
            found.append((mt, os.path.abspath(p)))
    found.sort(key=lambda item: (item[0], item[1].lower()))
    return [p for _, p in found]


def _upload_collection_tracks_from_local(*, media_paths: list[str], job_dir: str,
                                         job_id: str, state: dict, push) -> None:
    """
    Upload completed collection tracks from local disk after downloading ends.

    Local media is intentionally retained until the final ZIP has been streamed
    successfully to R2, keeping R2 network work out of the yt-dlp download loop
    and avoiding a second full-size local ZIP copy.
    """
    if not media_paths:
        raise RuntimeError("No playlist/multi-URL media files found on local disk")

    for media_path in media_paths:
        if not os.path.isfile(media_path):
            raise RuntimeError(f"Collection media disappeared before R2 upload: {media_path}")

        name = os.path.basename(media_path)
        local_size = int(os.path.getsize(media_path))
        existing = _state_track_by_filename(state, name)

        # Same-job retries may already have uploaded some tracks. Verify and reuse
        # those objects instead of uploading them again.
        if existing is not None:
            try:
                _verify_r2_object(existing["r2_key"], expected_size=local_size)
                thumb_key = existing.get("thumb_key")
                if thumb_key:
                    _verify_r2_object(thumb_key)
                continue
            except Exception:
                pass

        def _pct(v: float) -> None:
            push(f"[r2_upload] {v:.2f}%")

        key = _upload_to_r2(
            local_path=media_path,
            job_id=job_id,
            filename=name,
            on_progress=_pct,
        )
        _verify_r2_object(key, expected_size=local_size)
        push("[r2_upload] 100.00%")

        thumb_key = None
        thumb = _find_sidecar_thumb(media_path)
        if thumb:
            if media_path.lower().endswith(".mp3"):
                _square_crop(thumb)
            thumb_name = os.path.basename(thumb)
            thumb_key = _upload_to_r2(
                local_path=thumb,
                job_id=job_id,
                filename=thumb_name,
            )
            _verify_r2_object(thumb_key, expected_size=os.path.getsize(thumb))
            push(f"[meta_thumb] media={name}\tkey={thumb_key}")

        _record_uploaded_track(
            job_dir=job_dir,
            state=state,
            media_path=media_path,
            r2_key=key,
            thumb_key=thumb_key,
        )
        push(f"[r2_track] key={key}")


class _R2MultipartWriter:
    """Unseekable ZIP sink that pipelines multipart parts directly into R2."""

    def __init__(self, *, key: str, filename: str):
        self.client = _get_r2_client()
        self.bucket = (os.environ.get("R2_BUCKET") or "").strip()
        if self.client is None or not self.bucket:
            raise RuntimeError("R2 not configured")
        self.key = key
        self.filename = filename
        self.part_size = R2_ZIP_PART_SIZE_MB * 1024 * 1024
        self.max_workers = R2_ZIP_WORKERS
        self.buffer = bytearray()
        self.parts: list[dict] = []
        self.position = 0
        self.closed = False
        self.upload_id: str | None = None
        self._next_part_number = 1
        self._futures: set[Future] = set()
        self._executor: ThreadPoolExecutor | None = None
        resp = self.client.create_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            ContentType="application/zip",
            ContentDisposition=f'inline; filename="{os.path.basename(filename)}"',
        )
        self.upload_id = resp["UploadId"]
        try:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="ytpdl-r2-zip",
            )
        except Exception:
            try:
                self.client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                )
            except Exception:
                pass
            self.closed = True
            raise

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed R2 multipart writer")
        if not data:
            return 0
        b = bytes(data)
        self.buffer.extend(b)
        self.position += len(b)
        while len(self.buffer) >= self.part_size:
            self._submit_part(self.part_size)
        return len(b)

    def _upload_part_body(self, *, part_number: int, body: bytes) -> dict:
        resp = self.client.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=part_number,
            Body=body,
        )
        return {"ETag": resp["ETag"], "PartNumber": part_number}

    def _collect_done(self, done: set[Future]) -> None:
        for future in done:
            self.parts.append(future.result())
            self._futures.discard(future)

    def _wait_for_one(self) -> None:
        if not self._futures:
            return
        done, _ = wait(self._futures, return_when=FIRST_COMPLETED)
        self._collect_done(done)

    def _submit_part(self, length: int) -> None:
        if length <= 0:
            return

        # Keep at most max_workers full parts resident/in flight. This overlaps
        # local ZIP production with R2 uploads without letting a large ZIP queue
        # an unbounded amount of media in RAM.
        while len(self._futures) >= self.max_workers:
            self._wait_for_one()

        body = bytes(self.buffer[:length])
        del self.buffer[:length]
        part_number = self._next_part_number
        self._next_part_number += 1
        if self._executor is None:
            raise RuntimeError("R2 multipart executor is not available")
        self._futures.add(
            self._executor.submit(
                self._upload_part_body,
                part_number=part_number,
                body=body,
            )
        )

    def _finish_pending_parts(self) -> None:
        while self._futures:
            done, _ = wait(self._futures, return_when=FIRST_COMPLETED)
            self._collect_done(done)

    def _shutdown_executor(self, *, cancel_futures: bool = False) -> None:
        executor = self._executor
        if executor is None:
            return
        self._executor = None
        executor.shutdown(wait=True, cancel_futures=cancel_futures)

    def complete(self) -> None:
        if self.closed:
            return
        if self.buffer:
            self._submit_part(len(self.buffer))
        self._finish_pending_parts()
        self._shutdown_executor()
        if not self.parts:
            raise RuntimeError("Streaming ZIP produced no multipart data")
        self.parts.sort(key=lambda part: part["PartNumber"])
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts},
        )
        self.closed = True

    def abort(self) -> None:
        if self.closed or not self.upload_id:
            return
        try:
            self._shutdown_executor(cancel_futures=True)
        except Exception:
            pass
        try:
            self.client.abort_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.upload_id,
            )
        except Exception:
            pass
        self.closed = True


def _stream_local_collection_zip_to_r2(*, media_paths: list[str], job_id: str,
                                       zip_filename: str, push) -> str:
    """
    Stream a ZIP directly from local collection files into an R2 multipart upload.

    No full ZIP is written to VPS disk and no track is downloaded back from R2.
    ZIP_STORED avoids recompressing already-compressed media.
    """
    media_paths = [os.path.abspath(p) for p in media_paths if p and os.path.isfile(p)]
    if not media_paths:
        raise RuntimeError("Cannot build collection ZIP: no local media files")

    zip_key = _r2_key_for(job_id, zip_filename)
    writer = _R2MultipartWriter(key=zip_key, filename=zip_filename)
    total_source = sum(max(0, int(os.path.getsize(p))) for p in media_paths)
    copied = 0
    last_pct = -1

    try:
        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for media_path in media_paths:
                filename = os.path.basename(media_path) or "track"
                with open(media_path, "rb") as src:
                    with zf.open(filename, "w", force_zip64=True) as member:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            member.write(chunk)
                            copied += len(chunk)
                            if total_source > 0:
                                pct = min(99, int(copied * 100 / total_source))
                                if pct != last_pct:
                                    last_pct = pct
                                    push(f"[r2_upload] {pct:.2f}%")

        writer.complete()
        _verify_r2_object(zip_key, expected_size=writer.tell())
        push("[r2_upload] 100.00%")
        return zip_key
    except Exception:
        writer.abort()
        raise


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def handle_download():
    """
    Streams real-time yt-dlp stdout as SSE events.

    Single URLs produce one media file. Playlists / multi-URL jobs expose a
    ZIP as the primary result. With R2 enabled, the collection first downloads
    normally to local disk. After yt-dlp finishes, the local tracks are uploaded
    to R2 and the final ZIP is streamed directly from those local files into an
    R2 multipart upload, so no second full-size local ZIP is created.

    SSE event summary
    -----------------
    [start]       job_id=<id>
    [total_items] <n>            (playlist/multi only)
    <yt-dlp lines>
    [finalize]    XX.XX%         (when measurable FFmpeg finalization runs)
    [meta]        media=<name>\ttitle=<t>\tartist=<a>   (per file - lock screen)
    [r2_upload]   XX.XX%         (R2 only - per track, then the result file)
    [r2_track]    key=<key>      (R2 + playlist/multi only - one per track)
    [meta_thumb]  media=<name>\tkey=<key>   (R2 on - artwork uploaded to R2)
                  media=<name>\tfile=<thumb_filename>  (R2 off - fetch via /api/fetch)
    [ready]       job_id=<id>
    [file]        <filename>     (the media file, or the ZIP for playlists)
    [r2]          key=<key>      (R2 only - the result file's key)
    [fetch]       /api/fetch/<id>
    [error]       <message>      (terminal failure; followed by [done])
    [done]
    """
    if not _is_authorized():
        if _is_blocked_ua():
            return jsonify(error="Forbidden"), 403
        return jsonify(error="Unauthorized"), 401

    _cleanup_stale_jobs()

    slot_fd = _try_acquire_job_slot()
    if slot_fd is None:
        return jsonify(error="Server busy, try again later"), 503

    released = False

    def _release_once() -> None:
        nonlocal released, slot_fd
        if not released:
            released = True
            _release_job_slot(slot_fd)
            slot_fd = None

    active_job_fd: int | None = None
    worker_started = False

    try:
        data = request.get_json(force=True) or {}
        url = (data.get("url") or "").strip()
        resolution = data.get("resolution")
        extension = (data.get("extension") or "mp4").strip().lower()
        job_id = _sanitize_job_id(str(data.get("job_id") or ""))

        # Per-request Media Session metadata toggle. Absent => OFF (opt-in),
        # matching yt-dlp's own convention (no extra artifacts unless asked).
        # When on, the download writes a sidecar thumbnail + emits [meta]
        # title/artist for lock-screen use; when off, neither is produced
        # (zero extra cost, and existing API consumers see unchanged behavior).
        # Accepts a JSON bool or a truthy string ("1"/"true"/"yes"/"on").
        _meta_raw = data.get("metadata", False)
        if isinstance(_meta_raw, bool):
            write_metadata = _meta_raw
        else:
            write_metadata = str(_meta_raw).strip().lower() in {"1", "true", "yes", "y", "on"}

        if not url:
            _release_once()
            return jsonify(error="Missing 'url'"), 400

        try:
            url = _validate_url(url)
        except ValueError as e:
            _release_once()
            return jsonify(error=str(e)), 400

        if extension not in _ALLOWED_EXTENSIONS:
            _release_once()
            return jsonify(error=f"Invalid 'extension'. Allowed: {sorted(_ALLOWED_EXTENSIONS)}"), 400

        raw_urls = [u.strip() for u in url.split(",") if u.strip()]
        collection_job = _r2_enabled() and (
            len(raw_urls) > 1 or (len(raw_urls) == 1 and is_playlist_url(raw_urls[0]))
        )

        job_dir = _job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)

        # Keep a real reserve instead of waiting until the filesystem is within
        # 500 MB of full. Emergency cleanup is active-job aware.
        if _free_disk_mb() < MIN_FREE_DISK_MB:
            _emergency_cleanup()
            if _free_disk_mb() < MIN_FREE_DISK_MB:
                _release_once()
                return jsonify(error="Insufficient disk space. Try again shortly."), 507

        active_job_fd = _try_acquire_active_job_lock(job_dir)
        if active_job_fd is None:
            _release_once()
            return jsonify(error="This job_id is already active"), 409

        q: "queue.Queue[str]" = queue.Queue(maxsize=50000)
        done = threading.Event()
        enospc_seen = threading.Event()
        result: dict = {"path": None, "error": None, "r2_key": None, "r2_error": None}

        def push(line: str) -> None:
            text = str(line)
            if _is_enospc(text):
                enospc_seen.set()
            try:
                q.put_nowait(text)
            except Exception:
                pass

        def worker() -> None:
            disk_full_failure = False
            collection_state = _load_collection_state(job_dir) if collection_job else None

            # Same-job retries can already have R2 objects recorded from a prior
            # post-download upload attempt. Re-announce them so the SSE contract
            # remains complete; local media is still retained until final success.
            if collection_state is not None:
                for item in collection_state.get("tracks") or []:
                    if not isinstance(item, dict) or not item.get("r2_key"):
                        continue
                    push(f"[r2_track] key={item['r2_key']}")
                    if item.get("thumb_key"):
                        push(
                            f"[meta_thumb] media={item.get('filename', '')}\t"
                            f"key={item['thumb_key']}"
                        )

            try:
                path = download_video(
                    url=url,
                    resolution=resolution,
                    extension=extension,
                    out_dir=job_dir,
                    on_line=push,
                    write_metadata=write_metadata,
                    # R2 collection mode deliberately avoids callbacks in yt-dlp's
                    # hot path. Keep all media local and merely skip the full local
                    # ZIP; api.py handles R2 work after downloading has finished.
                    skip_collection_zip=collection_job,
                )
                result["path"] = path

                if _r2_enabled():
                    fname = os.path.basename(path) if path else ""
                    if fname:
                        if collection_job:
                            media_paths = _iter_collection_media(job_dir)
                            if not media_paths:
                                raise RuntimeError(
                                    "No playlist/multi-URL media files found after download"
                                )

                            _upload_collection_tracks_from_local(
                                media_paths=media_paths,
                                job_dir=job_dir,
                                job_id=job_id,
                                state=collection_state,
                                push=push,
                            )

                            # Build the final ZIP straight from the retained local
                            # tracks into R2. This avoids both a second full-size
                            # local ZIP copy and an R2->VPS->R2 read-back pass.
                            result["r2_key"] = _stream_local_collection_zip_to_r2(
                                media_paths=media_paths,
                                job_id=job_id,
                                zip_filename=fname,
                                push=push,
                            )
                        else:
                            try:
                                def _on_pct(pct: float) -> None:
                                    push(f"[r2_upload] {pct:.2f}%")
                                result["r2_key"] = _upload_to_r2(
                                    local_path=path,
                                    job_id=job_id,
                                    filename=fname,
                                    on_progress=_on_pct,
                                )
                                _verify_r2_object(
                                    result["r2_key"], expected_size=os.path.getsize(path)
                                )
                                push("[r2_upload] 100.00%")
                                _upload_thumb_for(
                                    media_path=path, job_id=job_id, push=push,
                                )
                            except (BotoCoreError, ClientError, Exception) as e:
                                # Single-file jobs still have a complete local file,
                                # so preserve the existing local-fetch fallback.
                                result["r2_error"] = str(e)

                _write_result_meta(job_dir, path, r2_key=result.get("r2_key"))

                if result.get("r2_key"):
                    # Free large local media immediately while retaining tiny
                    # result.json for the normal DONE_TTL fetch/redirect window.
                    _purge_job_payload_keep_meta(job_dir)
                _schedule_delete_job_dir(job_dir, after_s=DONE_TTL_S)

            except Exception as e:
                result["error"] = str(e)
                disk_full_failure = enospc_seen.is_set() or _is_enospc(e)
                if disk_full_failure:
                    # ENOSPC is different from an ordinary failed download:
                    # retaining multi-GB partials only delays recovery, and the
                    # Render relay uses a fresh VPS job_id for its next attempt.
                    # Reclaim this failed job's payload immediately.
                    _purge_job_payload_keep_meta(job_dir)
                else:
                    # Normal failures keep partial files/archive/manifest for
                    # STALE_JOB_TTL_S so a same-job retry can resume.
                    pass
            finally:
                _release_active_job_lock(active_job_fd)
                _release_once()
                if disk_full_failure:
                    # The job is inactive now, so emergency cleanup may safely
                    # reclaim other abandoned/inactive jobs until the disk
                    # reserve is restored. Active jobs remain protected.
                    _emergency_cleanup()
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        worker_started = True

        def gen():
            yield f"data: [start] job_id={job_id}\n\n"
            last_keepalive = time.monotonic()

            while not done.is_set() or not q.empty():
                try:
                    line = q.get(timeout=0.5)
                    if line.startswith("[playlist_title] "):
                        continue
                    # Rewrite [meta] lines so the server's absolute filepath is
                    # reduced to a bare filename before leaving the VPS. Format
                    # in:  [meta] <abs_path>\t<title>\t<artist>
                    # out: [meta] media=<filename>\ttitle=<title>\tartist=<artist>
                    if line.startswith("[meta] "):
                        line = _rewrite_meta_line(line)
                        if not line:
                            continue
                    yield f"data: {line}\n\n"
                except queue.Empty:
                    if (time.monotonic() - last_keepalive) >= 15:
                        yield ": keep-alive\n\n"
                        last_keepalive = time.monotonic()
                    continue

            if result.get("error"):
                yield f"data: [error] {result['error']}\n\n"
                yield "data: [done]\n\n"
                return

            p = result.get("path") or ""
            fname = os.path.basename(p) if p else ""

            yield f"data: [ready] job_id={job_id}\n\n"
            if fname:
                yield f"data: [file] {fname}\n\n"

            if result.get("r2_key"):
                yield f"data: [r2] key={result['r2_key']}\n\n"
            elif result.get("r2_error"):
                yield f"data: [r2_error] {result['r2_error']}\n\n"

            # Media Session artwork without R2: the sidecar thumbnail is in the
            # job dir and served by /api/fetch/<job_id>/<thumb>. Announce it as a
            # fetchable filename so consumers can build that URL. (With R2 on,
            # the thumbnail was already uploaded and announced via [meta_thumb]
            # key=… during the upload phase, so we skip this to avoid dupes.)
            if not result.get("r2_key") and p:
                if fname.lower().endswith(".zip"):
                    # Playlist ZIP: announce each track's sidecar thumbnail by
                    # name. The tracks (and their thumbs) remain in the job dir
                    # and are fetchable individually via /api/fetch/<id>/<name>,
                    # the same way per-track title/artist already arrives via the
                    # [meta] lines emitted during download.
                    for track in _collect_track_files(job_dir, p):
                        t_thumb = _find_sidecar_thumb(track)
                        if t_thumb:
                            if track.lower().endswith(".mp3"):
                                _square_crop(t_thumb)
                            yield (
                                f"data: [meta_thumb] media={os.path.basename(track)}"
                                f"\tfile={os.path.basename(t_thumb)}\n\n"
                            )
                else:
                    thumb = _find_sidecar_thumb(p)
                    if thumb:
                        if (p or "").lower().endswith(".mp3"):
                            _square_crop(thumb)
                        yield f"data: [meta_thumb] media={fname}\tfile={os.path.basename(thumb)}\n\n"

            yield f"data: [fetch] /api/fetch/{job_id}\n\n"
            yield "data: [done]\n\n"

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return Response(
            stream_with_context(gen()), headers=headers,
            content_type="text/event-stream; charset=utf-8",
        )

    except Exception as e:
        if active_job_fd is not None and not worker_started:
            _release_active_job_lock(active_job_fd)
        _release_once()
        return jsonify(error=f"Download failed: {str(e)}"), 500



@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id: str):
    """
    Report durable VPS-side state for a job.

    This endpoint is intentionally independent of the original SSE connection.
    A relay/client that loses its stream can query the same job_id and wait for
    the existing worker instead of starting a duplicate download.
    """
    auth_error = _require_auth()
    if auth_error:
        return auth_error

    job_id = _sanitize_job_id(job_id)
    job_dir = _job_dir(job_id)

    # result.json is written only after the worker has finished its download and
    # post-download R2 work. Check it first so the tiny completion window between
    # writing metadata and releasing the active lock is still reported complete.
    meta = _read_result_meta(job_dir)
    if isinstance(meta, dict):
        filename = str(meta.get("filename") or "").strip()
        r2_key = str(meta.get("r2_key") or "").strip()

        tracks = []
        state = _load_collection_state(job_dir)
        for item in state.get("tracks") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("filename") or "").strip()
            key = str(item.get("r2_key") or "").strip()
            if not name or not key:
                continue
            row = {
                "filename": name,
                "r2_key": key,
                "size": int(item.get("size") or 0),
            }
            thumb_key = str(item.get("thumb_key") or "").strip()
            if thumb_key:
                row["thumb_key"] = thumb_key
            tracks.append(row)

        return jsonify(
            state="complete",
            job_id=job_id,
            filename=filename,
            r2_key=r2_key,
            tracks=tracks,
        ), 200

    if os.path.isdir(job_dir) and _job_is_active(job_dir):
        return jsonify(state="active", job_id=job_id), 200

    if os.path.isdir(job_dir):
        # The worker is gone and no completion metadata exists. Partial files may
        # remain for stale-job cleanup, but there is no live job to wait for.
        return jsonify(state="incomplete", job_id=job_id), 200

    return jsonify(state="missing", job_id=job_id), 200


@app.route("/api/fetch/<job_id>", methods=["GET"])
def fetch_job(job_id: str):
    """Serve the primary output file (the ZIP for playlists, the media file otherwise)."""
    auth_error = _require_auth()
    if auth_error:
        return auth_error
    job_id = _sanitize_job_id(job_id)
    job_dir = _job_dir(job_id)
    meta = _read_result_meta(job_dir)
    if not meta:
        return jsonify(error="Job not found or not finished yet"), 404

    path = meta.get("path") or ""
    filename = str(meta.get("filename") or os.path.basename(path) or "download.bin")

    if path and os.path.exists(path):
        response = send_file(
            path,
            mimetype=_guess_content_type(filename),
            as_attachment=True,
            download_name=filename,
        )

        # Local-only result: clean up after the bytes have been served.
        def _cleanup() -> None:
            _schedule_delete_job_dir(job_dir, after_s=0)
        response.call_on_close(_cleanup)
        return response

    r2_key = str(meta.get("r2_key") or "").strip()
    if r2_key:
        url = _r2_presigned_get(key=r2_key, filename=filename, as_attachment=True)
        if url:
            return redirect(url, code=302)

    return jsonify(error="File missing"), 404


@app.route("/api/fetch/<job_id>/<path:filename>", methods=["GET"])
def fetch_job_file(job_id: str, filename: str):
    """
    Serve an individual track from a playlist job for inline streaming.
    The frontend uses this to play tracks sequentially in the media player
    while the ZIP is available for bulk download via /api/fetch/<job_id>.
    """
    auth_error = _require_auth()
    if auth_error:
        return auth_error
    job_id = _sanitize_job_id(job_id)
    job_dir = _job_dir(job_id)

    # Prevent path traversal: only allow bare filenames inside job_dir.
    safe_name = os.path.basename(filename)
    if not safe_name:
        return jsonify(error="Invalid filename"), 400

    file_path = os.path.join(job_dir, safe_name)
    if os.path.isfile(file_path):
        return send_file(
            file_path,
            mimetype=_guess_content_type(safe_name),
            as_attachment=False,
            conditional=True,   # honours Range / If-Modified-Since for seeking
        )

    # After successful R2 finalization, collection payload is removed locally.
    # Keep the tiny manifest until DONE_TTL so per-file fetches can still redirect.
    state = _load_collection_state(job_dir)
    item = _state_track_by_filename(state, safe_name)
    if item and item.get("r2_key"):
        url = _r2_presigned_get(
            key=item["r2_key"], filename=safe_name, as_attachment=False
        )
        if url:
            return redirect(url, code=302)

    return jsonify(error="File not found"), 404


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(ok=True, in_use=_global_in_use(), capacity=MAX_CONCURRENT), 200


def main():
    validate_environment()
    print("Starting ytp-dl API server…")
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
