#!/usr/bin/env python3
"""
screensaver.py

Idle-time album-art slideshow. When Spotify has been not-playing for
longer than SCREENSAVER_DEBOUNCE_SEC, this daemon takes over the same
nowplaying.png that now_playing_fancy.py normally drives, and cycles
through the curated art cache one album every SCREENSAVER_INTERVAL_SEC.
When playback resumes, it immediately stands down and hands the file
back.

WHY THIS IS SAFE TO SHARE nowplaying.png WITH now_playing_fancy.py:
  now_playing_fancy.py only writes nowplaying.png on a *transition* -
  it saves album art once when a new track starts, and calls clear_art()
  ONCE when playback stops (gated by its _art_cleared flag). While idle,
  it leaves the file completely alone. That gives screensaver.py a clear
  window to own the file. The single point of contention is the instant
  playback resumes: to avoid clobbering real album art with a slideshow
  frame, screensaver.py re-reads track_state.json immediately before
  EVERY write and bails the moment is_playing is true. Worst case is one
  already-written slideshow frame lingering until feh's next ~0.5s
  reload, after which now_playing_fancy's fresh art shows - a harmless
  sub-second overlap, not a persistent fight.

READS:
  /var/lib/pico8-led/track_state.json  (written by now_playing_fancy.py)
  <script_dir>/curated_albums.json     (same list the fetchers use)
  <script_dir>/screensaver_art/*.png   (built by either fetch script)

WRITES:
  ~/pico8-led/nowplaying.png              (only while active + idle)
  /var/lib/pico8-led/screensaver_state.json (so overlay_display.py can
                                             show a screensaver-specific
                                             overlay: clock + album text,
                                             no progress bar)

Deliberately a sub-state of the existing Spotify window, NOT a new
top-level display mode - it never touches PICO-8 or the mode switch, so
it adds no pressure to the mode-switching modularization work.
"""

import json
import os
import random
import re
import sys
import time

# ---------------------------------------------------------------------------
# Tunables. SCREENSAVER_DEBOUNCE_SEC is intentionally 0 for now per initial
# testing - screensaver kicks in immediately on pause. Raise it (e.g. 90)
# once behavior is confirmed so a normal between-song pause doesn't trigger
# a slideshow flash.
# ---------------------------------------------------------------------------
SCREENSAVER_DEBOUNCE_SEC = 0      # idle time before slideshow starts
SCREENSAVER_INTERVAL_SEC = 10     # seconds each album is shown
POLL_INTERVAL_SEC = 1.0           # how often we check idle state / advance

IMAGE_SIZE = 128

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALBUMS_PATH = os.path.join(SCRIPT_DIR, "curated_albums.json")
ART_DIR = os.path.join(SCRIPT_DIR, "screensaver_art")
ART_PATH = os.path.expanduser("~/pico8-led/nowplaying.png")

STATE_DIR = "/var/lib/pico8-led"
TRACK_STATE_PATH = os.path.join(STATE_DIR, "track_state.json")
SCREENSAVER_STATE_PATH = os.path.join(STATE_DIR, "screensaver_state.json")

os.makedirs(STATE_DIR, exist_ok=True)

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False


import signal
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


def slugify(artist, album):
    """MUST stay identical to slugify() in both fetch scripts - that's
    how we map a curated_albums.json entry to its cached PNG with no
    separate manifest. Any divergence silently breaks the mapping."""
    raw = f"{artist}_{album}".lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    return raw.strip("_")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path, obj):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(obj, f)
    os.replace(tmp_path, path)


def atomic_copy_png(src, dst):
    """Copy a cached PNG onto nowplaying.png via a tmp-then-rename, so
    feh never catches a partial file mid-reload. Copies raw bytes rather
    than re-encoding through PIL since the cached art is already exactly
    128x128 PNG - no work to do.

    IMPORTANT: uses a screensaver-specific tmp suffix, NOT the plain
    "<dst>.tmp" that now_playing_fancy.py's clear_art()/save_album_art()
    use. Those two writers and this one all target the same
    nowplaying.png, and at the pause transition screensaver's first
    write can fire within a fraction of a second of clear_art(). If both
    used "<dst>.tmp" they'd interleave writes to the SAME tmp file and
    produce a corrupt (black) frame - which is exactly the "first album
    is black, rest are fine" symptom, since now_playing_fancy only
    writes on that one transition and never again while idle. A distinct
    tmp path makes the two atomic writes fully independent regardless of
    timing, so correctness no longer depends on the debounce value."""
    tmp_path = dst + ".ss.tmp"
    with open(src, "rb") as fsrc, open(tmp_path, "wb") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp_path, dst)


def build_playlist():
    """Pair each curated entry with its cached PNG path, dropping any
    that were never successfully fetched (a straggler the fetch script
    couldn't resolve, or a list entry added since the last fetch run).
    Returns a shuffled list of (entry, art_path) so the rotation order
    differs each time the daemon starts / rebuilds."""
    albums = load_json(ALBUMS_PATH, [])
    playlist = []
    missing = 0
    for entry in albums:
        artist = entry.get("artist")
        album = entry.get("album")
        if not artist or not album:
            continue
        art_path = os.path.join(ART_DIR, f"{slugify(artist, album)}.png")
        if os.path.exists(art_path):
            playlist.append((entry, art_path))
        else:
            missing += 1
    if missing:
        print(f"screensaver: {missing} curated album(s) have no cached art, skipped",
              file=sys.stderr)
    random.shuffle(playlist)
    return playlist


def is_spotify_playing():
    track = load_json(TRACK_STATE_PATH, {"is_playing": False})
    return bool(track.get("is_playing"))


def write_screensaver_state(active, entry=None):
    """Overlay reads this to decide whether to draw the screensaver
    overlay (clock + album text, no progress bar). When inactive we
    write active=False rather than deleting the file so the overlay has
    a definite 'not screensaving' signal and never reads a stale True."""
    obj = {"active": active, "updated_at": time.time()}
    if active and entry:
        obj["artist"] = entry.get("artist")
        obj["album"] = entry.get("album")
        obj["year"] = entry.get("year")
    atomic_write_json(SCREENSAVER_STATE_PATH, obj)


def main():
    playlist = build_playlist()
    if not playlist:
        print("screensaver: no cached art available, nothing to show - "
              "run a fetch script first. Idling.", file=sys.stderr)

    idx = 0
    active = False           # are we currently running the slideshow?
    not_playing_since = None # timestamp playback last stopped, for debounce
    last_advance = 0.0       # when we last swapped the displayed album

    # Ensure the overlay starts from a known 'not screensaving' state.
    write_screensaver_state(False)

    while _running:
        now = time.time()
        playing = is_spotify_playing()

        if playing:
            # Resume path. Stand down immediately - do NOT write art here;
            # now_playing_fancy.py owns nowplaying.png again the moment a
            # track is playing, and it'll write the real cover on its next
            # poll (or already has).
            if active:
                active = False
                write_screensaver_state(False)
            not_playing_since = None
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # Not playing. Start the debounce clock on the first idle poll.
        if not_playing_since is None:
            not_playing_since = now

        idle_long_enough = (now - not_playing_since) >= SCREENSAVER_DEBOUNCE_SEC

        if idle_long_enough and playlist:
            first_frame = not active
            time_to_advance = (now - last_advance) >= SCREENSAVER_INTERVAL_SEC

            if first_frame or time_to_advance:
                # Re-check playback RIGHT before writing to close the race
                # with a resume that happened during this poll's own work.
                if is_spotify_playing():
                    if active:
                        active = False
                        write_screensaver_state(False)
                    not_playing_since = None
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                entry, art_path = playlist[idx]
                try:
                    atomic_copy_png(art_path, ART_PATH)
                    write_screensaver_state(True, entry)
                    active = True
                    last_advance = now
                    idx += 1
                    if idx >= len(playlist):
                        # Loop finished - reshuffle so the next pass through
                        # isn't in the same order.
                        random.shuffle(playlist)
                        idx = 0
                except Exception as e:
                    print(f"screensaver: failed to display {art_path}: {e}",
                          file=sys.stderr)
                    # Skip the bad entry so one unreadable file doesn't wedge
                    # the rotation.
                    idx = (idx + 1) % len(playlist)

        time.sleep(POLL_INTERVAL_SEC)

    # On shutdown, clear the screensaver flag so the overlay doesn't keep
    # drawing album text over whatever comes next.
    write_screensaver_state(False)


if __name__ == "__main__":
    main()
