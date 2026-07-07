#!/usr/bin/env python3
"""
now_playing_fancy.py

Sibling to now_playing.py - does NOT touch nowplaying.png or the existing
now-playing.service. Writes to its own files so both can run side by side:

  ~/pico8-led/nowplaying_fancy.png     - album art ONLY, no text baked in
  /var/lib/pico8-led/track_state.json  - title/artist/progress/is_playing

overlay_display.py reads track_state.json and draws the live overlay
(clock, title, artist, progress) in a separate ARGB window on top.

IMPORTANT: this deliberately does NOT refresh its own access token. It
reads access_token out of the same ~/.config/pico8-led/spotify.json that
now_playing.py already maintains and refreshes continuously as a running
service. If two independent processes each refreshed against the same
refresh_token, Spotify's token rotation could have one invalidate the
other's stored token. Instead, on a 401 this just re-reads the config
file (now_playing.py's own service will have refreshed it within a few
seconds) and retries next poll cycle - no writes to spotify.json from
this script, ever.

This means now_playing.py's service must actually be running for this to
work - which it will be, since we're not touching it.
"""

import io
import json
import os
import sys
import time

import requests
from PIL import Image

CONFIG_PATH = os.path.expanduser("~/.config/pico8-led/spotify.json")
ART_PATH = os.path.expanduser("~/pico8-led/nowplaying_fancy.png")
STATE_DIR = "/var/lib/pico8-led"
STATE_PATH = os.path.join(STATE_DIR, "track_state.json")
IMAGE_SIZE = 128
POLL_INTERVAL_SEC = 5

NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

os.makedirs(STATE_DIR, exist_ok=True)

_last_track_id = None


def load_config():
    """Read-only - never write back to this file from here."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_currently_playing(access_token):
    resp = requests.get(
        NOW_PLAYING_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if resp.status_code == 401:
        return None  # expired - caller re-reads config and retries next cycle
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


def write_state(is_playing, title=None, artist=None, progress_ms=0, duration_ms=0):
    atomic_write_json(STATE_PATH, {
        "is_playing": is_playing,
        "title": title,
        "artist": artist,
        "progress_ms": progress_ms,
        "duration_ms": duration_ms,
        "polled_at": time.time(),
    })


def poll_once():
    global _last_track_id

    cfg = load_config()
    data = get_currently_playing(cfg["access_token"])

    if data is None:
        # token expired - now_playing.py's own service should refresh it
        # within a few seconds; just skip this cycle rather than racing
        # a refresh ourselves
        print("now_playing_fancy: token expired, waiting on now-playing.service to refresh", file=sys.stderr)
        return

    item = data.get("item") if data else None
    is_playing = bool(data) and bool(item) and data.get("is_playing", False)

    if not is_playing:
        write_state(is_playing=False)
        _last_track_id = None
        return

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
    while True:
        try:
            poll_once()
        except requests.RequestException as e:
            print(f"now_playing_fancy: request error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"now_playing_fancy: unexpected error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SEC)
