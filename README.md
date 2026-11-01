# ytp-dl

[![PyPI version](https://img.shields.io/pypi/v/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Python Support](https://img.shields.io/pypi/pyversions/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![License](https://img.shields.io/pypi/l/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Downloads](https://img.shields.io/pypi/dm/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)

ytp-dl is a self-hosted yt-dlp API that routes downloads through an isolated Mullvad network namespace, with optional Cloudflare R2 delivery.

## Features

- MP4, MP3, and best-format downloads
- Playlist and multi-URL support
- Server-Sent Events (SSE) for live download output
- Optional title, artist, and artwork metadata
- Optional Cloudflare R2 storage
- Durable job status for reconnect/recovery
- Configurable concurrency, timeouts, and free-disk reserve
- Active disk-space protection
- Isolated Mullvad routing for yt-dlp while SSH/API traffic stays on the server's normal network
- Automatic Mullvad connection and device recovery

## Installation

```bash
pip install ytp-dl
```

This installs the Python package and API server. For a complete Ubuntu server setup, including Mullvad routing and systemd services, see [VPS installation](#vps-installation).

### Requirements

- Linux (Ubuntu 24.04 LTS recommended)
- Python 3.10+
- FFmpeg
- Deno
- A Mullvad account for the included VPN setup

## Quick start

Start a download. The example below supplies optional fields explicitly so the behavior and fetch URL are predictable; **only `url` is required**.

```bash
curl -N --http1.1 \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -X POST "http://YOUR_SERVER_IP:5000/api/download" \
  --data-binary '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","extension":"mp4","resolution":1080,"job_id":"demo1"}'
```

When the stream reports:

```text
data: [fetch] /api/fetch/demo1
```

fetch the result:

```bash
curl -L -O -J "http://YOUR_SERVER_IP:5000/api/fetch/demo1"
```

If `YTPDL_VPS_API_TOKEN` is configured, add:

```bash
-H "X-YTPDL-Token: YOUR_TOKEN"
```

to API requests.

### Download modes

| Mode | Behavior |
|---|---|
| `mp4` | Prefers H.264 video + AAC audio at or below `resolution`; falls back to other available formats if needed |
| `mp3` | Downloads the best audio stream and outputs MP3 |
| `best` | Prefers the best video + audio combination at or below `resolution`, with broader fallbacks |

`extension` is optional and defaults to `mp4`. Despite the field name, `best` is a download mode, not a literal `.best` file extension. `resolution` is optional and defaults to `1080`; it is ignored for `mp3` downloads.

## API

### `POST /api/download`

Starts a download and returns a Server-Sent Events (SSE) stream. The HTTP connection remains open while the job runs and closes after the terminal `[done]` event.

```json
{
  "url": "https://example.com/media",
  "extension": "mp4",
  "resolution": 1080,
  "job_id": "example-job",
  "metadata": false
}
```

`url` is the only required field. All other request fields are optional:

| Field | Required | Default | Meaning |
|---|---|---|---|
| `url` | Yes | — | One media URL, a playlist URL, or a comma-separated list of URLs. |
| `extension` | No | `mp4` | Download mode: `mp4`, `mp3`, or `best`. `best` is a mode, not a literal file extension. |
| `resolution` | No | `1080` | Maximum preferred video height. Ignored when `extension` is `mp3`. |
| `job_id` | No | Auto-generated | Stable job identifier used by status/fetch endpoints and reconnect recovery. Supplying one is recommended when the caller needs to reconnect to or fetch a known job later. |
| `metadata` | No | `false` | Set to `true` to emit title/artist metadata and artwork events when available. |

A minimal valid request is therefore:

```json
{
  "url": "https://example.com/media"
}
```

That request uses `mp4` mode at a default `1080` resolution cap, generates a `job_id` automatically, and leaves metadata output disabled.

#### Streaming response (SSE)

The response uses `Content-Type: text/event-stream`. Each message is sent as an SSE `data:` field followed by a blank line, so clients can consume download output as it happens instead of polling for progress.

The quick-start `curl` command uses `-N` (`--no-buffer`) so events are printed to the terminal immediately as they arrive.

A stream can contain three kinds of output:

- **yt-dlp output** — human-readable extractor, download, speed, ETA, retry, and post-processing messages. These are useful for logs and user interfaces, but their wording is controlled by yt-dlp and may change between yt-dlp versions.
- **ytp-dl protocol events** — documented events intended for programmatic job state, progress, metadata, and result handling.
- **diagnostic/internal lines** — implementation details that may appear in the stream. Clients should ignore unrecognized lines rather than depending on them.

Applications should use the documented ytp-dl protocol events for machine-readable behavior instead of parsing arbitrary yt-dlp text.

| Event | Meaning |
|---|---|
| `[start] job_id=<id>` | The job has started. |
| `[total_items] <n>` | Total item count for a playlist or multi-URL job. |
| `[finalize] <percent>` | Real FFmpeg finalization progress when an applicable conversion, merge, or remux stage runs. This event may be absent when no measurable FFmpeg finalization is required. |
| `[r2_upload] <percent>` | Progress for the current R2 upload. Collection jobs may emit this for individual tracks and the final result ZIP. |
| `[r2_track] key=<key>` | An individual collection item has been uploaded to R2. |
| `[meta] media=<name>\ttitle=<title>\tartist=<artist>` | Per-file media metadata when metadata output is enabled. |
| `[meta_thumb] ...` | Artwork location for a media file when metadata output is enabled. |
| `[ready] job_id=<id>` | Processing has completed and the result is ready to retrieve. |
| `[file] <filename>` | Final result filename. For collections, this is the result ZIP. |
| `[r2] key=<key>` | R2 key for the final result when R2 delivery is enabled. |
| `[fetch] /api/fetch/<id>` | Endpoint for retrieving the completed result. |
| `[error] <message>` | The job failed. A terminal `[done]` follows. |
| `[done]` | Terminal event for the SSE stream. No further job output follows. |

### `GET /api/status/<job_id>`

Returns the current durable server-side job state: `active`, `complete`, `incomplete`, or `missing`. This endpoint is independent of the original SSE connection and can be used to recover state after a client disconnects.

### `GET /api/fetch/<job_id>`

Returns the finished file or redirects to its R2 object when applicable.

### `GET /api/fetch/<job_id>/<filename>`

Returns an individual collection file for inline streaming.

### `GET /healthz`

Returns service health and current download capacity.

```json
{
  "ok": true,
  "in_use": 0,
  "capacity": 1,
  "free_disk_mb": 65432.1,
  "min_free_disk_mb": 8192,
  "disk_ready": true
}
```

`disk_ready` becomes `false` when the configured free-space reserve is breached. Clients can use this together with `in_use`/`capacity` to defer new work during temporary storage pressure.

When `YTPDL_VPS_API_TOKEN` is set, the download, status, and fetch routes require the same value in the `X-YTPDL-Token` header. `/healthz` remains unauthenticated.

## Configuration

Default service settings:

These are the conservative defaults used by both the service/package and the bundled VPS installer when no existing configuration is present. Tune them for the resources available on your host.

| Variable | Default | Description |
|---|---:|---|
| `PORT` | `5000` | API port |
| `YTPDL_MAX_CONCURRENT` | `1` | Maximum simultaneous download jobs on the server |
| `GUNICORN_WORKERS` | `1` | Gunicorn worker processes |
| `GUNICORN_THREADS` | `2` | Threads per Gunicorn worker |
| `YTPDL_MEMORY_MAX` | `2G` | systemd memory ceiling for the API service |
| `YTPDL_MIN_FREE_DISK_MB` | `8192` | Minimum free-disk reserve maintained by the service |
| `YTPDL_DISK_CHECK_INTERVAL_S` | `1.0` | Interval between active disk-space checks |
| `YTPDL_JOB_TIMEOUT_S` | `1800` | Single-file yt-dlp timeout |
| `YTPDL_PLAYLIST_JOB_TIMEOUT_S` | `21600` | Playlist yt-dlp timeout |
| `YTPDL_SOCKET_TIMEOUT_S` | `10` | Per-network-operation yt-dlp socket timeout |
| `YTPDL_VPN_RECOVERY_TIMEOUT_S` | `180` | Maximum time allowed for Mullvad recovery |
| `YTPDL_PLAYLIST_PASSES` | `5` | Maximum passes used to recover missing playlist entries |
| `YTPDL_R2_ZIP_PART_SIZE_MB` | `16` | Multipart buffer size for collection ZIP uploads |
| `YTPDL_R2_ZIP_WORKERS` | `6` | Concurrent multipart workers per collection ZIP upload |
| `YTPDL_R2_UPLOAD` | `0` | Enable R2 uploads |
| `YTPDL_VPS_API_TOKEN` | *(empty)* | Optional shared API token |
| `YTPDL_MULLVAD_LOCATION` | `us` | Mullvad relay location filter |
| `YTPDL_MULLVAD_AUTO_EVICT` | `1` | Allow automatic removal of one eligible Mullvad device when required to restore the server |
| `YTPDL_MULLVAD_PROTECTED_KEYS` | *(empty)* | Comma-separated Mullvad public keys that automatic recovery must never remove |
| `YTPDL_MULLVAD_CONFIG_REFRESH_S` | `21600` | Maximum age before Mullvad relay configurations are refreshed |
| `YTPDL_VPN_LOCK_TIMEOUT_S` | `120` | Maximum wait while another Mullvad recovery operation is in progress |

API runtime settings can be overridden in `/etc/default/ytp-dl-api`. After changing them:

```bash
sudo systemctl restart ytp-dl-api
```

R2 requires `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`.

If `YTPDL_VPS_API_TOKEN` is empty, ytp-dl does not perform application-level authentication. If port `5000` is reachable from the public internet, configure a token or restrict access with a firewall or reverse proxy.

## Service management

```bash
sudo systemctl status ytp-dl-api --no-pager
sudo journalctl -u ytp-dl-api -f
sudo systemctl restart ytp-dl-api
sudo systemctl stop ytp-dl-api
sudo systemctl start ytp-dl-api
```

## Deployment notes

- yt-dlp and its child processes run inside an isolated Mullvad WireGuard network namespace. SSH, the API service, and normal host traffic continue to use the server's standard network connection.
- Cloudflare R2 storage is optional. When enabled, completed downloads are uploaded to R2; otherwise, results are served from local storage.
- The service includes automatic disk-space protection and Mullvad recovery to help keep long-running deployments operational.
- Mullvad recovery may reclaim one eligible device slot when the account is full. Use `YTPDL_MULLVAD_PROTECTED_KEYS` to prevent specific devices from being removed.

## VPS installation

The included [`install.sh`](https://github.com/dumgum84/ytp-dl/blob/main/install.sh) deploys or upgrades a complete ytp-dl server on Ubuntu 24.04 LTS, including Gunicorn, Mullvad routing, disk-space protection, and optional Cloudflare R2 delivery.

The installer uses the defaults listed in [Configuration](#configuration). Existing settings are preserved when upgrading where applicable.

Automatic Mullvad recovery is enabled by default. If the account is full, recovery may remove one eligible unprotected device to restore the server. Use `YTPDL_MULLVAD_PROTECTED_KEYS` to protect specific devices.

SSH into the server and become root:

```bash
ssh ubuntu@YOUR_SERVER_IP
sudo -s
```

Download the installer from the project repository:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/dumgum84/ytp-dl/main/install.sh
chmod +x install.sh
```

Review or override settings if needed, then run it:

```bash
./install.sh
```

The installer verifies the API and Mullvad connection before it exits. Runtime settings can be changed afterward in `/etc/default/ytp-dl-api`; Mullvad settings are stored in `/etc/default/ytpdl-vpn`.


## Python client example

The following script shows how to call a deployed ytp-dl API from Python, stream download output, and fetch the completed file.

### Usage

```bash
# MP4
python3 ytp-dl.py --base "http://YOUR_SERVER_IP:5000" --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --extension mp4 --resolution 1080

# MP3
python3 ytp-dl.py --base "http://YOUR_SERVER_IP:5000" --url "https://soundcloud.com/artist/track" --extension mp3

# Playlist
python3 ytp-dl.py --base "http://YOUR_SERVER_IP:5000" --url "https://www.youtube.com/playlist?list=PLxxx" --extension mp4 --out-dir ./downloads
```

Set `YTPDL_BASE` to avoid passing `--base` each time. If API authentication is enabled, set `YTPDL_VPS_API_TOKEN` or pass `--token`.

```python
#!/usr/bin/env python3
"""
ytp-dl Python client (SSE + fetch)

Flow:
  1) POST /api/download         -> streams yt-dlp logs as Server-Sent Events (SSE)
  2) GET  /api/fetch/<job_id>   -> downloads the finished file

On a server-emitted [error] event (for example a rate-limit), re-POSTs with
the same job_id so the server can resume the same job.

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
    "[meta] ",
    "[meta_thumb] ",
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
    metadata: bool
    token: str
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
    Stream SSE logs from the ytp-dl download endpoint.

    Returns (fetch_path, resolved_job_id, error_msg).
    error_msg is non-None when the server emitted [error].
    For playlists the fetched file is the ZIP of individual tracks.
    """
    payload: dict = {"url": cfg.url, "extension": cfg.extension, "job_id": cfg.job_id}
    if cfg.resolution is not None:
        payload["resolution"] = int(cfg.resolution)
    if cfg.metadata:
        payload["metadata"] = True

    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if cfg.token:
        headers["X-YTPDL-Token"] = cfg.token

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

    headers = {"Connection": "close"}
    if cfg.token:
        headers["X-YTPDL-Token"] = cfg.token

    with requests.get(
        f"{cfg.base}{fetch_path}",
        stream=True,
        timeout=(cfg.connect_timeout_s, cfg.read_timeout_s),
        headers=headers,
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
        help="Base URL, e.g. http://YOUR_SERVER_IP:5000 (env: YTPDL_BASE)",
    )
    p.add_argument("--url", required=True, help="Media URL to download")
    p.add_argument("--extension", default="mp4", choices=["mp4", "mp3", "best"],
                   help="Download mode")
    p.add_argument("--resolution", type=int, default=1080,
                   help="Max height cap (default: 1080)")
    p.add_argument("--metadata", action="store_true",
                   help="Emit per-file track info to the client (sidecar thumbnail + title/artist)")
    p.add_argument("--out-dir", default=".", help="Directory to save the fetched file")
    p.add_argument(
        "--token",
        default=os.environ.get("YTPDL_VPS_API_TOKEN", ""),
        help="Shared secret sent as the X-YTPDL-Token header (env: YTPDL_VPS_API_TOKEN). "
             "Only required if the server has YTPDL_VPS_API_TOKEN set; leave empty otherwise.",
    )
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=300.0)
    p.add_argument("--max-retries", type=int, default=5,
                   help="Maximum total SSE attempts after server [error] events (default: 5)")
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
        metadata=a.metadata,
        token=a.token,
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

        # Server emitted an SSE [error] event (rate-limit, VPN cycle needed, etc.)
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

        # Keep the same job_id so the server can resume the same job.

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
