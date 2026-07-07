#!/usr/bin/env python3
"""
Polls the Spotify Web API for whatever track is currently playing on the
user's account (regardless of which device - phone, laptop, etc. - is
actually playing it), and writes the album art, center-cropped and resized
to 128x128, to a fixed path. A `feh` instance with --reload watches that
path and updates automatically.

Runs continuously in the background (see the accompanying systemd unit)
so the art is always current the moment you toggle over to Spotify mode -
it does not need to be started/stopped along with the display toggle.

One-time setup required: see the accompanying instructions to obtain a
Spotify API client id/secret and a refresh token for your account, saved
to ~/.config/pico8-led/spotify.json.
"""

import base64
import io
import json
import os
import sys
import time

import requests
from PIL import Image

CONFIG_PATH = os.path.expanduser("~/.config/pico8-led/spotify.json")
OUTPUT_PATH = os.path.expanduser("~/pico8-led/nowplaying.png")
POLL_INTERVAL_SEC = 5
IMAGE_SIZE = 128

TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def refresh_access_token(cfg):
    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth}"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    cfg["access_token"] = data["access_token"]
    # Spotify occasionally rotates the refresh token; keep the new one if given.
    if "refresh_token" in data:
        cfg["refresh_token"] = data["refresh_token"]
    save_config(cfg)
    return cfg


def get_currently_playing(cfg):
    resp = requests.get(
        NOW_PLAYING_URL,
        headers={"Authorization": f"Bearer {cfg['access_token']}"},
        timeout=10,
    )
    if resp.status_code == 401:
        return None  # token expired - caller should refresh and retry
    if resp.status_code == 204 or not resp.content:
        return {}  # nothing currently playing
    resp.raise_for_status()
    return resp.json()


def save_album_art(url):
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")

    # Center-crop to square, then resize down to the panel resolution.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)

    # Write to a temp file then atomically rename, so feh never catches a
    # half-written file mid-reload. format= is explicit because the .tmp
    # extension isn't something Pillow can infer a format from.
    tmp_path = OUTPUT_PATH + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, OUTPUT_PATH)


def save_placeholder():
    if os.path.exists(OUTPUT_PATH):
        return
    img = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0))
    img.save(OUTPUT_PATH, format="PNG")


def clear_art():
    """Overwrites the displayed art with solid black. Used whenever
    playback pauses or stops, so the panel doesn't keep showing stale
    art for a song that isn't playing anymore."""
    img = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0))
    tmp_path = OUTPUT_PATH + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, OUTPUT_PATH)


def main():
    save_placeholder()
    cfg = load_config()
    cfg = refresh_access_token(cfg)

    last_track_id = None
    art_cleared = True  # starts true since save_placeholder() already wrote black

    while True:
        try:
            data = get_currently_playing(cfg)

            if data is None:  # token expired
                cfg = refresh_access_token(cfg)
                time.sleep(1)
                continue

            item = data.get("item") if data else None
            is_playing = bool(data) and bool(item) and data.get("is_playing", False)

            if is_playing:
                track_id = item.get("id")
                if track_id != last_track_id:
                    images = item.get("album", {}).get("images", [])
                    if images:
                        # Images are listed largest-first. Grab the largest
                        # one and let Pillow do a high-quality downscale
                        # below - using Spotify's smallest thumbnail here
                        # instead would mean *upscaling* an already
                        # low-quality, heavily-compressed 64x64 JPEG,
                        # which is what caused the blocky artifacts.
                        art_url = images[0]["url"]
                        save_album_art(art_url)
                        last_track_id = track_id
                        art_cleared = False
            else:
                # Nothing playing, or playback is paused - don't keep
                # showing stale art. Only write once per transition into
                # this state, not on every poll.
                if not art_cleared:
                    clear_art()
                    last_track_id = None
                    art_cleared = True

        except requests.RequestException as e:
            print(f"now_playing: request error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"now_playing: unexpected error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
