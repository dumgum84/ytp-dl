#!/usr/bin/env python3
import subprocess
import sys
import os
import time
import shutil

def run_command(cmd, check=True):
    try:
        result = subprocess.run(
            cmd, shell=True, check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}\nError: {e.output}")
        raise

def get_venv_path():
    """Get the expected virtual environment path"""
    return "/opt/yt-dlp-mullvad/venv"

def validate_environment():
    """Validate that we're running in the correct environment"""
    venv_path = get_venv_path()
    
    # Check if we're in the expected venv
    current_venv = os.environ.get('VIRTUAL_ENV')
    if not current_venv or current_venv != venv_path:
        print(f"Error: This package must be installed and run from the virtual environment at {venv_path}")
        print(f"Current VIRTUAL_ENV: {current_venv}")
        print("\nTo fix this:")
        print(f"1. Create virtual environment: python3 -m venv {venv_path}")
        print(f"2. Activate it: source {venv_path}/bin/activate")
        print("3. Install package: pip install ytp-dl")
        sys.exit(1)
    
    # Check if yt-dlp is available
    ytdlp_path = f"{venv_path}/bin/yt-dlp"
    if not os.path.exists(ytdlp_path):
        print(f"Error: yt-dlp not found at {ytdlp_path}")
        print("This should have been installed automatically. Try reinstalling the package.")
        sys.exit(1)
    
    return venv_path

def check_mullvad():
    """Check if Mullvad CLI is available"""
    if not shutil.which("mullvad"):
        print("Error: Mullvad CLI not found.")
        print("Please install Mullvad VPN:")
        print("curl -fsSLo /tmp/mullvad.deb https://mullvad.net/download/app/deb/latest/")
        print("sudo apt install -y /tmp/mullvad.deb")
        sys.exit(1)

def wait_for_connection(timeout=10):
    """Poll Mullvad status until connected or timeout"""
    for _ in range(timeout):
        status = run_command("mullvad status")
        if "Connected" in status:
            print("Mullvad VPN connected.")
            return True
        time.sleep(1)
    print("Failed to confirm Mullvad VPN connection within timeout.")
    return False

def download_video(url, mullvad_account, resolution=None, extension=None):
    """
    Download a video using yt-dlp through Mullvad VPN
    
    Args:
        url (str): YouTube URL
        mullvad_account (str): Mullvad account number
        resolution (str, optional): Desired resolution (e.g., '1080')
        extension (str, optional): Desired file extension (e.g., 'mp4', 'mp3')
    
    Returns:
        str: Path to downloaded file or None if failed
    """
    venv_path = validate_environment()
    check_mullvad()
    
    print("Logging into Mullvad...")
    run_command(f"mullvad account login {mullvad_account}")
    
    print("Connecting to Mullvad VPN...")
    run_command("mullvad connect")

    if not wait_for_connection():
        sys.exit(1)

    print(f"Downloading: {url}")
    
    try:
        audio_extensions = ["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"]
        if extension and extension in audio_extensions:
            # Audio download
            ytdlp_cmd = (
                f"{venv_path}/bin/yt-dlp -x --audio-format {extension} "
                f"--embed-metadata "
                f"--output '/root/%(title)s.%(ext)s' "
                f"--user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36' {url}"
            )
        else:
            # Video download
            if resolution:
                format_filter = f"bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]"
            else:
                format_filter = "bestvideo+bestaudio"
            
            merge_extension = extension if extension else "mp4"
            ytdlp_cmd = (
                f"{venv_path}/bin/yt-dlp -f '{format_filter}' --merge-output-format {merge_extension} "
                f"--embed-thumbnail --embed-metadata "
                f"--output '/root/%(title)s.%(ext)s' "
                f"--user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36' {url}"
            )
        
        output = run_command(ytdlp_cmd)
        filename = None
        for line in output.splitlines():
            if line.startswith("[download]"):
                if "Destination:" in line:
                    filename = line.split("Destination: ")[1].strip()
                elif "has already been downloaded" in line:
                    start = line.find("] ") + 2
                    end = line.find(" has already been downloaded")
                    filename = line[start:end].strip()
                if filename and filename.startswith("'") and filename.endswith("'"):
                    filename = filename[1:-1]
                break
        
        if filename and os.path.exists(filename):
            print(f"DOWNLOADED_FILE:{filename}")
            return filename
        else:
            print("Download failed: File not found")
            return None
            
    except subprocess.CalledProcessError as e:
        print(f"yt-dlp failed with error: {e.output}")
        return None
    finally:
        print("Disconnecting VPN...")
        run_command("mullvad disconnect")
