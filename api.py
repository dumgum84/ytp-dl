#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import zipfile
import mimetypes
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Callable
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from .downloader import download_video, validate_environment, is_playlist_url

app = Flask(__name__)

BASE_DOWNLOAD_DIR = os.environ.get("YTPDL_JOB_BASE_DIR", "/root/ytpdl_jobs")
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT = int(os.environ.get("YTPDL_MAX_CONCURRENT", "1"))

_sem = BoundedSemaphore(MAX_CONCURRENT)
_in_use = 0
_in_use_lock = Lock()

STALE_JOB_TTL_S = int(os.environ.get("YTPDL_STALE_JOB_TTL_S", "3600"))
DONE_TTL_S = int(os.environ.get("YTPDL_DONE_TTL_S", "300"))
MIN_FREE_DISK_MB = int(os.environ.get("YTPDL_MIN_FREE_DISK_MB", "500"))
CLEANUP_INTERVAL_S = int(os.environ.get("YTPDL_CLEANUP_INTERVAL_S", "60"))

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


def _schedule_delete_job_dir(job_dir: str, *, after_s: int) -> None:
    def _worker():
        try:
            time.sleep(max(0, int(after_s)))
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


def _cleanup_stale_jobs() -> None:
    now = time.time()
    try:
        for name in os.listdir(BASE_DOWNLOAD_DIR):
            p = os.path.join(BASE_DOWNLOAD_DIR, name)
            if not os.path.isdir(p):
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


def _free_disk_mb() -> float:
    """Return free disk space in MB for BASE_DOWNLOAD_DIR's filesystem."""
    try:
        st = os.statvfs(BASE_DOWNLOAD_DIR)
        return (st.f_bavail * st.f_frsize) / (1024 * 1024)
    except Exception:
        return float("inf")


def _emergency_cleanup() -> None:
    """Aggressively delete all job dirs oldest-first until MIN_FREE_DISK_MB is free."""
    try:
        dirs = []
        for name in os.listdir(BASE_DOWNLOAD_DIR):
            p = os.path.join(BASE_DOWNLOAD_DIR, name)
            if os.path.isdir(p):
                try:
                    dirs.append((os.path.getmtime(p), p))
                except Exception:
                    pass
        dirs.sort()  # oldest first
        for _, p in dirs:
            if _free_disk_mb() >= MIN_FREE_DISK_MB:
                break
            shutil.rmtree(p, ignore_errors=True)
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


# Start background cleanup thread on module load.
threading.Thread(target=_background_cleanup_worker, daemon=True).start()


def _try_acquire_job_slot() -> bool:
    global _in_use
    if not _sem.acquire(blocking=False):
        return False
    with _in_use_lock:
        _in_use += 1
    return True


def _release_job_slot() -> None:
    global _in_use
    with _in_use_lock:
        if _in_use > 0:
            _in_use -= 1
    _sem.release()


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
    key = f"{_sanitize_job_id(job_id)}/{filename}"
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


# ─── Playlist track upload helpers ───────────────────────────────────────────

def _collect_track_files(job_dir: str, zip_path: str) -> list[str]:
    """
    Find the individual media files of a playlist/multi-URL job on disk,
    restricted to files that are actually members of the job's ZIP — so the
    uploaded track set is exactly what extracting the ZIP would yield.
    (_create_zip stores members by basename, and these are the same source
    files.) Stray artifacts in the job dir are ignored.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = {os.path.basename(n) for n in zf.namelist()}
    except Exception:
        members = set()
    if not members:
        return []

    out: list[str] = []
    zip_abs = os.path.abspath(zip_path)
    for root, dirs, files in os.walk(job_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in files:
            if n not in members:
                continue
            p = os.path.join(root, n)
            if os.path.abspath(p) == zip_abs:
                continue
            out.append(p)
    return sorted(out, key=lambda p: os.path.basename(p).lower())


def _upload_playlist_tracks(*, job_dir: str, zip_path: str, job_id: str, push) -> bool:
    """
    Upload each individual track to R2 and emit [r2_track] key=<key> per
    success so Render can map tracks without fetching/extracting the ZIP.
    Returns True when every track uploaded; on any failure emits
    [r2_tracks_incomplete] so Render falls back to its fetch+extract path.
    """
    ok = True
    for p in _collect_track_files(job_dir, zip_path):
        name = os.path.basename(p)
        try:
            def _pct(v: float) -> None:
                push(f"[r2_upload] {v:.2f}%")
            key = _upload_to_r2(local_path=p, job_id=job_id, filename=name, on_progress=_pct)
            push("[r2_upload] 100.00%")
            push(f"[r2_track] key={key}")
            # Upload this track's sidecar thumbnail for Media Session artwork.
            _upload_thumb_for(media_path=p, job_id=job_id, push=push)
        except Exception as e:
            ok = False
            push(f"[r2_error] Track upload failed ({name}): {e}")
    if not ok:
        push("[r2_tracks_incomplete]")
    return ok



# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def handle_download():
    """
    Streams real-time yt-dlp stdout as SSE events.

    Single URLs produce one media file. Playlists / multi-URL jobs produce a
    ZIP of individual tracks as the primary result; with R2 enabled, every
    track is uploaded to R2 (announced via [r2_track]) before the ZIP itself,
    so the client never needs to fetch + extract the ZIP.

    SSE event summary
    -----------------
    [start]      job_id=<id>
    [total_items] <n>            (playlist/multi only)
    <yt-dlp lines>
    [meta]       media=<name>\ttitle=<t>\tartist=<a>   (per file - lock screen)
    [r2_upload]  XX.XX%          (R2 only - per track, then the result file)
    [r2_track]   key=<key>       (R2 + playlist/multi only - one per track)
    [meta_thumb] media=<name>\tkey=<key>   (R2 on - artwork uploaded to R2)
                 media=<name>\tfile=<thumb_filename>  (R2 off - fetch via /api/fetch)
    [r2_tracks_incomplete]       (R2 + playlist/multi only - a track failed)
    [ready]      job_id=<id>
    [file]       <filename>      (the media file, or the ZIP for playlists)
    [r2]         key=<key>       (R2 only - the result file's key)
    [fetch]      /api/fetch/<id>
    [done]
    """
    if not _is_authorized():
        if _is_blocked_ua():
            return jsonify(error="Forbidden"), 403
        return jsonify(error="Unauthorized"), 401

    _cleanup_stale_jobs()

    if not _try_acquire_job_slot():
        return jsonify(error="Server busy, try again later"), 503

    released = False

    def _release_once() -> None:
        nonlocal released
        if not released:
            released = True
            _release_job_slot()

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

        job_dir = _job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)

        # Refuse early if disk is critically low — better than failing mid-download.
        if _free_disk_mb() < MIN_FREE_DISK_MB:
            _emergency_cleanup()
            if _free_disk_mb() < MIN_FREE_DISK_MB:
                _release_once()
                return jsonify(error=f"Insufficient disk space. Try again shortly."), 507

        q: "queue.Queue[str]" = queue.Queue(maxsize=50000)
        done = threading.Event()
        result: dict = {"path": None, "error": None, "r2_key": None, "r2_error": None}

        def push(line: str) -> None:
            try:
                q.put_nowait(str(line))
            except Exception:
                pass

        def worker() -> None:
            try:
                path = download_video(
                    url=url,
                    resolution=resolution,
                    extension=extension,
                    out_dir=job_dir,
                    on_line=push,
                    write_metadata=write_metadata,
                )
                result["path"] = path

                if _r2_enabled():
                    try:
                        fname = os.path.basename(path) if path else ""
                        if fname:
                            # Playlist/multi result is a ZIP: upload the
                            # individual tracks first and announce their keys
                            # so Render never needs to fetch + extract the ZIP.
                            if fname.lower().endswith(".zip"):
                                _upload_playlist_tracks(
                                    job_dir=job_dir, zip_path=path,
                                    job_id=job_id, push=push,
                                )
                            def _on_pct(pct: float) -> None:
                                push(f"[r2_upload] {pct:.2f}%")
                            result["r2_key"] = _upload_to_r2(
                                local_path=path,
                                job_id=job_id,
                                filename=fname,
                                on_progress=_on_pct,
                            )
                            push("[r2_upload] 100.00%")
                            # Upload the sidecar thumbnail (single-file only;
                            # playlist tracks handle their own above). Skipped
                            # automatically for ZIP results since those have no
                            # sidecar thumbnail of their own.
                            if not fname.lower().endswith(".zip"):
                                _upload_thumb_for(
                                    media_path=path, job_id=job_id, push=push,
                                )
                    except (BotoCoreError, ClientError, Exception) as e:
                        result["r2_error"] = str(e)

                _write_result_meta(job_dir, path, r2_key=result.get("r2_key"))

                _schedule_delete_job_dir(job_dir, after_s=DONE_TTL_S)

            except Exception as e:
                result["error"] = str(e)
                # Clean up immediately on failure — don't leave partial files on disk.
                _schedule_delete_job_dir(job_dir, after_s=0)
            finally:
                _release_once()
                done.set()

        threading.Thread(target=worker, daemon=True).start()

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
        _release_once()
        return jsonify(error=f"Download failed: {str(e)}"), 500


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
    if not path or not os.path.exists(path):
        return jsonify(error="File missing"), 404

    filename = os.path.basename(path)
    response = send_file(
        path,
        mimetype=_guess_content_type(filename),
        as_attachment=True,
        download_name=filename,
    )

    # The served file is the job's only deliverable — clean up right after.
    def _cleanup() -> None:
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    response.call_on_close(_cleanup)

    return response


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
    if not os.path.isfile(file_path):
        return jsonify(error="File not found"), 404

    return send_file(
        file_path,
        mimetype=_guess_content_type(safe_name),
        as_attachment=False,
        conditional=True,   # honours Range / If-Modified-Since for seeking
    )


@app.route("/healthz", methods=["GET"])
def healthz():
    with _in_use_lock:
        in_use = _in_use
    return jsonify(ok=True, in_use=in_use, capacity=MAX_CONCURRENT), 200


def main():
    validate_environment()
    print("Starting ytp-dl API server…")
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
