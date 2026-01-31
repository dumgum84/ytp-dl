#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import tempfile
import time

from flask import Flask, request, send_file, jsonify
from gevent.lock import Semaphore

from .downloader import validate_environment, download_video

app = Flask(__name__)

BASE_DOWNLOAD_DIR = os.environ.get("YTPDL_JOB_BASE_DIR", "/root/ytpdl_jobs")
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT = int(os.environ.get("YTPDL_MAX_CONCURRENT", "1"))
_sem = Semaphore(MAX_CONCURRENT)

# Failsafe: delete abandoned job dirs older than this many seconds.
# (keep 21600 if you prefer; 3600 is fine too)
STALE_JOB_TTL_S = int(os.environ.get("YTPDL_STALE_JOB_TTL_S", "3600"))

_ALLOWED_EXTENSIONS = {"mp3", "mp4", "best"}


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


@app.route("/api/download", methods=["POST"])
def handle_download():
    _cleanup_stale_jobs()

    if not _sem.acquire(blocking=False):
        return jsonify(error="Server busy, try again later"), 503

    job_dir: str | None = None
    released = False

    def _release_once() -> None:
        nonlocal released
        if not released:
            released = True
            _sem.release()

    try:
        data = request.get_json(force=True)
        url = (data.get("url") or "").strip()
        resolution = data.get("resolution")

        # extension is now a "mode": mp3 | mp4 | best
        extension = (data.get("extension") or "mp4").strip().lower()

        if not url:
            _release_once()
            return jsonify(error="Missing 'url'"), 400

        if extension not in _ALLOWED_EXTENSIONS:
            _release_once()
            return jsonify(
                error=f"Invalid 'extension'. Allowed: {sorted(_ALLOWED_EXTENSIONS)}"
            ), 400

        job_dir = tempfile.mkdtemp(prefix="ytpdl_", dir=BASE_DOWNLOAD_DIR)

        # yt-dlp work (guarded by semaphore)
        filename = download_video(
            url=url,
            resolution=resolution,
            extension=extension,
            out_dir=job_dir,
        )

        if not (filename and os.path.exists(filename)):
            raise RuntimeError("Download failed")

        # Release semaphore as soon as yt-dlp is done.
        # Streaming the file should not block the next download job.
        _release_once()

        response = send_file(filename, as_attachment=True)

        # Cleanup directory after client finishes consuming the response.
        def _cleanup() -> None:
            try:
                if job_dir:
                    shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass

        response.call_on_close(_cleanup)
        return response

    except RuntimeError as e:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        _release_once()

        msg = str(e)
        if "Mullvad not logged in" in msg:
            return jsonify(error=msg), 503
        return jsonify(error=f"Download failed: {msg}"), 500

    except Exception as e:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        _release_once()
        return jsonify(error=f"Download failed: {str(e)}"), 500


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(ok=True, in_use=(MAX_CONCURRENT - _sem.counter), capacity=MAX_CONCURRENT), 200


def main():
    validate_environment()
    print("Starting ytp-dl API server...")
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
