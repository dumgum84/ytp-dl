#!/usr/bin/env bash
#
# ytp-dl VPS installer
#
# Sets up a complete ytp-dl server on Ubuntu, including:
#   - Python virtual environment and ytp-dl
#   - Gunicorn API service
#   - FFmpeg and Deno
#   - Isolated Mullvad WireGuard network namespace for yt-dlp traffic
#   - Automatic Mullvad relay recovery and device reconciliation
#   - Disk-space protection for active downloads
#   - Optional Cloudflare R2 storage
#
# SSH, the API service, and normal host traffic remain on the server's
# standard network connection. Only yt-dlp and its child processes
# are routed through Mullvad.
#
# Recommended: Ubuntu 24.04 LTS

set -euo pipefail

# Preserve the currently deployed API/R2 tuning when this is used as an upgrade.
if [[ -f /etc/default/ytp-dl-api ]]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/default/ytp-dl-api
    set +a
fi
if [[ -f /etc/default/ytpdl-vpn ]]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/default/ytpdl-vpn
    set +a
fi

PORT="${PORT:-5000}"
APP_DIR="${APP_DIR:-/opt/yt-dlp-mullvad}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"
YTPDL_VERSION="${YTPDL_VERSION:-2026.11.1}"
# Prefer the wheel alongside the installer; when downloaded on its own from
# GitHub, install this release from PyPI after it has been published.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YTPDL_BUNDLED_WHEEL="${SCRIPT_DIR}/dist/ytp_dl-${YTPDL_VERSION}-py3-none-any.whl"
if [[ -f "${YTPDL_BUNDLED_WHEEL}" ]]; then
    YTPDL_DEFAULT_PACKAGE_SPEC="${YTPDL_BUNDLED_WHEEL}"
else
    YTPDL_DEFAULT_PACKAGE_SPEC="ytp-dl==${YTPDL_VERSION}"
fi
YTPDL_PACKAGE_SPEC="${YTPDL_PACKAGE_SPEC:-${YTPDL_DEFAULT_PACKAGE_SPEC}}"

# Mullvad / isolated WireGuard namespace.
MV_ACCOUNT="${MV_ACCOUNT:-${YTPDL_MULLVAD_ACCOUNT:-}}"
YTPDL_MULLVAD_LOCATION="${YTPDL_MULLVAD_LOCATION:-us}"
VPN_NAMESPACE="${VPN_NAMESPACE:-${YTPDL_VPN_NAMESPACE:-ytpdl-vpn}}"
VPN_DIR="${VPN_DIR:-/etc/ytpdl-vpn}"
VPN_CONFIG_DIR="${VPN_CONFIG_DIR:-${YTPDL_VPN_CONFIG_DIR:-${VPN_DIR}/configs}}"
VPN_HELPER="${VPN_HELPER:-${YTPDL_VPN_HELPER:-/usr/local/sbin/ytpdl-vpn}}"
VPN_MTU="${VPN_MTU:-${YTPDL_VPN_MTU:-1420}}"
VPN_ROTATE_COOLDOWN="${VPN_ROTATE_COOLDOWN:-${YTPDL_VPN_ROTATE_COOLDOWN:-10}}"
YTPDL_MULLVAD_AUTO_EVICT="${YTPDL_MULLVAD_AUTO_EVICT:-1}"
YTPDL_MULLVAD_PROTECTED_KEYS="${YTPDL_MULLVAD_PROTECTED_KEYS:-}"
YTPDL_MULLVAD_CONFIG_REFRESH_S="${YTPDL_MULLVAD_CONFIG_REFRESH_S:-21600}"
YTPDL_VPN_LOCK_TIMEOUT_S="${YTPDL_VPN_LOCK_TIMEOUT_S:-120}"
YTPDL_MULLVAD_RECONCILE_INTERVAL_MIN="${YTPDL_MULLVAD_RECONCILE_INTERVAL_MIN:-15}"

# API / Gunicorn. These are the bundled installer's starting defaults when no
# previous /etc/default/ytp-dl-api exists. Tune them for the host's resources.
YTPDL_MAX_CONCURRENT="${YTPDL_MAX_CONCURRENT:-1}"
YTPDL_SOCKET_TIMEOUT_S="${YTPDL_SOCKET_TIMEOUT_S:-10}"
YTPDL_VPN_RECOVERY_TIMEOUT_S="${YTPDL_VPN_RECOVERY_TIMEOUT_S:-180}"
YTPDL_MIN_FREE_DISK_MB="${YTPDL_MIN_FREE_DISK_MB:-8192}"
YTPDL_DISK_CHECK_INTERVAL_S="${YTPDL_DISK_CHECK_INTERVAL_S:-1.0}"
YTPDL_R2_ZIP_PART_SIZE_MB="${YTPDL_R2_ZIP_PART_SIZE_MB:-16}"
YTPDL_R2_ZIP_WORKERS="${YTPDL_R2_ZIP_WORKERS:-6}"
YTPDL_MEMORY_MAX="${YTPDL_MEMORY_MAX:-2G}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
YTPDL_VPS_API_TOKEN="${YTPDL_VPS_API_TOKEN:-}"
YTPDL_JOB_BASE_DIR="${YTPDL_JOB_BASE_DIR:-/root/ytpdl_jobs}"

# R2. Existing values are preserved automatically on upgrade.
YTPDL_R2_UPLOAD="${YTPDL_R2_UPLOAD:-0}"
R2_ENDPOINT="${R2_ENDPOINT:-}"
R2_BUCKET="${R2_BUCKET:-}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
export AWS_EC2_METADATA_DISABLED="true"

[[ "${EUID}" -eq 0 ]] || { echo "Please run as root (sudo -s)" >&2; exit 1; }

if [[ -z "${YTPDL_VPS_API_TOKEN}" ]]; then
    echo "YTPDL_VPS_API_TOKEN is required for an internet-accessible API." >&2
    echo "Set it in your deployment environment or existing /etc/default/ytp-dl-api." >&2
    exit 1
fi
if [[ "${YTPDL_R2_UPLOAD}" == "1" ]]; then
    for required_var in R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
        if [[ -z "${!required_var}" ]]; then
            echo "R2 is enabled but ${required_var} is not configured." >&2
            exit 1
        fi
    done
fi


if [[ -z "${MV_ACCOUNT}" ]]; then
    read -rsp "Mullvad account number: " MV_ACCOUNT
    echo
fi
[[ "${MV_ACCOUNT}" =~ ^[0-9]+$ ]] || { echo "A numeric Mullvad account number is required." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
log() { printf '\n==> %s\n' "$*"; }

log "0) Stop API and remove obsolete host-level Mullvad routing"
systemctl stop ytp-dl-api.service 2>/dev/null || true

if command -v mullvad >/dev/null 2>&1; then
    mullvad disconnect >/dev/null 2>&1 || true
fi
systemctl disable --now mullvad-daemon.service 2>/dev/null || true
systemctl disable --now mullvad-early-boot-blocking.service 2>/dev/null || true
systemctl disable --now ytpdl-policy-routing.service 2>/dev/null || true
systemctl disable --now ytpdl-mullvad-exclude-ports.service 2>/dev/null || true
rm -f /etc/systemd/system/ytpdl-policy-routing.service \
      /etc/systemd/system/ytpdl-mullvad-exclude-ports.service \
      /usr/local/sbin/ytpdl-policy-routing.sh \
      /usr/local/sbin/ytpdl-mullvad-exclusions.sh \
      /etc/ytpdl-mullvad-exclude-ports.nft \
      /etc/default/ytpdl-policy-routing \
      /etc/sysctl.d/99-ytpdl-policy-routing.conf
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
modprobe wireguard

log "2) Install Mullvad generator and self-healing reconciler"
install -d -m 700 "${VPN_DIR}" "${VPN_CONFIG_DIR}"
install -d -m 755 /usr/local/lib/ytpdl

curl -fsSLo /usr/local/lib/ytpdl/wg-mullvad.py \
    https://raw.githubusercontent.com/mullvad/wg-tools/main/wg-mullvad.py
chmod 755 /usr/local/lib/ytpdl/wg-mullvad.py

cat > /usr/local/lib/ytpdl/mullvad-reconcile.py <<'PYEOF'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import gzip
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.mullvad.net"


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def response_json(response):
    raw = response.read()
    if not raw:
        return None
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


class MullvadApi:
    def __init__(self, account: str):
        self.account = account
        self._token: str | None = None

    def token(self) -> str:
        if self._token:
            return self._token
        req = urllib.request.Request(
            f"{API}/auth/v1/webtoken",
            data=json.dumps({"account_number": self.account}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = response_json(r) or {}
        self._token = str(data.get("access_token") or "")
        if not self._token:
            raise RuntimeError("Mullvad authentication returned no access token")
        return self._token

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token()}")
        req.add_header("Accept-Encoding", "gzip")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            return response_json(r)

    def devices(self) -> list[dict]:
        data = self.request("GET", "/accounts/v1/devices")
        return data if isinstance(data, list) else []

    def max_devices(self) -> int:
        try:
            data = self.request("GET", "/accounts/v1/accounts/me") or {}
            return max(1, int(data.get("max_devices", 5)))
        except Exception:
            return 5

    def delete_device(self, device_id: str) -> None:
        safe = urllib.parse.quote(str(device_id), safe="")
        self.request("DELETE", f"/accounts/v1/devices/{safe}")


def read_private_key(settings_file: pathlib.Path) -> str:
    cfg = configparser.ConfigParser()
    cfg.read(settings_file)
    try:
        return cfg.get("Interface", "privatekey").strip()
    except (configparser.NoOptionError, configparser.NoSectionError) as e:
        raise RuntimeError(f"No WireGuard private key in {settings_file}") from e


def public_key(private_key: str) -> str:
    p = subprocess.run(
        ["wg", "pubkey"], input=private_key + "\n", text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
    )
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"Could not derive WireGuard public key: {p.stderr.strip()}")
    return p.stdout.strip()


def split_keys(raw: str) -> set[str]:
    for ch in ";\n\t ":
        raw = raw.replace(ch, ",")
    return {x.strip() for x in raw.split(",") if x.strip()}


def config_age_s(config_dir: pathlib.Path) -> float:
    try:
        mtimes = [p.stat().st_mtime for p in config_dir.glob("*.conf") if p.is_file()]
        if not mtimes:
            return float("inf")
        return max(0.0, time.time() - max(mtimes))
    except Exception:
        return float("inf")


def install_configs(*, generator: pathlib.Path, account: str, location: str,
                    settings_file: pathlib.Path, config_dir: pathlib.Path,
                    expected_pubkey: str) -> dict:
    parent = config_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".configs-new-", dir=str(parent)))
    backup = parent / ".configs-prev"
    try:
        cmd = [
            sys.executable, str(generator),
            "--account", account,
            "--filter", location,
            "--active",
            "--settings-file", str(settings_file),
            "--output-dir", str(staging),
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=120)
        if p.stdout:
            print(p.stdout.rstrip())
        if p.returncode != 0:
            out = (p.stdout or "").lower()
            if "pubkey_in_use" in out or "grace period" in out or "5 min" in out:
                raise RuntimeError(
                    "Mullvad is still holding the removed key in its grace period; "
                    "the periodic reconciler will retry automatically."
                )
            raise RuntimeError(f"Mullvad config generator failed with exit code {p.returncode}")

        configs = sorted(staging.glob("*.conf"))
        if not configs:
            raise RuntimeError("Mullvad generator produced no WireGuard configs")

        first_cfg = configparser.ConfigParser()
        first_cfg.read(configs[0])
        generated_private = first_cfg.get("Interface", "privatekey").strip()
        if public_key(generated_private) != expected_pubkey:
            raise RuntimeError("Generated configs do not use the persistent server WireGuard key")

        for cfg in configs:
            os.chmod(cfg, 0o600)

        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        moved_old = False
        try:
            if config_dir.exists():
                os.replace(config_dir, backup)
                moved_old = True
            os.replace(staging, config_dir)
            staging = None
        except Exception:
            # Never strand the VPN without a config directory if the final rename
            # fails after the old set was moved aside.
            if moved_old and backup.exists() and not config_dir.exists():
                os.replace(backup, config_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        print(f"Installed {len(configs)} refreshed Mullvad relay configs atomically.")
        return {"config_count": len(configs)}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def write_state(path: pathlib.Path, data: dict) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("check", "repair"), default="check")
    args = parser.parse_args()

    account = os.environ.get("YTPDL_MULLVAD_ACCOUNT", "").strip()
    location = os.environ.get("YTPDL_MULLVAD_LOCATION", "us").strip() or "us"
    vpn_dir = pathlib.Path(os.environ.get("YTPDL_VPN_DIR", "/etc/ytpdl-vpn"))
    config_dir = pathlib.Path(os.environ.get("YTPDL_VPN_CONFIG_DIR", str(vpn_dir / "configs")))
    settings_file = pathlib.Path(os.environ.get("YTPDL_MULLVAD_SETTINGS_FILE", str(vpn_dir / "device.conf")))
    generator = pathlib.Path(os.environ.get("YTPDL_MULLVAD_GENERATOR", "/usr/local/lib/ytpdl/wg-mullvad.py"))
    protected = split_keys(os.environ.get("YTPDL_MULLVAD_PROTECTED_KEYS", ""))
    auto_evict = truthy(os.environ.get("YTPDL_MULLVAD_AUTO_EVICT", "1"))
    refresh_s = max(300, int(os.environ.get("YTPDL_MULLVAD_CONFIG_REFRESH_S", "21600")))
    state_file = vpn_dir / "reconcile-state.json"

    if not account or not account.isdigit():
        raise RuntimeError("YTPDL_MULLVAD_ACCOUNT is missing or invalid")
    if not settings_file.is_file():
        raise RuntimeError(f"Missing Mullvad settings file: {settings_file}")
    if not generator.is_file():
        raise RuntimeError(f"Missing Mullvad generator: {generator}")

    private = read_private_key(settings_file)
    pubkey = public_key(private)
    api = MullvadApi(account)
    devices = api.devices()
    device = next((d for d in devices if d.get("pubkey") == pubkey), None)
    changed = False
    device_repaired = False

    if device is None:
        print(f"VPS Mullvad device is not registered: {pubkey}")
        max_devices = api.max_devices()
        if len(devices) >= max_devices:
            if not auto_evict:
                raise RuntimeError(
                    f"Mullvad device limit reached ({len(devices)}/{max_devices}) and auto-eviction is disabled"
                )
            candidates = [
                d for d in devices
                if d.get("pubkey") != pubkey and d.get("pubkey") not in protected
            ]
            if not candidates:
                raise RuntimeError(
                    "Mullvad device limit reached, but every non-VPS device is protected; "
                    "no autonomous eviction candidate exists"
                )
            candidates.sort(key=lambda d: str(d.get("created") or ""))
            victim = candidates[0]
            print(
                "Mullvad account full; evicting exactly one unprotected device: "
                f"{victim.get('name', 'unknown')} ({victim.get('pubkey', 'unknown')})"
            )
            victim_id = str(victim.get("id") or "").strip()
            if not victim_id:
                raise RuntimeError("Selected Mullvad eviction candidate has no device id")
            api.delete_device(victim_id)
            changed = True
            # Refresh the list so state/logging reflects the post-delete account.
            devices = api.devices()

        print("Re-registering the persistent server WireGuard key and refreshing relays.")
        install_configs(
            generator=generator, account=account, location=location,
            settings_file=settings_file, config_dir=config_dir,
            expected_pubkey=pubkey,
        )
        changed = True
        device_repaired = True
        devices = api.devices()
        device = next((d for d in devices if d.get("pubkey") == pubkey), None)
        if device is None:
            raise RuntimeError("Mullvad generator completed, but the VPS device is still not registered")
    else:
        age = config_age_s(config_dir)
        if args.mode == "repair" or age >= refresh_s:
            why = "repair requested" if args.mode == "repair" else f"configs are {age:.0f}s old"
            print(f"VPS Mullvad device is registered; refreshing relay configs ({why}).")
            install_configs(
                generator=generator, account=account, location=location,
                settings_file=settings_file, config_dir=config_dir,
                expected_pubkey=pubkey,
            )
            changed = True
        else:
            print(
                f"Mullvad device healthy: {device.get('name', 'unknown')} {pubkey}; "
                f"relay configs are fresh ({age:.0f}s old)."
            )

    write_state(state_file, {
        "checked_at": int(time.time()),
        "changed": changed,
        "device_repaired": device_repaired,
        "device_id": (device or {}).get("id"),
        "device_name": (device or {}).get("name"),
        "public_key": pubkey,
        "config_count": len(list(config_dir.glob("*.conf"))),
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"Mullvad API error HTTP {e.code}: {body or e.reason}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as e:
        print(f"Mullvad reconciliation failed: {e}", file=sys.stderr)
        raise SystemExit(1)
PYEOF
chmod 755 /usr/local/lib/ytpdl/mullvad-reconcile.py

# Persist the account because autonomous repair cannot ask for it later.
cat > /etc/default/ytpdl-vpn <<EOF2
YTPDL_VPN_NAMESPACE=${VPN_NAMESPACE}
YTPDL_VPN_DIR=${VPN_DIR}
YTPDL_VPN_CONFIG_DIR=${VPN_CONFIG_DIR}
YTPDL_VPN_MTU=${VPN_MTU}
YTPDL_VPN_ROTATE_COOLDOWN=${VPN_ROTATE_COOLDOWN}
YTPDL_MULLVAD_ACCOUNT=${MV_ACCOUNT}
YTPDL_MULLVAD_LOCATION=${YTPDL_MULLVAD_LOCATION}
YTPDL_MULLVAD_SETTINGS_FILE=${VPN_DIR}/device.conf
YTPDL_MULLVAD_GENERATOR=/usr/local/lib/ytpdl/wg-mullvad.py
YTPDL_MULLVAD_AUTO_EVICT=${YTPDL_MULLVAD_AUTO_EVICT}
YTPDL_MULLVAD_PROTECTED_KEYS=${YTPDL_MULLVAD_PROTECTED_KEYS}
YTPDL_MULLVAD_CONFIG_REFRESH_S=${YTPDL_MULLVAD_CONFIG_REFRESH_S}
YTPDL_VPN_LOCK_TIMEOUT_S=${YTPDL_VPN_LOCK_TIMEOUT_S}
EOF2
chmod 600 /etc/default/ytpdl-vpn

# Export the just-written settings for the initial reconciliation.
set -a
# shellcheck disable=SC1091
source /etc/default/ytpdl-vpn
set +a

# If this is a fresh install, the official generator creates device.conf first.
if [[ ! -f "${VPN_DIR}/device.conf" ]]; then
    # On a fresh install the official generator creates the persistent private
    # key. If the account is already full it may fail after writing device.conf;
    # in that case immediately hand control to the autonomous reconciler, which
    # can evict one eligible device and register this same newly-created key.
    if ! python3 /usr/local/lib/ytpdl/wg-mullvad.py \
        --account "${MV_ACCOUNT}" \
        --filter "${YTPDL_MULLVAD_LOCATION}" \
        --active \
        --settings-file "${VPN_DIR}/device.conf" \
        --output-dir "${VPN_CONFIG_DIR}"; then
        [[ -f "${VPN_DIR}/device.conf" ]] || {
            echo "Mullvad generator failed before creating device.conf." >&2
            exit 1
        }
        python3 /usr/local/lib/ytpdl/mullvad-reconcile.py --mode repair
    fi
    chmod 600 "${VPN_DIR}/device.conf" "${VPN_CONFIG_DIR}"/*.conf
else
    python3 /usr/local/lib/ytpdl/mullvad-reconcile.py --mode repair
fi

if ! find "${VPN_CONFIG_DIR}" -maxdepth 1 -type f -name '*.conf' -print -quit | grep -q .; then
    echo "No Mullvad WireGuard configs are available for '${YTPDL_MULLVAD_LOCATION}'." >&2
    exit 1
fi

log "3) Install isolated VPN helper with account/config self-repair"
cat > "${VPN_HELPER}" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail

# Export the root-only settings so the Python reconciler invoked below receives
# the Mullvad account, location, protected-key policy, and config paths.
set -a
source /etc/default/ytpdl-vpn
set +a

NS="${YTPDL_VPN_NAMESPACE:-ytpdl-vpn}"
CONFIG_DIR="${YTPDL_VPN_CONFIG_DIR:-/etc/ytpdl-vpn/configs}"
WG_IF="ytpdlwg"
MTU="${YTPDL_VPN_MTU:-1420}"
ROTATE_COOLDOWN="${YTPDL_VPN_ROTATE_COOLDOWN:-10}"
LOCK_TIMEOUT="${YTPDL_VPN_LOCK_TIMEOUT_S:-120}"
RUN_DIR="/run/ytpdl-vpn"
LOCK_FILE="/run/lock/ytpdl-vpn.lock"
CURRENT_FILE="${RUN_DIR}/current-config"
LAST_ROTATE_FILE="${RUN_DIR}/last-rotate"
STRIPPED_FILE="${RUN_DIR}/wireguard-stripped.conf"
RECONCILER="/usr/local/lib/ytpdl/mullvad-reconcile.py"
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
    ip netns exec "${NS}" curl -fsS --connect-timeout 2 --max-time 3 "${CHECK_URL}" 2>/dev/null \
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

    wg-quick strip "${cfg}" > "${STRIPPED_FILE}"
    chmod 600 "${STRIPPED_FILE}"

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

    local i
    for i in 1 2; do
        if connected; then
            printf '%s\n' "${cfg}" > "${CURRENT_FILE}"
            chmod 600 "${CURRENT_FILE}"
            echo "Connected: $(basename "${cfg}")"
            return 0
        fi
        (( i < 2 )) && sleep 1
    done

    ip -n "${NS}" link del "${WG_IF}" 2>/dev/null || true
    return 1
}

connect_from_candidates() {
    local avoid="${1:-}"
    local -a configs=()
    mapfile -t configs < <(list_configs_random)
    (( ${#configs[@]} > 0 )) || return 1

    local cfg attempts=0
    for cfg in "${configs[@]}"; do
        [[ -n "${avoid}" && "${cfg}" == "${avoid}" && ${#configs[@]} -gt 1 ]] && continue
        attempts=$((attempts + 1))
        echo "Trying relay: $(basename "${cfg}")"
        if connect_config "${cfg}"; then
            return 0
        fi
        (( attempts >= 4 )) && break
    done
    return 1
}

repair_mullvad_locked() {
    echo "Reconciling Mullvad VPS device and relay configs."
    python3 "${RECONCILER}" --mode repair || return 1
    rm -f "${CURRENT_FILE}"
    return 0
}

ensure_locked() {
    setup_namespace
    if connected; then
        return 0
    fi

    local cur=""
    cur="$(current_config || true)"
    if [[ -n "${cur}" ]]; then
        echo "Current Mullvad relay unavailable; failing over from $(basename "${cur}")."
    else
        echo "Mullvad tunnel unavailable; selecting a relay."
    fi

    if connect_from_candidates "${cur}"; then
        return 0
    fi

    # Four unrelated relay failures usually means the common device identity or
    # saved relay set is stale. Repair the account/config layer, then try again.
    echo "Relay failover exhausted; attempting Mullvad account/config self-repair."
    repair_mullvad_locked || die "Mullvad account/config self-repair failed"
    connect_from_candidates "" || die "Unable to establish Mullvad WireGuard tunnel after self-repair"
}

rotate_locked() {
    setup_namespace

    local now last=0
    now="$(date +%s)"
    if [[ -s "${LAST_ROTATE_FILE}" ]]; then
        last="$(cat "${LAST_ROTATE_FILE}" 2>/dev/null || echo 0)"
    fi

    if (( now - last < ROTATE_COOLDOWN )) && connected; then
        echo "Rotation already completed recently; keeping fresh relay."
        return 0
    fi

    local cur=""
    cur="$(current_config || true)"
    if ! connect_from_candidates "${cur}"; then
        echo "Normal rotation failed; attempting Mullvad account/config self-repair."
        repair_mullvad_locked || die "Mullvad account/config self-repair failed"
        connect_from_candidates "" || die "Unable to rotate Mullvad WireGuard tunnel after self-repair"
    fi
    date +%s > "${LAST_ROTATE_FILE}"
    chmod 600 "${LAST_ROTATE_FILE}"
}

reconcile_locked() {
    setup_namespace
    python3 "${RECONCILER}" --mode check || return 1

    # If the account layer had to recreate this VPS device, reconnect even when
    # the pre-repair interface still answers momentarily: Mullvad may have issued
    # a fresh tunnel address and the live interface must match the refreshed config.
    local device_repaired="false"
    if [[ -s "${YTPDL_VPN_DIR:-/etc/ytpdl-vpn}/reconcile-state.json" ]]; then
        device_repaired="$(python3 -c 'import json,sys; print(str(bool(json.load(open(sys.argv[1])).get("device_repaired"))).lower())' \
            "${YTPDL_VPN_DIR:-/etc/ytpdl-vpn}/reconcile-state.json" 2>/dev/null || echo false)"
    fi

    if [[ "${device_repaired}" == "true" ]] || ! connected; then
        down_locked
        rm -f "${CURRENT_FILE}"
        connect_from_candidates "" || return 1
    fi
}

with_lock() {
    exec 9>"${LOCK_FILE}"
    flock -w "${LOCK_TIMEOUT}" -x 9 || die "Timed out waiting for Mullvad recovery lock"
    "$@"
}

command="${1:-}"
[[ -n "${command}" ]] || die "Usage: ytpdl-vpn namespace|ensure|rotate|reconcile|status|ip|down|exec -- COMMAND..."
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
    reconcile)
        with_lock reconcile_locked
        ;;
    status)
        if connected; then
            echo "Connected"
            if [[ -s "${CURRENT_FILE}" ]]; then
                echo "Relay config: $(basename "$(cat "${CURRENT_FILE}")")"
            fi
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

cat > /etc/systemd/system/ytpdl-vpn-reconcile.service <<EOF2
[Unit]
Description=ytp-dl Mullvad device/config reconciliation
After=network-online.target ytpdl-vpn-namespace.service
Wants=network-online.target ytpdl-vpn-namespace.service

[Service]
Type=oneshot
ExecStart=${VPN_HELPER} reconcile
EOF2

cat > /etc/systemd/system/ytpdl-vpn-reconcile.timer <<EOF2
[Unit]
Description=Periodically verify ytp-dl Mullvad identity and relay configs

[Timer]
OnBootSec=5min
OnUnitActiveSec=${YTPDL_MULLVAD_RECONCILE_INTERVAL_MIN}min
RandomizedDelaySec=60
Persistent=true
Unit=ytpdl-vpn-reconcile.service

[Install]
WantedBy=timers.target
EOF2

systemctl daemon-reload
systemctl enable --now ytpdl-vpn-namespace.service
systemctl enable --now ytpdl-vpn-reconcile.timer

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
pip install --upgrade "${YTPDL_PACKAGE_SPEC}"
ACTUAL_YTPDL_VERSION="$(python -c 'from importlib.metadata import version; print(version("ytp-dl"))')"
if [[ "${ACTUAL_YTPDL_VERSION}" != "${YTPDL_VERSION}" ]]; then
    echo "Installed ytp-dl version ${ACTUAL_YTPDL_VERSION}, expected ${YTPDL_VERSION}." >&2
    exit 1
fi
pip install --upgrade --pre "yt-dlp[default,curl-cffi]"
if [[ "${YTPDL_R2_UPLOAD}" == "1" ]]; then
    pip install --upgrade boto3
fi
deactivate

log "6) Configure API environment"
cat > /etc/default/ytp-dl-api <<EOF2
YTPDL_MAX_CONCURRENT=${YTPDL_MAX_CONCURRENT}
YTPDL_SOCKET_TIMEOUT_S=${YTPDL_SOCKET_TIMEOUT_S}
YTPDL_VPN_RECOVERY_TIMEOUT_S=${YTPDL_VPN_RECOVERY_TIMEOUT_S}
YTPDL_VENV=${VENV_DIR}
YTPDL_VPN_HELPER=${VPN_HELPER}
YTPDL_SLOT_DIR=/run/ytpdl-slots
YTPDL_ACTIVE_LOCK_DIR=/run/ytpdl-active
YTPDL_JOB_BASE_DIR=${YTPDL_JOB_BASE_DIR}
YTPDL_MIN_FREE_DISK_MB=${YTPDL_MIN_FREE_DISK_MB}
YTPDL_DISK_CHECK_INTERVAL_S=${YTPDL_DISK_CHECK_INTERVAL_S}
YTPDL_DISK_PRESSURE_LOCK_PATH=/run/lock/ytpdl-disk-pressure.lock
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
mkdir -p /run/ytpdl-slots /run/ytpdl-active "${YTPDL_JOB_BASE_DIR}"
chmod 700 /run/ytpdl-slots /run/ytpdl-active "${YTPDL_JOB_BASE_DIR}"

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

log "8) Verify API, disk readiness, and isolated Mullvad tunnel"
curl -fsS --connect-timeout 5 "http://127.0.0.1:${PORT}/healthz"
echo

if "${VPN_HELPER}" ensure; then
    echo "VPN namespace check: OK"
    "${VPN_HELPER}" status || true
    echo -n "VPN exit IP: "
    "${VPN_HELPER}" ip || true
    echo
else
    echo "ERROR: isolated Mullvad tunnel could not be established." >&2
    echo "Check: ${VPN_HELPER} reconcile" >&2
    exit 1
fi

log "Installation complete"
echo "ytp-dl version: ${YTPDL_VERSION}"
echo "API: http://0.0.0.0:${PORT}"
echo "Global download capacity: ${YTPDL_MAX_CONCURRENT} jobs"
echo "Free-disk reserve: ${YTPDL_MIN_FREE_DISK_MB} MB"
echo "Disk-pressure check: every ${YTPDL_DISK_CHECK_INTERVAL_S}s"
echo "VPN recovery timeout: ${YTPDL_VPN_RECOVERY_TIMEOUT_S}s"
echo "Gunicorn: ${GUNICORN_WORKERS} workers x ${GUNICORN_THREADS} threads"
echo "Service memory limit: ${YTPDL_MEMORY_MAX}"
echo "VPN namespace: ${VPN_NAMESPACE}"
echo "Mullvad relay filter: ${YTPDL_MULLVAD_LOCATION}"
echo "Mullvad auto-eviction: ${YTPDL_MULLVAD_AUTO_EVICT}"
echo "Mullvad reconcile timer: every ${YTPDL_MULLVAD_RECONCILE_INTERVAL_MIN} min"
echo
echo "Useful commands:"
echo "  systemctl status ytp-dl-api.service --no-pager"
echo "  journalctl -u ytp-dl-api.service -f"
echo "  ${VPN_HELPER} status"
echo "  ${VPN_HELPER} ip"
echo "  ${VPN_HELPER} rotate"
echo "  ${VPN_HELPER} reconcile"
echo "  systemctl list-timers ytpdl-vpn-reconcile.timer --no-pager"
echo "  ip netns exec ${VPN_NAMESPACE} wg show"
