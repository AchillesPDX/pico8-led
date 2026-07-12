#!/usr/bin/env python3
"""
now_playing_fancy.py

Replaces now_playing.py as the running poller. Writes to the SAME
canonical paths the old one did:

  ~/pico8-led/nowplaying.png   - album art, center-cropped/resized to 128x128
  ~/.config/pico8-led/spotify.json - refreshed in place, same as before

Plus one new file the overlay reads from:

  /var/lib/pico8-led/track_state.json - title/artist/progress/is_playing

overlay_display.py reads track_state.json and draws the live overlay
(clock, title, artist, progress) in a separate ARGB window on top,
composited by picom.
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
ART_PATH = os.path.expanduser("~/pico8-led/nowplaying.png")
STATE_DIR = "/var/lib/pico8-led"
STATE_PATH = os.path.join(STATE_DIR, "track_state.json")
IMAGE_SIZE = 128
POLL_INTERVAL_SEC = 5

TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

os.makedirs(STATE_DIR, exist_ok=True)

_last_track_id = None


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
    if "refresh_token" in data:
        cfg["refresh_token"] = data["refresh_token"]
    save_config(cfg)
    return cfg


def get_currently_playing(access_token):
    resp = requests.get(
        NOW_PLAYING_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if resp.status_code == 401:
        return None  # expired - caller refreshes and retries
    if resp.status_code == 204 or not resp.content:
        return {}  # nothing currently playing
    resp.raise_for_status()
    return resp.json()


def atomic_write_bytes(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def atomic_write_json(path, obj):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(obj, f)
    os.replace(tmp_path, path)


def save_album_art(url):
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)

    tmp_path = ART_PATH + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, ART_PATH)


def clear_art():
    """Overwrites the displayed art with solid black. Same atomic-write
    pattern as save_album_art() so feh never catches a half-written
    file mid-reload. Called once on the transition into a stopped/
    paused state, not every poll - same one-write-per-transition idea
    as _art_cleared below, to avoid needlessly rewriting a file that
    hasn't changed every 5s poll cycle."""
    img = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0))
    tmp_path = ART_PATH + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, ART_PATH)


def write_state(is_playing, title=None, artist=None, progress_ms=0, duration_ms=0):
    atomic_write_json(STATE_PATH, {
        "is_playing": is_playing,
        "title": title,
        "artist": artist,
        "progress_ms": progress_ms,
        "duration_ms": duration_ms,
        "polled_at": time.time(),
    })


_cfg = None  # cached across polls, refreshed in place on 401
# Starts False, not True: unlike now_playing.py's save_placeholder() at
# startup, this script never writes a placeholder, so on a fresh start
# nowplaying.png could still be holding stale art from whatever was
# last playing before a restart. Starting False means the first
# not-playing poll clears it rather than assuming it's already black.
_art_cleared = False


def poll_once():
    global _last_track_id, _cfg, _art_cleared

    if _cfg is None:
        _cfg = load_config()

    data = get_currently_playing(_cfg["access_token"])

    if data is None:
        _cfg = refresh_access_token(_cfg)
        data = get_currently_playing(_cfg["access_token"])
        if data is None:
            print("now_playing_fancy: still unauthorized after refresh", file=sys.stderr)
            return

    item = data.get("item") if data else None
    is_playing = bool(data) and bool(item) and data.get("is_playing", False)

    if not is_playing:
        if not _art_cleared:
            clear_art()
            _art_cleared = True
        write_state(is_playing=False)
        _last_track_id = None
        return

    _art_cleared = False

    track_id = item.get("id")
    if track_id != _last_track_id:
        images = item.get("album", {}).get("images", [])
        if images:
            save_album_art(images[0]["url"])  # largest first, same lesson as now_playing.py
        _last_track_id = track_id

    write_state(
        is_playing=True,
        title=item.get("name"),
        artist=", ".join(a["name"] for a in item.get("artists", [])),
        progress_ms=data.get("progress_ms", 0),
        duration_ms=item.get("duration_ms", 0),
    )


if __name__ == "__main__":
    _cfg = refresh_access_token(load_config())
    while True:
        try:
            poll_once()
        except requests.RequestException as e:
            print(f"now_playing_fancy: request error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"now_playing_fancy: unexpected error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SEC)
