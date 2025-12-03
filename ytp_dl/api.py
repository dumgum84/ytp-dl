#!/usr/bin/env python3
from flask import Flask, request, send_file, jsonify
import os
from gevent.lock import Semaphore  # gevent-friendly semaphore
from .downloader import validate_environment, download_video

app = Flask(__name__)
DOWNLOAD_DIR = "/root"

# Concurrency cap (process-wide). With Gunicorn -w 1, this is an instance-wide cap.
MAX_CONCURRENT = int(os.environ.get("YTPDL_MAX_CONCURRENT", "1"))
_sem = Semaphore(MAX_CONCURRENT)

@app.route('/api/download', methods=['POST'])
def handle_download():
    if not _sem.acquire(blocking=False):
        # Busy: fail fast so the client can retry with backoff
        return jsonify(error="Server busy, try again later"), 503
    try:
        data = request.get_json(force=True)
        url = data.get("url")
        resolution = data.get("resolution")
        extension = data.get("extension")

        if not url:
            return jsonify(error="Missing 'url'"), 400

        filename = download_video(
            url=url,
            resolution=resolution,
            extension=extension,
        )

        if filename and os.path.exists(filename):
            return send_file(filename, as_attachment=True)
        else:
            return jsonify(error="Download failed"), 500

    except RuntimeError as e:
        msg = str(e)
        if "Mullvad is not logged in" in msg:
            return jsonify(error=msg), 503
        return jsonify(error=f"Download failed: {msg}"), 500
    except Exception as e:
        return jsonify(error=f"Download failed: {str(e)}"), 500
    finally:
        _sem.release()

@app.route('/healthz', methods=['GET'])
def healthz():
    # Quick health endpoint
    return jsonify(ok=True, in_use=(MAX_CONCURRENT - _sem.counter), capacity=MAX_CONCURRENT), 200

def main():
    validate_environment()
    print("Starting ytp-dl API server...")
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    main()
