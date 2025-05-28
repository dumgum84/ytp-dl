#!/usr/bin/env python3
import argparse
from .downloader import download_video

def main():
    parser = argparse.ArgumentParser(description="YouTube video downloader with Mullvad VPN")
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument("mullvad_account", help="Mullvad account number")
    parser.add_argument("--resolution", help="Desired resolution (e.g., 1080)", default=None)
    parser.add_argument("--extension", help="Desired file extension (e.g., mp4, mp3)", default=None)
    
    args = parser.parse_args()
    
    result = download_video(
        url=args.url,
        mullvad_account=args.mullvad_account,
        resolution=args.resolution,
        extension=args.extension
    )
    
    if result:
        print(f"Successfully downloaded: {result}")
    else:
        print("Download failed")
        exit(1)

if __name__ == "__main__":
    main()