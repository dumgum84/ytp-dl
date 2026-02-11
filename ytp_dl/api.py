#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from threading import BoundedSemaphore, Lock

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from .downloader import download_video, validate_environment

app = Flask(__name__)

BASE_DOWNLOAD_DIR = os.environ.get("YTPDL_JOB_BASE_DIR", "/root/ytpdl_jobs")
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT = int(os.environ.get("YTPDL_MAX_CONCURRENT", "1"))

# Thread-safe concurrency gate (caps actual download jobs).
_sem = BoundedSemaphore(MAX_CONCURRENT)

# Track in-flight jobs for /healthz reporting.
_in_use = 0
_in_use_lock = Lock()

# Failsafe: delete abandoned job dirs older than this many seconds.
STALE_JOB_TTL_S = int(os.environ.get("YTPDL_STALE_JOB_TTL_S", "3600"))

_ALLOWED_EXTENSIONS = {"mp3", "mp4", "best"}


def _sanitize_job_id(job_id: str) -> str:
    # Keep job_id filesystem-safe (and prevent traversal).
    job_id = (job_id or "").strip()
    safe = "".join(c for c in job_id if c.isalnum() or c in ("-", "_"))
    return safe or str(int(time.time() * 1000))


def _job_dir(job_id: str) -> str:
    safe = _sanitize_job_id(job_id)
    return os.path.join(BASE_DOWNLOAD_DIR, f"ytpdl_{safe}")


def _write_result_meta(job_dir: str, path: str) -> None:
    try:
        meta_path = os.path.join(job_dir, "result.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "path": os.path.abspath(path),
                    "filename": os.path.basename(path),
                    "ts": int(time.time()),
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


def _cleanup_stale_jobs() -> None:
    now = time.time()
    try:
        for name in os.listdir(BASE_DOWNLOAD_DIR):
            p = os.path.join(BASE_DOWNLOAD_DIR, name)
            if not os.path.isdir(p):
                continue
            try:
                age = now - os.path.getmtime(p)
            except Exception:
                continue
            if age > STALE_JOB_TTL_S:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


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


@app.route("/api/download", methods=["POST"])
def handle_download():
    """
    Streams real-time yt-dlp stdout lines as SSE `data:` events.

    When finished, the client (or your Render relay) fetches the file via:
      GET /api/fetch/<job_id>
    """
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

        if extension not in _ALLOWED_EXTENSIONS:
            _release_once()
            return jsonify(
                error=f"Invalid 'extension'. Allowed: {sorted(_ALLOWED_EXTENSIONS)}"
            ), 400

        job_dir = _job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)

        q: "queue.Queue[str]" = queue.Queue(maxsize=5000)
        done = threading.Event()
        result: dict = {"path": None, "error": None}

        def push(line: str) -> None:
            # best-effort; drop if client is too slow
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
                _write_result_meta(job_dir, path)
            except Exception as e:
                result["error"] = str(e)
            finally:
                _release_once()
                done.set()

        threading.Thread(target=worker, daemon=True).start()

        def gen():
            # Initial line so clients see the stream immediately.
            yield f"data: [start] job_id={job_id}\n\n"
            last_keepalive = time.monotonic()

            while not done.is_set() or not q.empty():
                try:
                    line = q.get(timeout=0.5)
                    yield f"data: {line}\n\n"
                except queue.Empty:
                    # occasional comment keepalive for picky proxies (NOT a data event)
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
            yield f"data: [file] {fname}\n\n"
            yield f"data: [fetch] /api/fetch/{job_id}\n\n"
            yield "data: [done]\n\n"

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(
            stream_with_context(gen()),
            headers=headers,
            content_type="text/event-stream",
        )

    except Exception as e:
        _release_once()
        return jsonify(error=f"Download failed: {str(e)}"), 500


@app.route("/api/fetch/<job_id>", methods=["GET"])
def fetch_job(job_id: str):
    job_id = _sanitize_job_id(job_id)
    job_dir = _job_dir(job_id)
    meta = _read_result_meta(job_dir)
    if not meta:
        return jsonify(error="Job not found or not finished yet"), 404

    path = meta.get("path") or ""
    if not path or not os.path.exists(path):
        return jsonify(error="File missing"), 404

    response = send_file(path, as_attachment=True)

    def _cleanup() -> None:
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass

    response.call_on_close(_cleanup)
    return response


@app.route("/healthz", methods=["GET"])
def healthz():
    with _in_use_lock:
        in_use = _in_use
    return jsonify(ok=True, in_use=in_use, capacity=MAX_CONCURRENT), 200


def main():
    validate_environment()
    print("Starting ytp-dl API server...")
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
