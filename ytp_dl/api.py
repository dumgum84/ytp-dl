#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
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


# ─── Playlist ZIP sibling helper ─────────────────────────────────────────────

def _zip_sibling(result_path: str) -> str | None:
    """Return the ZIP basename if one exists alongside the result file.
    Returns None when the result IS a zip (no separate sibling exists)."""
    if not result_path:
        return None
    # If the primary result is itself a ZIP, there is no sibling.
    if result_path.lower().endswith(".zip"):
        return None
    stem, _ = os.path.splitext(result_path)
    zip_path = stem + ".zip"
    if os.path.isfile(zip_path):
        return os.path.basename(zip_path)
    return None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def handle_download():
    """
    Streams real-time yt-dlp stdout as SSE events.

    For playlists, creates a concat file (player preview) + ZIP (individual
    tracks). The ZIP is kept for YTPDL_PLAYLIST_DONE_TTL_S seconds (default
    600) so Render can fetch it before cleanup.

    SSE event summary
    -----------------
    [start]     job_id=<id>
    <yt-dlp lines>
    [zip_file]  <zip filename>   (playlist only)
    [ready]     job_id=<id>
    [file]      <concat filename>
    [r2_upload] XX.XX%           (if R2 enabled on VPS)
    [r2]        key=<key>        (if R2 enabled on VPS)
    [fetch]     /api/fetch/<id>
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
                )
                result["path"] = path

                if _r2_enabled():
                    try:
                        fname = os.path.basename(path) if path else ""
                        if fname:
                            def _on_pct(pct: float) -> None:
                                push(f"[r2_upload] {pct:.2f}%")
                            result["r2_key"] = _upload_to_r2(
                                local_path=path,
                                job_id=job_id,
                                filename=fname,
                                on_progress=_on_pct,
                            )
                            push("[r2_upload] 100.00%")
                    except (BotoCoreError, ClientError, Exception) as e:
                        result["r2_error"] = str(e)

                _write_result_meta(job_dir, path, r2_key=result.get("r2_key"))

                # Use a longer TTL when a playlist ZIP sibling exists so
                # Render has time to make a second fetch for the ZIP.
                zip_name = _zip_sibling(path)
                result["zip_name"] = zip_name
                ttl = int(os.environ.get("YTPDL_PLAYLIST_DONE_TTL_S", "600")) if zip_name else DONE_TTL_S
                _schedule_delete_job_dir(job_dir, after_s=ttl)

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

            # Emit ZIP name so Render can fetch it as a separate file.
            zip_name = result.get("zip_name")
            if zip_name:
                yield f"data: [zip_file] {zip_name}\n\n"

            yield f"data: [ready] job_id={job_id}\n\n"
            if fname:
                yield f"data: [file] {fname}\n\n"

            if result.get("r2_key"):
                yield f"data: [r2] key={result['r2_key']}\n\n"
            elif result.get("r2_error"):
                yield f"data: [r2_error] {result['r2_error']}\n\n"

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
    """Serve the primary output file (concat media file for playlists, single file otherwise)."""
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

    # Only delete immediately when there is no ZIP sibling that Render still
    # needs to fetch.  Playlist jobs use TTL-based deletion instead.
    if _zip_sibling(path) is None:
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
