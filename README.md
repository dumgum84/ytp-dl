# ytp-dl

[![PyPI version](https://img.shields.io/pypi/v/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Python Support](https://img.shields.io/pypi/pyversions/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![License](https://img.shields.io/pypi/l/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)
[![Downloads](https://img.shields.io/pypi/dm/ytp-dl.svg)](https://pypi.org/project/ytp-dl/)

Privacy-focused yt-dlp API with Mullvad routing.

## Features

- MP4, MP3, and best-format downloads
- Playlist and multi-URL support
- Server-Sent Events (SSE) for live download output
- Optional title, artist, and artwork metadata
- Optional Cloudflare R2 storage
- Durable job status for reconnect/recovery
- Configurable concurrency, timeouts, and disk reserve
- Isolated Mullvad routing for yt-dlp while SSH/API traffic stays on the normal VPS network

## Installation

```bash
pip install ytp-dl==2026.9.22
```

### Requirements

- Linux (Ubuntu 24.04 LTS recommended)
- Python 3.10+
- FFmpeg
- Deno
- A Mullvad account for the included VPN setup

## Quick start

Start a download:

```bash
curl -N --http1.1 \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -X POST "http://YOUR_VPS_IP:5000/api/download" \
  --data-binary '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","extension":"mp4","resolution":1080,"job_id":"demo1"}'
```

When the stream reports:

```text
data: [fetch] /api/fetch/demo1
```

fetch the result:

```bash
curl -L -O -J "http://YOUR_VPS_IP:5000/api/fetch/demo1"
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

`resolution` defaults to `1080` and is ignored for MP3 downloads.

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

`url` is required. `extension` may be `mp4`, `mp3`, or `best`. `resolution` defaults to `1080` and is ignored for MP3 downloads. `job_id` is optional but recommended when the caller needs to reconnect or fetch the completed result. Set `metadata` to `true` to emit media metadata and artwork events.

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

Returns the current durable VPS-side job state: `active`, `complete`, `incomplete`, or `missing`. This endpoint is independent of the original SSE connection and can be used to recover state after a client disconnects.

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
  "capacity": 1
}
```

When `YTPDL_VPS_API_TOKEN` is set, the download, status, and fetch routes require the same value in the `X-YTPDL-Token` header. `/healthz` remains unauthenticated.

## Configuration

Default service settings:

| Variable | Default | Description |
|---|---:|---|
| `PORT` | `5000` | API port |
| `YTPDL_MAX_CONCURRENT` | `1` | Maximum simultaneous download jobs across the VPS |
| `GUNICORN_WORKERS` | `1` | Gunicorn worker processes |
| `GUNICORN_THREADS` | `2` | Threads per Gunicorn worker |
| `YTPDL_MEMORY_MAX` | `2G` | systemd memory ceiling for the API service |
| `YTPDL_MIN_FREE_DISK_MB` | `8192` | Minimum free disk reserve before new jobs are refused |
| `YTPDL_JOB_TIMEOUT_S` | `1800` | Single-file yt-dlp timeout |
| `YTPDL_PLAYLIST_JOB_TIMEOUT_S` | `21600` | Playlist yt-dlp timeout |
| `YTPDL_PLAYLIST_PASSES` | `5` | Maximum passes used to recover missing playlist entries |
| `YTPDL_R2_ZIP_PART_SIZE_MB` | `16` | Multipart buffer size for collection ZIP uploads |
| `YTPDL_R2_ZIP_WORKERS` | `10` | Concurrent multipart workers per collection ZIP upload |
| `YTPDL_R2_UPLOAD` | `0` | Enable R2 uploads |
| `YTPDL_VPS_API_TOKEN` | *(empty)* | Optional shared API token |
| `YTPDL_MULLVAD_LOCATION` | `us` | Mullvad relay filter used by the installer |

API runtime settings can be overridden in `/etc/default/ytp-dl-api`. After changing them:

```bash
sudo systemctl restart ytp-dl-api
```

R2 requires `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`.

## Service management

```bash
sudo systemctl status ytp-dl-api --no-pager
sudo journalctl -u ytp-dl-api -f
sudo systemctl restart ytp-dl-api
sudo systemctl stop ytp-dl-api
sudo systemctl start ytp-dl-api
```

## Deployment notes

- yt-dlp and its child processes use the isolated Mullvad namespace; host services use the VPS's normal network.
- With R2 enabled, completed files are uploaded to R2; collection ZIPs are streamed to R2 from local media using concurrent multipart uploads. Without R2, results are served from local storage.

## VPS installer

Run as root (`sudo -s`). Enter your Mullvad account number in the `MV_ACCOUNT` variable in the installer before running. R2 and API authentication are optional.

```bash
#!/usr/bin/env bash
# VPS_Installation.sh - Ubuntu VPS setup for ytp-dl 2026.9.22
#
# Architecture:
#   - SSH, Gunicorn/API and Cloudflare R2 stay on the VPS's normal network.
#   - ONLY yt-dlp (and children such as ffmpeg/Deno) run inside a dedicated
#     Linux network namespace whose sole non-loopback interface is WireGuard.
#   - The WireGuard interface is created in the host namespace, then moved into
#     the yt-dlp namespace. Its encrypted UDP socket remains in the host
#     namespace, while cleartext yt-dlp traffic can only leave through ytpdlwg.
#   - No global Mullvad route changes, no SSH/API nftables exceptions and no
#     custom source-policy routing are required.
#   - VPN rotation is serialized across all Gunicorn workers with flock().
#   - YTPDL_MAX_CONCURRENT is enforced globally by api.py across all workers.
#
# Target: Ubuntu 24.04 LTS.

set -euo pipefail

PORT="${PORT:-5000}"
APP_DIR="${APP_DIR:-/opt/yt-dlp-mullvad}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"
YTPDL_VERSION="${YTPDL_VERSION:-2026.9.22}"

# Mullvad / isolated WireGuard namespace
MV_ACCOUNT="${MV_ACCOUNT:-}"
YTPDL_MULLVAD_LOCATION="${YTPDL_MULLVAD_LOCATION:-us}"
VPN_NAMESPACE="${VPN_NAMESPACE:-ytpdl-vpn}"
VPN_DIR="${VPN_DIR:-/etc/ytpdl-vpn}"
VPN_CONFIG_DIR="${VPN_CONFIG_DIR:-${VPN_DIR}/configs}"
VPN_HELPER="${VPN_HELPER:-/usr/local/sbin/ytpdl-vpn}"
VPN_MTU="${VPN_MTU:-1420}"
VPN_ROTATE_COOLDOWN="${VPN_ROTATE_COOLDOWN:-10}"

# API / Gunicorn
YTPDL_MAX_CONCURRENT="${YTPDL_MAX_CONCURRENT:-1}"
YTPDL_MIN_FREE_DISK_MB="${YTPDL_MIN_FREE_DISK_MB:-8192}"
YTPDL_R2_ZIP_PART_SIZE_MB="${YTPDL_R2_ZIP_PART_SIZE_MB:-16}"
YTPDL_R2_ZIP_WORKERS="${YTPDL_R2_ZIP_WORKERS:-10}"
YTPDL_MEMORY_MAX="${YTPDL_MEMORY_MAX:-2G}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
YTPDL_VPS_API_TOKEN="${YTPDL_VPS_API_TOKEN:-}"

# R2
YTPDL_R2_UPLOAD="${YTPDL_R2_UPLOAD:-0}"
R2_ENDPOINT="${R2_ENDPOINT:-}"
R2_BUCKET="${R2_BUCKET:-}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
export AWS_EC2_METADATA_DISABLED="true"

[[ "${EUID}" -eq 0 ]] || { echo "Please run as root (sudo -s)" >&2; exit 1; }
[[ -n "${MV_ACCOUNT}" ]] || { echo "MV_ACCOUNT is required" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }

log "0) Prepare host networking and stop the API"
systemctl stop ytp-dl-api.service 2>/dev/null || true

# Ensure the host itself is not routed through the Mullvad app.
# yt-dlp uses the isolated WireGuard namespace configured below.
if command -v mullvad >/dev/null 2>&1; then
    mullvad disconnect >/dev/null 2>&1 || true
fi
systemctl disable --now mullvad-daemon.service 2>/dev/null || true
systemctl disable --now mullvad-early-boot-blocking.service 2>/dev/null || true

# Remove conflicting host-level routing or firewall rules if present.
systemctl disable --now ytpdl-policy-routing.service 2>/dev/null || true
systemctl disable --now ytpdl-mullvad-exclude-ports.service 2>/dev/null || true
rm -f /etc/systemd/system/ytpdl-policy-routing.service
rm -f /etc/systemd/system/ytpdl-mullvad-exclude-ports.service
rm -f /usr/local/sbin/ytpdl-policy-routing.sh
rm -f /usr/local/sbin/ytpdl-mullvad-exclusions.sh
rm -f /etc/ytpdl-mullvad-exclude-ports.nft
rm -f /etc/default/ytpdl-policy-routing
rm -f /etc/sysctl.d/99-ytpdl-policy-routing.conf
nft delete table inet ytpdl_mullvad_exclusions 2>/dev/null || true
while ip rule show | grep -qE '^11000:'; do
    ip rule del priority 11000 2>/dev/null || break
done
if grep -qE '^[[:space:]]*100[[:space:]]+ytpdl-public[[:space:]]*$' /etc/iproute2/rt_tables 2>/dev/null; then
    sed -i '/^[[:space:]]*100[[:space:]]\+ytpdl-public[[:space:]]*$/d' /etc/iproute2/rt_tables
fi
systemctl daemon-reload

log "1) Install base packages"
apt-get update
apt-get install -yq --no-install-recommends \
    python3-venv python3-pip python3-cryptography \
    curl ca-certificates ffmpeg unzip \
    iproute2 wireguard-tools util-linux

# Verify kernel WireGuard support early.
modprobe wireguard

log "2) Generate Mullvad WireGuard configurations"
install -d -m 700 "${VPN_DIR}" "${VPN_CONFIG_DIR}"
install -d -m 755 /usr/local/lib/ytpdl

# Official Mullvad wg-tools generator. It creates/reuses one WireGuard device key
# and generates configs for active relays matching YTPDL_MULLVAD_LOCATION.
curl -fsSLo /usr/local/lib/ytpdl/wg-mullvad.py \
    https://raw.githubusercontent.com/mullvad/wg-tools/main/wg-mullvad.py
chmod 755 /usr/local/lib/ytpdl/wg-mullvad.py

# Keep the device key, but refresh relay configs so stale/inactive entries do not
# accumulate across installer reruns.
rm -f "${VPN_CONFIG_DIR}"/*.conf 2>/dev/null || true
python3 /usr/local/lib/ytpdl/wg-mullvad.py \
    --account "${MV_ACCOUNT}" \
    --filter "${YTPDL_MULLVAD_LOCATION}" \
    --active \
    --settings-file "${VPN_DIR}/device.conf" \
    --output-dir "${VPN_CONFIG_DIR}"
chmod 600 "${VPN_DIR}/device.conf" "${VPN_CONFIG_DIR}"/*.conf

if ! find "${VPN_CONFIG_DIR}" -maxdepth 1 -type f -name '*.conf' -print -quit | grep -q .; then
    echo "No Mullvad WireGuard configs were generated for '${YTPDL_MULLVAD_LOCATION}'." >&2
    exit 1
fi

log "3) Install isolated VPN namespace helper"
cat > /etc/default/ytpdl-vpn <<EOF2
YTPDL_VPN_NAMESPACE=${VPN_NAMESPACE}
YTPDL_VPN_CONFIG_DIR=${VPN_CONFIG_DIR}
YTPDL_VPN_MTU=${VPN_MTU}
YTPDL_VPN_ROTATE_COOLDOWN=${VPN_ROTATE_COOLDOWN}
EOF2
chmod 600 /etc/default/ytpdl-vpn

cat > "${VPN_HELPER}" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail

source /etc/default/ytpdl-vpn

NS="${YTPDL_VPN_NAMESPACE:-ytpdl-vpn}"
CONFIG_DIR="${YTPDL_VPN_CONFIG_DIR:-/etc/ytpdl-vpn/configs}"
WG_IF="ytpdlwg"
MTU="${YTPDL_VPN_MTU:-1420}"
ROTATE_COOLDOWN="${YTPDL_VPN_ROTATE_COOLDOWN:-10}"
RUN_DIR="/run/ytpdl-vpn"
LOCK_FILE="/run/lock/ytpdl-vpn.lock"
CURRENT_FILE="${RUN_DIR}/current-config"
LAST_ROTATE_FILE="${RUN_DIR}/last-rotate"
STRIPPED_FILE="${RUN_DIR}/wireguard-stripped.conf"
CHECK_URL="https://am.i.mullvad.net/connected"
IP_URL="https://am.i.mullvad.net/ip"

mkdir -p "${RUN_DIR}" /run/lock
chmod 700 "${RUN_DIR}"

die() { echo "ytpdl-vpn: $*" >&2; exit 1; }

namespace_exists() {
    ip netns list | awk '{print $1}' | grep -Fxq "${NS}"
}

setup_namespace() {
    if ! namespace_exists; then
        ip netns add "${NS}"
    fi
    ip -n "${NS}" link set lo up
    mkdir -p "/etc/netns/${NS}"
    cat > "/etc/netns/${NS}/resolv.conf" <<'DNS'
nameserver 10.64.0.1
DNS
    chmod 644 "/etc/netns/${NS}/resolv.conf"
}

wg_exists() {
    namespace_exists && ip -n "${NS}" link show "${WG_IF}" >/dev/null 2>&1
}

connected() {
    wg_exists || return 1
    ip netns exec "${NS}" curl -fsS --connect-timeout 4 --max-time 8 "${CHECK_URL}" 2>/dev/null \
        | grep -qi 'You are connected to Mullvad'
}

current_config() {
    if [[ -s "${CURRENT_FILE}" ]]; then
        cat "${CURRENT_FILE}"
    fi
}

list_configs_random() {
    find "${CONFIG_DIR}" -maxdepth 1 -type f -name '*.conf' -print | shuf
}

down_locked() {
    setup_namespace
    if wg_exists; then
        ip -n "${NS}" link del "${WG_IF}" || true
    fi
}

connect_config() {
    local cfg="$1"
    [[ -f "${cfg}" ]] || return 1

    setup_namespace
    down_locked

    # wg(8) accepts the WireGuard fields only; wg-quick strip removes Address,
    # DNS and other wg-quick-only keys from the Mullvad-generated config.
    wg-quick strip "${cfg}" > "${STRIPPED_FILE}"
    chmod 600 "${STRIPPED_FILE}"

    # Critical namespace design: create the WireGuard interface on the HOST,
    # then move it into the restricted namespace. WireGuard keeps its encrypted
    # UDP socket in the namespace where it was born (the host), so the tunnel can
    # use the VPS's normal ens3 route while cleartext processes in ${NS} see only
    # lo + ${WG_IF}. There is therefore no non-VPN fallback path to leak through.
    ip link del "${WG_IF}" 2>/dev/null || true
    ip link add dev "${WG_IF}" type wireguard
    ip link set "${WG_IF}" netns "${NS}"

    ip netns exec "${NS}" wg setconf "${WG_IF}" "${STRIPPED_FILE}"

    local address_line
    address_line="$(sed -n 's/^[[:space:]]*Address[[:space:]]*=[[:space:]]*//Ip' "${cfg}" | head -n1)"
    [[ -n "${address_line}" ]] || { ip -n "${NS}" link del "${WG_IF}" || true; return 1; }

    local addr
    IFS=',' read -r -a _addrs <<< "${address_line}"
    for addr in "${_addrs[@]}"; do
        addr="${addr//[[:space:]]/}"
        [[ -n "${addr}" ]] || continue
        ip -n "${NS}" address add "${addr}" dev "${WG_IF}"
    done

    ip -n "${NS}" link set dev "${WG_IF}" mtu "${MTU}"
    ip -n "${NS}" link set dev "${WG_IF}" up
    ip -n "${NS}" route replace default dev "${WG_IF}"
    if printf '%s\n' "${address_line}" | grep -q ':'; then
        ip -n "${NS}" -6 route replace default dev "${WG_IF}" 2>/dev/null || true
    fi

    # Force a handshake and verify that the namespace really exits through Mullvad.
    local i
    for i in $(seq 1 15); do
        if connected; then
            printf '%s\n' "${cfg}" > "${CURRENT_FILE}"
            chmod 600 "${CURRENT_FILE}"
            echo "Connected: $(basename "${cfg}")"
            return 0
        fi
        sleep 1
    done

    ip -n "${NS}" link del "${WG_IF}" 2>/dev/null || true
    return 1
}

connect_from_candidates() {
    local avoid="${1:-}"
    local -a configs=()
    mapfile -t configs < <(list_configs_random)
    (( ${#configs[@]} > 0 )) || die "No WireGuard configs in ${CONFIG_DIR}"

    local cfg attempts=0
    for cfg in "${configs[@]}"; do
        [[ -n "${avoid}" && "${cfg}" == "${avoid}" && ${#configs[@]} -gt 1 ]] && continue
        attempts=$((attempts + 1))
        echo "Trying relay: $(basename "${cfg}")"
        if connect_config "${cfg}"; then
            return 0
        fi
        (( attempts >= 5 )) && break
    done
    return 1
}

ensure_locked() {
    setup_namespace
    if connected; then
        return 0
    fi

    local cur=""
    cur="$(current_config || true)"
    if [[ -n "${cur}" && -f "${cur}" ]]; then
        echo "Reconnecting current relay: $(basename "${cur}")"
        if connect_config "${cur}"; then
            return 0
        fi
    fi

    connect_from_candidates "${cur}" || die "Unable to establish Mullvad WireGuard tunnel"
}

rotate_locked() {
    setup_namespace

    local now last=0
    now="$(date +%s)"
    if [[ -s "${LAST_ROTATE_FILE}" ]]; then
        last="$(cat "${LAST_ROTATE_FILE}" 2>/dev/null || echo 0)"
    fi

    # Several concurrent yt-dlp jobs can observe the same dying IP at once. The
    # first worker rotates; later workers arriving during this small cooldown use
    # the already-fresh tunnel instead of immediately rotating it again.
    if (( now - last < ROTATE_COOLDOWN )) && connected; then
        echo "Rotation already completed recently; keeping fresh relay."
        return 0
    fi

    local cur=""
    cur="$(current_config || true)"
    connect_from_candidates "${cur}" || die "Unable to rotate Mullvad WireGuard tunnel"
    date +%s > "${LAST_ROTATE_FILE}"
    chmod 600 "${LAST_ROTATE_FILE}"
}

with_lock() {
    exec 9>"${LOCK_FILE}"
    flock -x 9
    "$@"
}

command="${1:-}"
[[ -n "${command}" ]] || die "Usage: ytpdl-vpn namespace|ensure|rotate|status|ip|down|exec -- COMMAND..."
shift || true

case "${command}" in
    namespace)
        setup_namespace
        ;;
    ensure)
        with_lock ensure_locked
        ;;
    rotate)
        with_lock rotate_locked
        ;;
    status)
        if connected; then
            echo "Connected"
            if [[ -s "${CURRENT_FILE}" ]]; then
                echo "Relay config: $(basename "$(cat "${CURRENT_FILE}")")"
            fi
            ip netns exec "${NS}" curl -fsS --connect-timeout 4 --max-time 8 "${CHECK_URL}" || true
            exit 0
        fi
        echo "Disconnected" >&2
        exit 1
        ;;
    ip)
        connected || die "VPN namespace is not connected"
        exec ip netns exec "${NS}" curl -fsS --connect-timeout 4 --max-time 8 "${IP_URL}"
        ;;
    down)
        with_lock down_locked
        ;;
    exec)
        [[ "${1:-}" == "--" ]] && shift
        (( $# > 0 )) || die "exec requires a command"
        wg_exists || die "VPN namespace is not connected"
        exec ip netns exec "${NS}" "$@"
        ;;
    *)
        die "Unknown command '${command}'"
        ;;
esac
EOF2
chmod 755 "${VPN_HELPER}"

cat > /etc/systemd/system/ytpdl-vpn-namespace.service <<EOF2
[Unit]
Description=ytp-dl isolated network namespace
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${VPN_HELPER} namespace
ExecStop=${VPN_HELPER} down
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF2

systemctl daemon-reload
systemctl enable --now ytpdl-vpn-namespace.service

log "4) Install Deno system-wide"
if ! command -v deno >/dev/null 2>&1; then
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes --no-modify-path
fi

log "5) Install ytp-dl ${YTPDL_VERSION} and latest yt-dlp pre-release"
mkdir -p "${APP_DIR}"
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install --upgrade "ytp-dl==${YTPDL_VERSION}"
pip install --upgrade --pre "yt-dlp[default,curl-cffi]"
if [[ "${YTPDL_R2_UPLOAD}" == "1" ]]; then
    pip install --upgrade boto3
fi
deactivate

log "6) Configure API environment"
cat > /etc/default/ytp-dl-api <<EOF2
YTPDL_MAX_CONCURRENT=${YTPDL_MAX_CONCURRENT}
YTPDL_VENV=${VENV_DIR}
YTPDL_VPN_HELPER=${VPN_HELPER}
YTPDL_SLOT_DIR=/run/ytpdl-slots
YTPDL_ACTIVE_LOCK_DIR=/run/ytpdl-active
YTPDL_MIN_FREE_DISK_MB=${YTPDL_MIN_FREE_DISK_MB}
YTPDL_R2_ZIP_PART_SIZE_MB=${YTPDL_R2_ZIP_PART_SIZE_MB}
YTPDL_R2_ZIP_WORKERS=${YTPDL_R2_ZIP_WORKERS}
GUNICORN_WORKERS=${GUNICORN_WORKERS}
GUNICORN_THREADS=${GUNICORN_THREADS}
YTPDL_R2_UPLOAD=${YTPDL_R2_UPLOAD}
R2_ENDPOINT=${R2_ENDPOINT}
R2_BUCKET=${R2_BUCKET}
R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
AWS_EC2_METADATA_DISABLED=true
YTPDL_VPS_API_TOKEN=${YTPDL_VPS_API_TOKEN}
EOF2
chmod 600 /etc/default/ytp-dl-api
mkdir -p /run/ytpdl-slots /run/ytpdl-active
chmod 700 /run/ytpdl-slots /run/ytpdl-active

log "7) Install Gunicorn systemd service"
cat > /etc/systemd/system/ytp-dl-api.service <<EOF2
[Unit]
Description=Gunicorn for ytp-dl API
After=network-online.target ytpdl-vpn-namespace.service
Wants=network-online.target ytpdl-vpn-namespace.service

[Service]
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/default/ytp-dl-api
Environment=VIRTUAL_ENV=${VENV_DIR}
Environment=PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
RuntimeDirectory=ytpdl-slots ytpdl-active
RuntimeDirectoryMode=0700
ExecStart=${VENV_DIR}/bin/gunicorn \
    -k gthread \
    -w \${GUNICORN_WORKERS} \
    --threads \${GUNICORN_THREADS} \
    --timeout 0 \
    --graceful-timeout 15 \
    --keep-alive 20 \
    --bind 0.0.0.0:${PORT} \
    scripts.api:app
Restart=always
RestartSec=3
LimitNOFILE=65535
MemoryMax=${YTPDL_MEMORY_MAX}

[Install]
WantedBy=multi-user.target
EOF2

systemctl daemon-reload
systemctl enable --now ytp-dl-api.service
sleep 3

log "8) Verify API and isolated VPN"
curl -fsS --connect-timeout 5 "http://127.0.0.1:${PORT}/healthz"
echo

# This test touches only the isolated namespace. It must not alter the host's
# default route, SSH connectivity or public API route.
if "${VPN_HELPER}" ensure; then
    echo "VPN namespace check: OK"
    "${VPN_HELPER}" status || true
    echo -n "VPN exit IP: "
    "${VPN_HELPER}" ip || true
    echo
else
    echo "WARNING: API is installed, but the isolated Mullvad tunnel test failed." >&2
    echo "Check: ${VPN_HELPER} status" >&2
fi

log "Installation complete"
echo "API: http://0.0.0.0:${PORT}"
echo "Global download capacity: ${YTPDL_MAX_CONCURRENT} jobs"
echo "Free-disk reserve: ${YTPDL_MIN_FREE_DISK_MB} MB"
echo "Gunicorn: ${GUNICORN_WORKERS} workers x ${GUNICORN_THREADS} threads"
echo "Service memory limit: ${YTPDL_MEMORY_MAX}"
echo "VPN namespace: ${VPN_NAMESPACE}"
echo "Mullvad relay filter: ${YTPDL_MULLVAD_LOCATION}"
echo
echo "Useful commands:"
echo "  systemctl status ytp-dl-api.service --no-pager"
echo "  journalctl -u ytp-dl-api.service -f"
echo "  ${VPN_HELPER} status"
echo "  ${VPN_HELPER} ip"
echo "  ${VPN_HELPER} rotate"
echo "  ip netns exec ${VPN_NAMESPACE} ip addr"
```

## Python client example

The following script shows how to call a deployed ytp-dl API from Python, stream download output, and fetch the completed file.

### Usage

```bash
# MP4
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --extension mp4 --resolution 1080

# MP3
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://soundcloud.com/artist/track" --extension mp3

# Playlist
python3 ytp-dl.py --base "http://YOUR_VPS_IP:5000" --url "https://www.youtube.com/playlist?list=PLxxx" --extension mp4 --out-dir ./downloads
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
the same job_id so the VPS .ytdlp-archive can resume playlist work.

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
    Stream SSE logs from the VPS download endpoint.

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
        help="Base URL, e.g. http://YOUR_VPS_IP:5000 (env: YTPDL_BASE)",
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
