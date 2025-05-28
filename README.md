# ytp-dl · v0.2.85

**ytp-dl** is a headless YouTube downloader that tunnels all traffic through
Mullvad VPN and exposes an optional Flask API.  
It's packaged for one‑command deployment on a fresh Ubuntu VPS.

---

## Features

* `ytp-dl` — CLI: download a video or audio with one line
* `ytp-dl-api` — Flask server for remote JSON downloads
* Hard‑coded virtual‑env path `/opt/yt-dlp-mullvad/venv` for consistency
* Automatic yt‑dlp, thumbnail, and metadata embedding
* Clear environment validation & helpful error messages

---

## VPS Installation Guide (PyPI workflow)

**Tested on Ubuntu 22.04 DigitalOcean droplets**

```bash
# 1) SSH in
ssh root@<droplet_ip>

# 2) OS prerequisites
sudo apt update && sudo apt install -y python3-venv python3-pip curl ffmpeg

# 3) Mullvad CLI
curl -fsSLo /tmp/mullvad.deb https://mullvad.net/download/app/deb/latest/
sudo apt install -y /tmp/mullvad.deb

# 4) Project directory + venv (must match package expectations)
mkdir -p /opt/yt-dlp-mullvad
python3 -m venv /opt/yt-dlp-mullvad/venv
source /opt/yt-dlp-mullvad/venv/bin/activate

# 5) Install from PyPI
pip install --upgrade pip
pip install ytp-dl==0.2.85
```

### Quick smoke‑test

```bash
ytp-dl "https://youtu.be/dQw4w9WgXcQ" <mullvad_account> --resolution 720
# Expect: DOWNLOADED_FILE:/root/Rick Astley - Never Gonna Give You Up.mp4
```

### Persist the API with systemd

```bash
sudo tee /etc/systemd/system/ytp-dl-api.service > /dev/null <<'EOF'
[Unit]
Description=Flask API for ytp-dl Mullvad Downloader
After=network.target

[Service]
User=root
WorkingDirectory=/opt/yt-dlp-mullvad
Environment=VIRTUAL_ENV=/opt/yt-dlp-mullvad/venv
Environment=PATH=/opt/yt-dlp-mullvad/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=/opt/yt-dlp-mullvad/venv/bin/ytp-dl-api --host 0.0.0.0 --port 5000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ytp-dl-api
systemctl status ytp-dl-api
```

Once running, download via HTTP:

```bash
curl -X POST http://<droplet_ip>:5000/api/download \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://youtu.be/dQw4w9WgXcQ","mullvad_account":"<acct>"}' \
     -O -J
```

---

## CLI Examples

```bash
# Best quality video (default mp4):
ytp-dl "<url>" <acct>

# Force 1080p WebM:
ytp-dl "<url>" <acct> --resolution 1080 --extension webm

# Extract audio as MP3:
ytp-dl "<url>" <acct> --extension mp3
```

---

## Local Python Script

```python
#!/usr/bin/env python3
import requests
import sys
import os
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument("--resolution", help="Desired resolution (e.g., 1080)", default=None)
    parser.add_argument("--extension", help="Desired file extension (e.g., mp4, mp3)", default=None)
    args = parser.parse_args()

    mullvad_account = ""
    api_url = "http://<droplet_ip>:5000/api/download"

    payload = {"url": args.url, "mullvad_account": mullvad_account}
    if args.resolution:
        payload["resolution"] = args.resolution
    if args.extension:
        payload["extension"] = args.extension

    try:
        with requests.post(api_url, json=payload, stream=True) as response:
            if response.status_code != 200:
                print(f"Error: API returned status code {response.status_code}")
                if response.headers.get("Content-Type") == "application/json":
                    print(response.json().get("error", "Unknown error"))
                else:
                    print("Non-JSON response:", response.text)
                sys.exit(1)

            content_disposition = response.headers.get("Content-Disposition")
            filename = None
            if content_disposition and "filename=" in content_disposition:
                filename = content_disposition.split("filename=")[1].strip('"')
            else:
                filename = "downloaded_video.mp4"

            if os.path.exists(filename):
                print(f"File already exists: {filename}")
                sys.exit(0)

            total_size = int(response.headers.get('Content-Length', 0))
            if total_size == 0:
                print("Warning: Content-Length header is missing or zero. Progress bar may not be accurate.")

            downloaded_size = 0
            chunk_size = 8192

            with open(filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            print(f"\rDownloading: {progress:.2f}% ({downloaded_size}/{total_size} bytes)", end="", flush=True)
                        else:
                            print(f"\rDownloaded: {downloaded_size} bytes", end="", flush=True)

            print(f"\nVideo downloaded successfully: {filename}")

    except requests.RequestException as e:
        print(f"Error connecting to the API: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### Setup

```bash
# Install required dependency
pip install requests
```

### Usage

```bash
# Basic download
python ytp-dl.py "https://youtu.be/dQw4w9WgXcQ"

# With specific resolution
python ytp-dl.py "https://youtu.be/dQw4w9WgXcQ" --resolution 1080

# With specific format
python ytp-dl.py "https://youtu.be/dQw4w9WgXcQ" --extension mp3

# Combined options
python ytp-dl.py "https://youtu.be/dQw4w9WgXcQ" --resolution 720 --extension webm
```

**Note:** You'll need to update the `<droplet_ip>` and `mullvad_account` variables in the script before use.

---

## License
MIT – © dumgum82 2025