# ytp-dl

[![PyPI version](https://img.shields.io/pypi/v/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Python Support](https://img.shields.io/pypi/pyversions/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![License](https://img.shields.io/pypi/l/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Downloads](https://img.shields.io/pypi/dm/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)

Privacy-focused media downloader API for Linux VPS deployments — powered by yt-dlp, routed through Mullvad VPN, with real-time SSE log streaming.

---

## Features

- Privacy-first: connect/disconnect Mullvad per download
- Smart quality selection: prefers 1080p H.264 + AAC (no transcoding)
- Best format mode: let yt-dlp pick the highest quality available (adaptive, no transcoding)
- Audio extraction: downloads best audio stream as MP3 with embedded cover art and metadata (re-encodes only when the source isn't already MP3)
- Playlist support: YouTube playlists, SoundCloud sets, Bilibili series, Odysee playlists — downloads all tracks and produces a ZIP of the individual files
- Streaming HTTP API:
  - `POST /api/download` streams real-time yt-dlp output as Server-Sent Events (SSE)
  - `GET  /api/fetch/<job_id>` fetches the finished file
  - `GET  /api/fetch/<job_id>/<filename>` fetches a specific file from a job by name
- Optional R2 upload: upload completed files to Cloudflare R2 and emit `data: [r2] key=<object_key>` — playlist/multi jobs also upload every individual track, announced via `data: [r2_track] key=<object_key>`
- Stable public API under VPN cycling: exclude the API port from the tunnel (nftables marks) + policy routing
- VPS-ready: automated installer script for Ubuntu

---

## Installation

```bash
pip install ytp-dl==2026.6.12
```

### Requirements

- Linux (tested on Ubuntu 24.04/25.04)
- Mullvad CLI installed and configured
- FFmpeg (audio/video handling)
- Deno (system-wide; required by yt-dlp for modern YouTube extraction)
- Python 3.8+

Notes:
- yt-dlp expects **Deno** to be available on `PATH` to run its JavaScript-based extraction logic.

---

## Quick start

A download is a **two-phase** flow:

1) Start the job and stream logs:
- `POST /api/download` (SSE)

2) Fetch the finished file:
- `GET /api/fetch/<job_id>`

### Start a best-quality download

```bash
curl -N --http1.1 \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -X POST "http://YOUR_VPS_IP:5000/api/download" \
  --data-binary '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","extension":"best","job_id":"demo3"}'
```

### Start a video download (1080p MP4)

Choose a `job_id` you can reuse for the follow-up fetch (only letters, numbers, `-`, `_`).

```bash
curl -N --http1.1 \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -X POST "http://YOUR_VPS_IP:5000/api/download" \
  --data-binary '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","extension":"mp4","resolution":1080,"job_id":"demo1"}'
```

### Start an audio download (MP3)

```bash
curl -N --http1.1 \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -X POST "http://YOUR_VPS_IP:5000/api/download" \
  --data-binary '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","extension":"mp3","job_id":"demo2"}'
```


### Fetch the finished file

When the SSE stream emits a line like:

```
data: [fetch] /api/fetch/demo1
```

Fetch the file using the same `job_id`:

```bash
curl -L -O -J "http://YOUR_VPS_IP:5000/api/fetch/demo1"
```

- `-O -J` tells curl to use the filename from `Content-Disposition`.

### Windows (CMD.exe) examples

Start the SSE stream:

```bat
curl -N --http1.1 ^
  -H "Accept: text/event-stream" ^
  -H "Content-Type: application/json" ^
  -X POST "http://YOUR_VPS_IP:5000/api/download" ^
  --data-binary "{\"url\":\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\",\"extension\":\"mp4\",\"resolution\":1080,\"job_id\":\"demo1\"}"
```

Note: PowerShell handles JSON escaping differently — wrap the `--data-binary` value in single quotes and use standard double quotes inside.

Fetch the finished file:

```bat
curl -L -O -J "http://YOUR_VPS_IP:5000/api/fetch/demo1"
```

---

## Python client

A minimal Python script that replicates the two-phase download flow (SSE stream → fetch) and handles server-side errors with retries and playlist resume.

### Usage

```bash
# Single video — MP4
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --extension mp4 --resolution 1080 --out-dir .

# Audio — MP3
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://soundcloud.com/artist/track" --extension mp3 --out-dir .

# Playlist with retries
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://www.youtube.com/playlist?list=PLxxx" --extension mp4 --out-dir ./downloads
```

Set `YTPDL_BASE` in your environment to avoid passing `--base` every time:

```bash
export YTPDL_BASE="http://YOUR_VPS_IP:5000"
python3 ytp-dl.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--base` | `$YTPDL_BASE` or `http://127.0.0.1:5000` | VPS base URL |
| `--url` | *(required)* | Media URL to download |
| `--extension` | `mp4` | `mp4`, `mp3`, or `best` |
| `--resolution` | `1080` | Max height cap (ignored for `mp3`) |
| `--out-dir` | `.` | Directory to save the file |
| `--max-retries` | `5` | Retries on server-side error, e.g. rate-limit |
| `--retry-delay` | `1` | Seconds before first retry; doubles each attempt (max 60s) |
| `--retry-factor` | `2` | Backoff multiplier applied after each retry |
| `--connect-timeout` | `15` | Connection timeout in seconds |
| `--read-timeout` | `300` | Read timeout in seconds |

### Retry behavior

On any server-side error (rate-limit, VPN cycle, etc.), the script retries automatically up to `--max-retries` times with exponential backoff (`--retry-delay` × `--retry-factor` each attempt, capped at 60s). It reuses the same `job_id` on each attempt — for playlist downloads, the VPS `.ytdlp-archive` skips already-completed tracks so nothing is re-downloaded.

### Installation

```bash
pip install requests
```

Save the script as `ytp-dl.py`, then run it with the usage examples above.

### Script

```python
#!/usr/bin/env python3
"""
ytp-dl Python client (SSE + fetch)

Flow:
  1) POST /api/download         -> streams yt-dlp logs as Server-Sent Events (SSE)
  2) GET  /api/fetch/<job_id>   -> downloads the finished file

On rate-limit ([error] from server), re-POSTs with the same job_id so the
VPS .ytdlp-archive skips already-downloaded tracks (playlist resume).

Requirements:
  pip install requests
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote

import requests


_FETCH_RX    = re.compile(r"(/api/fetch/[A-Za-z0-9_\-]+)")
_START_JOB_RX = re.compile(r"\[start\]\s+job_id=([A-Za-z0-9_\-]+)")

# Lines that are internal protocol signals — suppress from printed output.
_SUPPRESS_PREFIXES = (
    "[playlist_count] ",
    "[playlist_title] ",
    "[r2_track] ",
    "[r2_tracks_incomplete]",
    "[ready] ",
    "[file] ",
)


@dataclass(frozen=True)
class Config:
    base: str
    url: str
    extension: str
    resolution: Optional[int]
    job_id: str
    out_dir: str
    connect_timeout_s: float
    read_timeout_s: float
    max_retries: int
    retry_delay_s: float
    retry_factor: float


def _normalize_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing --base")
    return base


def _parse_fetch_path(msg: str) -> Optional[str]:
    m = _FETCH_RX.search(msg or "")
    return m.group(1) if m else None


def _parse_job_id(msg: str) -> Optional[str]:
    m = _START_JOB_RX.search(msg or "")
    return m.group(1) if m else None


def _filename_from_content_disposition(cd: str) -> Optional[str]:
    cd = (cd or "").strip()
    if not cd:
        return None

    # filename*
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, flags=re.I)
    if m:
        v = m.group(1).strip().strip('"')
        _, _, tail = v.partition("''")
        raw = tail or v
        try:
            name = unquote(raw)
            return os.path.basename(name)
        except Exception:
            pass

    # filename
    m = re.search(r'filename\s*=\s*("?)([^";]+)\1', cd, flags=re.I)
    if m:
        return os.path.basename(m.group(2).strip())

    return None


def _safe_default_filename(ext: str) -> str:
    ext = (ext or "").lower().strip().lstrip(".")
    if ext in {"mp3", "mp4"}:
        return f"download.{ext}"
    return "download.bin"


def _auto_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def stream_logs_and_get_fetch_path(
    cfg: Config,
) -> tuple[Optional[str], str, Optional[str]]:
    """
    Stream SSE logs from the VPS download endpoint.

    Returns (fetch_path, resolved_job_id, error_msg).
    error_msg is non-None when the server emitted [error].
    For playlists the fetched file is the ZIP of individual tracks.
    """
    payload: dict = {"url": cfg.url, "extension": cfg.extension, "job_id": cfg.job_id}
    if cfg.resolution is not None:
        payload["resolution"] = int(cfg.resolution)

    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}

    fetch_path: Optional[str] = None
    resolved_job_id: str = cfg.job_id
    error_msg: Optional[str] = None

    with requests.post(
        f"{cfg.base}/api/download",
        json=payload,
        stream=True,
        headers=headers,
        timeout=(cfg.connect_timeout_s, cfg.read_timeout_s),
    ) as r:
        r.raise_for_status()

        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = (raw or "").strip("\r")
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue

            msg = line[5:].lstrip()
            if not msg:
                continue

            # Capture job id from [start] event.
            jid = _parse_job_id(msg)
            if jid:
                resolved_job_id = jid

            # Capture fetch hint.
            if not fetch_path:
                fp = _parse_fetch_path(msg)
                if fp:
                    fetch_path = fp

            # Detect server-side error.
            if msg.startswith("[error]"):
                error_msg = msg[len("[error]"):].strip()
                print(msg, flush=True)
                continue

            # Suppress internal protocol events from output.
            if any(msg.startswith(p) for p in _SUPPRESS_PREFIXES):
                continue

            # Suppress [done] — protocol signal only.
            if msg.startswith("[done]") or msg == "All downloads complete.":
                continue

            print(msg, flush=True)

    return fetch_path, resolved_job_id, error_msg


def fetch_file(cfg: Config, fetch_path: str) -> str:
    os.makedirs(cfg.out_dir, exist_ok=True)

    with requests.get(
        f"{cfg.base}{fetch_path}",
        stream=True,
        timeout=(cfg.connect_timeout_s, cfg.read_timeout_s),
        headers={"Connection": "close"},
    ) as r:
        r.raise_for_status()

        cd = r.headers.get("Content-Disposition", "")
        filename = _filename_from_content_disposition(cd) or _safe_default_filename(cfg.extension)
        out_path = os.path.abspath(os.path.join(cfg.out_dir, filename))

        if os.path.exists(out_path):
            base, ext = os.path.splitext(out_path)
            i = 1
            while os.path.exists(f"{base}_{i}{ext}"):
                i += 1
            out_path = f"{base}_{i}{ext}"

        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)

    return out_path


def parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(description="ytp-dl client (SSE + fetch)")
    p.add_argument(
        "--base",
        default=os.environ.get("YTPDL_BASE", "http://127.0.0.1:5000"),
        help="Base URL, e.g. http://YOUR_VPS_IP:5000 (env: YTPDL_BASE)",
    )
    p.add_argument("--url", required=True, help="Media URL to download")
    p.add_argument("--extension", default="mp4", choices=["mp4", "mp3", "best"],
                   help="Download mode")
    p.add_argument("--resolution", type=int, default=1080,
                   help="Max height cap (default: 1080)")
    p.add_argument("--out-dir", default=".", help="Directory to save the fetched file")
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=300.0)
    p.add_argument("--max-retries", type=int, default=5,
                   help="Max retries on server-side error, e.g. rate-limit (default: 5)")
    p.add_argument("--retry-delay", type=float, default=1.0,
                   help="Seconds before first retry; doubles each attempt (default: 1)")
    p.add_argument("--retry-factor", type=float, default=2.0,
                   help="Backoff multiplier applied after each retry (default: 2)")

    a = p.parse_args(argv)

    return Config(
        base=_normalize_base(a.base),
        url=a.url,
        extension=a.extension,
        resolution=a.resolution if a.extension != "mp3" else None,
        job_id=_auto_job_id(),
        out_dir=a.out_dir,
        connect_timeout_s=a.connect_timeout,
        read_timeout_s=a.read_timeout,
        max_retries=a.max_retries,
        retry_delay_s=a.retry_delay,
        retry_factor=a.retry_factor,
    )


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)

    attempt = 0
    delay   = cfg.retry_delay_s

    while True:
        attempt += 1

        try:
            fetch_path, resolved_job_id, error_msg = stream_logs_and_get_fetch_path(cfg)
        except requests.RequestException as e:
            print(f"ERROR: Request failed: {e}", file=sys.stderr)
            return 1

        # Success path — no server error.
        if error_msg is None:
            break

        # Server signalled an error (rate-limit, VPN cycle needed, etc.)
        if attempt >= cfg.max_retries:
            print(
                f"ERROR: Server error after {attempt} attempt(s): {error_msg}",
                file=sys.stderr,
            )
            return 1

        print(
            f"[retry] Attempt {attempt}/{cfg.max_retries} failed: {error_msg}",
            file=sys.stderr,
        )
        print(f"[retry] Retrying in {delay:.0f}s...", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * cfg.retry_factor, 60.0)   # exponential backoff, cap at 60s

        # Keep the same job_id so the VPS .ytdlp-archive resumes the playlist.

    if not fetch_path:
        fetch_path = f"/api/fetch/{resolved_job_id}"

    try:
        out = fetch_file(cfg, fetch_path)
    except requests.RequestException as e:
        print(f"ERROR: Fetch failed: {e}", file=sys.stderr)
        return 1

    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

```

---

## Configuration

Runtime config lives in `/etc/default/ytp-dl-api` (the installer creates it). Edit the file and restart the service to apply changes.

### Installer-only variables

| Variable | Description | Default |
|---|---|---:|
| `PORT` | API server port | `5000` |
| `APP_DIR` | Installation directory | `/opt/yt-dlp-mullvad` |
| `MV_ACCOUNT` | Mullvad account number (required; one-time login) | *(empty)* |

### Runtime variables

These are read from `/etc/default/ytp-dl-api`. You can also export any of them before running the installer to pre-seed that file.

| Variable | Description | Default |
|---|---|---:|
| `YTPDL_VENV` | Path to virtualenv for ytp-dl | `/opt/yt-dlp-mullvad/venv` |
| `YTPDL_MULLVAD_LOCATION` | Mullvad relay location code | `us` |
| `YTPDL_MAX_CONCURRENT` | Maximum concurrent download jobs | `1` |
| `YTPDL_DONE_TTL_S` | Seconds to keep a completed job dir before deletion | `300` |
| `YTPDL_STALE_JOB_TTL_S` | Seconds before an unfinished/unfetched job dir is force-deleted | `3600` |
| `YTPDL_MIN_FREE_DISK_MB` | Minimum free disk MB — new jobs refused and emergency cleanup triggered below this threshold | `500` |
| `YTPDL_CLEANUP_INTERVAL_S` | How often the background cleanup thread runs in seconds | `60` |
| `YTPDL_JOB_TIMEOUT_S` | Hard kill timeout for a single-file yt-dlp process | `1800` |
| `YTPDL_PLAYLIST_JOB_TIMEOUT_S` | Hard kill timeout for a playlist yt-dlp process | `21600` |
| `GUNICORN_WORKERS` | Gunicorn worker processes | `1` |
| `GUNICORN_THREADS` | Threads per Gunicorn worker | `4` |
| `YTPDL_R2_UPLOAD` | Upload completed files to R2 | `0` |
| `R2_ENDPOINT` | R2 endpoint (no bucket suffix) | *(empty)* |
| `R2_BUCKET` | R2 bucket name | *(empty)* |
| `R2_ACCESS_KEY_ID` | R2 uploader access key id | *(empty)* |
| `R2_SECRET_ACCESS_KEY` | R2 uploader secret access key | *(empty)* |
| `AWS_EC2_METADATA_DISABLED` | Disable EC2 metadata fetch | `true` |
| `YTPDL_VPS_API_TOKEN` | Shared secret to authenticate requests to this API. Must be set to the same value in client-side env. Leave empty to disable auth. | *(empty)* |

To change runtime configuration:

```bash
sudo nano /etc/default/ytp-dl-api
sudo systemctl restart ytp-dl-api
```

Keep secrets (e.g. `R2_SECRET_ACCESS_KEY`) on the server only — do not commit them to repos or READMEs.

---

## Managing your VPS service

```bash
sudo systemctl status ytp-dl-api
sudo journalctl -u ytp-dl-api -f
sudo systemctl restart ytp-dl-api
sudo systemctl stop ytp-dl-api
sudo systemctl start ytp-dl-api
```

---

## API reference

### `POST /api/download` (SSE logs)

Request body:

```json
{
  "url": "string (required)",
  "resolution": "integer (optional, default: 1080)",
  "extension": "string (optional: 'mp4', 'mp3', or 'best')",
  "job_id": "string (optional, recommended for fetch; [A-Za-z0-9_-])"
}
```

- `mp4` — 1080p H.264 + AAC, no transcoding
- `mp3` — best audio stream, output as MP3 with embedded cover art and metadata
- `best` — yt-dlp selects the highest quality adaptive format; `resolution` is ignored

Response — `200 OK` SSE stream (`text/event-stream`):

```
data: [start] job_id=<job_id>
data: [total_items] <n>               # playlist/multi only — track count
data: <yt-dlp output lines>
data: [r2_upload] XX.XX%              # R2 only — per track, then the result file
data: [r2_track] key=<object_key>     # R2 + playlist/multi only — one per uploaded track
data: [r2_tracks_incomplete]          # R2 + playlist/multi only — a track upload failed
data: [ready] job_id=<job_id>
data: [file] <filename>               # the media file — or the ZIP name for playlists
data: [r2] key=<object_key>           # R2 only — the result file's key
data: [fetch] /api/fetch/<job_id>
data: [done]
```

Other responses:
- `400 Bad Request` — missing or invalid URL/params
- `503 Service Unavailable` — server busy (max concurrent downloads reached)

### `GET /api/fetch/<job_id>`

Returns the finished file as an attachment. The job directory is cleaned up after the response completes (or after `YTPDL_DONE_TTL_S` elapses).

### `GET /api/fetch/<job_id>/<filename>`

Returns a specific file from the job directory by name. Useful for fetching any secondary output files produced by a job (e.g. the ZIP of individual playlist tracks).

### `GET /healthz`

```json
{
  "ok": true,
  "in_use": 1,
  "capacity": 1
}
```

---

## VPS deployment

The included Ubuntu installer script is designed for a **fresh VPS** and sets everything up end-to-end so the public API stays reachable while Mullvad is cycling.

Under the hood, Mullvad connect/disconnect can change Linux routing. Without extra routing rules, inbound connections to your API can intermittently fail (e.g., TCP handshakes time out). The installer handles this by:

* **Pinning replies from your public VPS IP** to the public interface via a small policy-routing rule (so your API keeps responding on the same route).
* **Excluding the API port from the VPN tunnel** using nftables marks (so the port stays reachable even while Mullvad is connected).

It also installs all runtime dependencies and configures the API as a managed systemd service.

```bash
#!/usr/bin/env bash
# VPS_Installation.sh - Minimal Ubuntu 24.04/25.04 setup for ytp-dl API + Mullvad
#
# What this does:
#   - Installs Python, ffmpeg, Mullvad CLI
#   - Installs Deno system-wide (JS runtime required for modern YouTube extraction via yt-dlp)
#   - Configures policy routing so the public API stays reachable while Mullvad toggles
#   - Adds Mullvad excluded-port rules (nftables marks) so :PORT stays reachable under VPN
#   - Creates a virtualenv at /opt/yt-dlp-mullvad/venv
#   - Installs ytp-dl + yt-dlp[default] + gunicorn (+ boto3 if R2 upload enabled)
#   - (Optional) bakes in Cloudflare R2 uploader env vars
#   - Creates a systemd service ytp-dl-api.service on port 5000
#
# Mullvad IP rotation is handled automatically by downloader.py.
# VPN connects on first job and stays up. IP rotates only on bot detection.

set -euo pipefail

### --- Tunables -------------------------------------------------------------
PORT="${PORT:-5000}"                           # API listen port
APP_DIR="${APP_DIR:-/opt/yt-dlp-mullvad}"      # app/venv root
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"        # python venv

MV_ACCOUNT="${MV_ACCOUNT:-}"                            # Mullvad account number (required)
YTPDL_MAX_CONCURRENT="${YTPDL_MAX_CONCURRENT:-1}"       # API concurrency cap (download jobs)
YTPDL_MULLVAD_LOCATION="${YTPDL_MULLVAD_LOCATION:-us}"  # default Mullvad relay hint
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"               # Gunicorn worker processes
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"               # Threads per Gunicorn worker

# --- Optional R2 upload (Cloudflare R2 / S3-compatible) ----------------------
YTPDL_R2_UPLOAD="${YTPDL_R2_UPLOAD:-0}"                 # 1 to enable upload
R2_ENDPOINT="${R2_ENDPOINT:-}"                          # e.g. https://<accountid>.r2.cloudflarestorage.com
R2_BUCKET="${R2_BUCKET:-}"                              # e.g. ezmdl
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"                # uploader key id
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"        # uploader secret
YTPDL_VPS_API_TOKEN="${YTPDL_VPS_API_TOKEN:-}"          # shared secret — must be set to same value in client-side env; leave empty to disable
export AWS_EC2_METADATA_DISABLED="true"
### -------------------------------------------------------------------------

[[ "${EUID}" -eq 0 ]] || { echo "Please run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "==> 0) Capture public routing (pre-VPN)"
PUB_DEV="$(ip route show default | awk '/default/ {print $5; exit}')"
PUB_GW="$(ip route show default | awk '/default/ {print $3; exit}')"
PUB_IP="$(ip -4 addr show dev "${PUB_DEV}" | awk '/inet / {print $2}' | cut -d/ -f1 | head -n1)"

if [[ -z "${PUB_DEV}" || -z "${PUB_GW}" || -z "${PUB_IP}" ]]; then
  echo "Failed to detect public routing (PUB_DEV/PUB_GW/PUB_IP)."
  echo "PUB_DEV=${PUB_DEV} PUB_GW=${PUB_GW} PUB_IP=${PUB_IP}"
  exit 1
fi

echo "Public dev: ${PUB_DEV} | gw: ${PUB_GW} | ip: ${PUB_IP}"

echo "==> 1) Base packages & Mullvad CLI"
apt-get update
apt-get install -yq --no-install-recommends   python3-venv python3-pip curl ffmpeg ca-certificates unzip   iproute2 iptables nftables

if ! command -v mullvad >/dev/null 2>&1; then
  curl -fsSLo /tmp/mullvad.deb https://mullvad.net/download/app/deb/latest/
  apt-get install -y /tmp/mullvad.deb
fi

if [[ -n "${MV_ACCOUNT}" ]]; then
  echo "Logging into Mullvad account (if not already logged in)..."
  mullvad account login "${MV_ACCOUNT}" || true
fi

mullvad status || true

# Keep the public API reachable even if Mullvad disconnects between jobs.
# (Lockdown mode can block all traffic while disconnected.)
mullvad lockdown-mode set off || true
mullvad lan set allow || true

echo "==> 1.1) Policy routing: keep replies from ${PUB_IP} on ${PUB_DEV}"
# Loose reverse-path filtering avoids drops when the default route changes under VPN.
tee /etc/sysctl.d/99-ytpdl-policy-routing.conf >/dev/null <<EOF
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
net.ipv4.conf.${PUB_DEV}.rp_filter=2
EOF
sysctl --system >/dev/null

# Persist the detected public route info for re-apply at boot.
tee /etc/default/ytpdl-policy-routing >/dev/null <<EOF
PUB_DEV=${PUB_DEV}
PUB_GW=${PUB_GW}
PUB_IP=${PUB_IP}
EOF

# Add a routing table id if it doesn't already exist.
grep -qE '^100\s+ytpdl-public$' /etc/iproute2/rt_tables || echo '100 ytpdl-public' >> /etc/iproute2/rt_tables

# Idempotent apply script.
tee /usr/local/sbin/ytpdl-policy-routing.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

source /etc/default/ytpdl-policy-routing

TABLE_ID="100"
TABLE_NAME="ytpdl-public"
PRIO="11000"

# Ensure table has the public default route.
ip route replace default via "${PUB_GW}" dev "${PUB_DEV}" table "${TABLE_NAME}"

# Ensure rule exists (replace is not supported for rules).
if ip rule show | grep -qE "^${PRIO}:.*from ${PUB_IP}/32 lookup ${TABLE_NAME}"; then
  :
else
  # remove any stale rule at this priority
  while ip rule show | grep -qE "^${PRIO}:"; do
    ip rule del priority "${PRIO}" || true
  done
  ip rule add priority "${PRIO}" from "${PUB_IP}/32" table "${TABLE_NAME}"
fi

ip route flush cache || true
EOF
chmod +x /usr/local/sbin/ytpdl-policy-routing.sh

tee /etc/systemd/system/ytpdl-policy-routing.service >/dev/null <<EOF
[Unit]
Description=ytp-dl policy routing (keep public API reachable)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ytpdl-policy-routing.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ytpdl-policy-routing.service

echo "==> 1.2) Mullvad exclude rules: keep :${PORT} reachable during VPN"
# Uses Mullvad-documented nftables marks (advanced split tunneling).
EXCLUDE_NFT="/etc/ytpdl-mullvad-exclude-ports.nft"
tee "${EXCLUDE_NFT}" >/dev/null <<EOF
table inet ytpdl_mullvad_exclusions {
  chain allowIncoming {
    type filter hook input priority -100; policy accept;
    tcp dport ${PORT} ct mark set 0x00000f41 meta mark set 0x6d6f6c65
    udp dport ${PORT} ct mark set 0x00000f41 meta mark set 0x6d6f6c65
  }

  chain allowOutgoing {
    type route hook output priority -100; policy accept;
    tcp sport ${PORT} ct mark set 0x00000f41 meta mark set 0x6d6f6c65
    udp sport ${PORT} ct mark set 0x00000f41 meta mark set 0x6d6f6c65
  }
}
EOF

# Apply now (idempotent: it replaces/overwrites the table)
nft -f "${EXCLUDE_NFT}"

tee /etc/systemd/system/ytpdl-mullvad-exclude-ports.service >/dev/null <<EOF
[Unit]
Description=ytp-dl Mullvad excluded ports (nftables)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f ${EXCLUDE_NFT}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ytpdl-mullvad-exclude-ports.service

echo "==> 1.5) Install Deno (system-wide, for yt-dlp YouTube extraction)"
# Non-interactive:
#   --yes            => skip prompts / accept defaults
#   --no-modify-path => do NOT edit shell rc files (we install into /usr/local anyway)
if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes --no-modify-path
fi

deno --version

echo "==> 2) App dir & virtualenv"
mkdir -p "${APP_DIR}"
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip

pip install "ytp-dl==2026.6.12"
if [[ "${YTPDL_R2_UPLOAD}" == "1" ]]; then
  pip install boto3
fi
deactivate

echo "==> 3) API environment file (/etc/default/ytp-dl-api)"
tee /etc/default/ytp-dl-api >/dev/null <<EOF
YTPDL_MAX_CONCURRENT=${YTPDL_MAX_CONCURRENT}
YTPDL_MULLVAD_LOCATION=${YTPDL_MULLVAD_LOCATION}
YTPDL_VENV=${VENV_DIR}

GUNICORN_WORKERS=${GUNICORN_WORKERS}
GUNICORN_THREADS=${GUNICORN_THREADS}

YTPDL_R2_UPLOAD=${YTPDL_R2_UPLOAD}
R2_ENDPOINT=${R2_ENDPOINT}
R2_BUCKET=${R2_BUCKET}
R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
AWS_EC2_METADATA_DISABLED=true
YTPDL_VPS_API_TOKEN=${YTPDL_VPS_API_TOKEN}
EOF

echo "==> 4) Gunicorn systemd service (ytp-dl-api.service on :${PORT})"
tee /etc/systemd/system/ytp-dl-api.service >/dev/null <<EOF
[Unit]
Description=Gunicorn for ytp-dl Mullvad API (minimal)
After=network-online.target ytpdl-policy-routing.service ytpdl-mullvad-exclude-ports.service
Wants=network-online.target
Requires=ytpdl-policy-routing.service ytpdl-mullvad-exclude-ports.service

[Service]
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/default/ytp-dl-api
Environment=VIRTUAL_ENV=${VENV_DIR}
Environment=PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

ExecStart=${VENV_DIR}/bin/gunicorn -k gthread -w ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS}   --timeout 0 --graceful-timeout 15 --keep-alive 20   --bind 0.0.0.0:${PORT} scripts.api:app

Restart=always
RestartSec=3
LimitNOFILE=65535
MemoryMax=1G

[Install]
WantedBy=multi-user.target
EOF

echo "==> 5) Start and enable API service"
systemctl daemon-reload
systemctl enable --now ytp-dl-api.service

echo "==> 6) Quick status + health check"
systemctl status ytp-dl-api --no-pager || true

echo
echo "Waiting for API to start..."
sleep 3
echo "Health (local):"
curl -sS "http://127.0.0.1:${PORT}/healthz" || true

echo
echo "========================================="
echo "Installation complete!"
echo "API running on port ${PORT}"
echo "Test from outside: curl http://YOUR_VPS_IP:${PORT}/healthz"
echo "========================================="
```
