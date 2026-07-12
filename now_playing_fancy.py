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
# Source frame produced by screensaver.py. This script is the ONLY writer
# of ART_PATH (nowplaying.png); during idle it publishes this file onto
# ART_PATH. screensaver.py never writes ART_PATH, which is what makes the
# resume clobber impossible.
SCREENSAVER_ART_PATH = os.path.expanduser("~/pico8-led/screensaver.png")
STATE_DIR = "/var/lib/pico8-led"
STATE_PATH = os.path.join(STATE_DIR, "track_state.json")
SCREENSAVER_STATE_PATH = os.path.join(STATE_DIR, "screensaver_state.json")
IMAGE_SIZE = 128
POLL_INTERVAL_SEC = 5      # how often we hit the Spotify API
PUBLISH_INTERVAL_SEC = 0.5 # how often we refresh the displayed image while
                            # idle (publishing screensaver frames / black) -
                            # decoupled from the API poll so slideshow frames
                            # appear promptly without polling Spotify faster

TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

os.makedirs(STATE_DIR, exist_ok=True)

_last_track_id = None
_is_playing = False          # last known playback state, shared with the
                              # idle-publish loop in __main__
_black_published = False      # have we already blacked the panel this idle spell?
_published_ss_mtime = None    # mtime of the screensaver.png we last published,
                              # so we only re-copy when the frame actually changes
_running = True


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
    """Overwrites the displayed art with solid black via the same
    atomic-write pattern as save_album_art(), so feh never catches a
    half-written file mid-reload. Used by publish_idle_frame() when idle
    with no active screensaver (e.g. during the debounce window, or when
    no cached art exists)."""
    img = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0))
    tmp_path = ART_PATH + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, ART_PATH)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def publish_screensaver_frame():
    """Copy screensaver.png onto the displayed nowplaying.png. Raw byte
    copy (the source is already a 128x128 PNG) via tmp-then-rename. This
    is the ONLY path that puts screensaver art on screen - screensaver.py
    itself never writes nowplaying.png."""
    tmp_path = ART_PATH + ".tmp"
    with open(SCREENSAVER_ART_PATH, "rb") as fsrc, open(tmp_path, "wb") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp_path, ART_PATH)


def publish_idle_frame():
    """Called repeatedly (every PUBLISH_INTERVAL_SEC) while playback is
    stopped. Decides what the panel shows during idle:

      - screensaver active + screensaver.png present -> publish that frame,
        but only when it actually changed (mtime differs from the last one
        we published) so we're not rewriting nowplaying.png every 0.5s.
      - otherwise (debounce window, screensaver off, or no cached art) ->
        black, written once per idle spell.

    Because only this process ever writes nowplaying.png, a resume can
    never be clobbered: the instant poll_once() sees playback, it writes
    the live cover and _is_playing flips, so this stops being called."""
    global _black_published, _published_ss_mtime

    ss = load_json(SCREENSAVER_STATE_PATH, {"active": False})
    if ss.get("active") and os.path.exists(SCREENSAVER_ART_PATH):
        try:
            mtime = os.path.getmtime(SCREENSAVER_ART_PATH)
        except OSError:
            return
        if mtime != _published_ss_mtime:
            publish_screensaver_frame()
            _published_ss_mtime = mtime
            _black_published = False
    else:
        if not _black_published:
            clear_art()
            _black_published = True
            _published_ss_mtime = None


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


def poll_once():
    global _last_track_id, _cfg, _is_playing
    global _black_published, _published_ss_mtime

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
        # Don't touch the image here - the idle-publish loop in __main__
        # owns nowplaying.png while stopped (screensaver frame or black).
        # Reset _last_track_id so that when playback resumes we always
        # re-fetch and write the live cover (the idle loop will have
        # overwritten nowplaying.png with screensaver art in the meantime).
        write_state(is_playing=False)
        _last_track_id = None
        _is_playing = False
        return

    # Transitioning into (or continuing) playback. If we were idle, reset
    # the publisher's bookkeeping so the next idle spell re-publishes
    # cleanly rather than trusting stale flags.
    if not _is_playing:
        _black_published = False
        _published_ss_mtime = None
    _is_playing = True

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


def _sigterm(_sig, _frame):
    global _running
    _running = False


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    _cfg = refresh_access_token(load_config())
    while _running:
        cycle_start = time.time()
        try:
            poll_once()
        except requests.RequestException as e:
            print(f"now_playing_fancy: request error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"now_playing_fancy: unexpected error: {e}", file=sys.stderr)

        # Between Spotify polls, keep the displayed image fresh while idle.
        # This is the ONLY place nowplaying.png is written during idle, and
        # it runs on a fast, cheap cadence (no API calls) so screensaver
        # frames appear within ~PUBLISH_INTERVAL_SEC of screensaver.py
        # producing them, without polling Spotify any faster.
        while _running and (time.time() - cycle_start) < POLL_INTERVAL_SEC:
            if not _is_playing:
                try:
                    publish_idle_frame()
                except Exception as e:
                    print(f"now_playing_fancy: idle publish error: {e}", file=sys.stderr)
            time.sleep(PUBLISH_INTERVAL_SEC)
