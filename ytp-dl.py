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

    mullvad_account = "5529155492074454"
    api_url = "http://143.244.179.172:5000/api/download"

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