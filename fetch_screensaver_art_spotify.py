#!/usr/bin/env python3
"""
fetch_screensaver_art_spotify.py

Spotify-search variant of fetch_screensaver_art.py. Same offline,
run-occasionally model and the SAME output layout (slug-named PNGs in
screensaver_art/), so it's a drop-in alternative art source - the
iTunes version is left untouched as a fallback.

Why this exists: iTunes' legacy Search API failed to surface a handful
of well-known albums under any query strategy, forcing manual itunes_id
overrides. Spotify's search resolves those same names cleanly, and we
already have client_id/client_secret sitting in spotify.json - so this
needs no new credentials and no manual per-album IDs.

Token model - deliberately separate from the live poller's:
  This uses the CLIENT CREDENTIALS grant (client_id + client_secret
  only), which returns an app-level token good for public catalog reads
  like search. That's a DIFFERENT token from the user-scoped
  access_token/refresh_token that now_playing_fancy.py maintains for
  reading YOUR currently-playing track. We fetch our own client-creds
  token into memory here and never read or write the user token fields
  in spotify.json - so there's no repeat of the old now_playing.py
  refresh-token rotation collision, where two processes refreshing the
  same user token could invalidate each other. This script only ever
  READS client_id/client_secret from that file; it writes nothing back.

For each {"artist", "album", "year"} entry:
  1. Search Spotify for the album (q="artist album", type=album, limit=1).
  2. Take images[0].url (Spotify lists largest-first, typically 640x640).
  3. Center-crop to square and resize to 128x128 - same pipeline as
     now_playing_fancy.py's save_album_art(), so screensaver frames are
     pixel-compatible with the live nowplaying.png.
  4. Save to <output_dir>/<slug>.png. slug MUST match the iTunes
     script's and screensaver.py's slugify() exactly, so the two
     fetchers produce interchangeable caches and screensaver.py finds
     either one. If you change slugify() anywhere, change it everywhere.

Usage:
  ./fetch_screensaver_art_spotify.py [albums.json] [output_dir]

Defaults to curated_albums.json and ./screensaver_art if not given.
Safe to re-run - already-cached art is skipped unless --force is passed.
"""

import base64
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

IMAGE_SIZE = 128
CONFIG_PATH = os.path.expanduser("~/.config/pico8-led/spotify.json")
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
REQUEST_TIMEOUT = 10
# Spotify's rate limits are far more generous than iTunes' undocumented
# per-IP ceiling, but a small pause between calls is still polite and
# keeps us comfortably clear of any burst threshold on an 80+ item list.
RATE_LIMIT_SLEEP = 0.2
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0  # seconds - doubles each retry (2, 4, 8, 16)

DEFAULT_ALBUMS_PATH = "curated_albums.json"
DEFAULT_OUTPUT_DIR = "screensaver_art"


def slugify(artist, album):
    """Deterministic filename stem for a given artist/album pair. MUST
    stay identical to the same function in fetch_screensaver_art.py and
    screensaver.py - all three derive the cache filename this way, with
    no separate manifest, so any divergence silently breaks matching."""
    raw = f"{artist}_{album}".lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    return raw.strip("_")


def load_albums(path):
    with open(path) as f:
        return json.load(f)


def get_client_credentials_token():
    """App-level token for public catalog reads. Reads only client_id
    and client_secret from spotify.json - never the user token fields."""
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    auth = base64.b64encode(
        f"{cfg['client_id']}:{cfg['client_secret']}".encode()
    ).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _search_with_retry(token, artist, album):
    """Retries on 429 with exponential backoff, honoring Retry-After if
    Spotify sends it. After MAX_RETRIES the last response's
    raise_for_status() propagates as requests.HTTPError, which main()'s
    per-entry try/except handles the same as any other error - logged,
    counted failed, retried on the next run since it never gets cached."""
    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.get(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "q": f"{artist} {album}",
                "type": "album",
                "limit": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            print(
                f"fetch_screensaver_art_spotify: rate limited on {artist} - {album}, "
                f"waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()


def find_artwork_url(token, artist, album):
    data = _search_with_retry(token, artist, album)
    items = data.get("albums", {}).get("items", [])
    if not items:
        return None
    images = items[0].get("images", [])
    if not images:
        return None
    return images[0]["url"]  # largest first


def save_art(url, dest_path):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.HAMMING)

    tmp_path = dest_path + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, dest_path)


def main():
    albums_path = DEFAULT_ALBUMS_PATH
    output_dir = DEFAULT_OUTPUT_DIR
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(positional) >= 1:
        albums_path = positional[0]
    if len(positional) >= 2:
        output_dir = positional[1]
    force = "--force" in sys.argv

    albums = load_albums(albums_path)
    os.makedirs(output_dir, exist_ok=True)

    token = get_client_credentials_token()

    fetched = skipped = failed = 0

    for entry in albums:
        artist = entry.get("artist")
        album = entry.get("album")
        if not artist or not album:
            print(f"fetch_screensaver_art_spotify: skipping malformed entry: {entry}", file=sys.stderr)
            failed += 1
            continue

        slug = slugify(artist, album)
        dest_path = os.path.join(output_dir, f"{slug}.png")

        if os.path.exists(dest_path) and not force:
            skipped += 1
            continue

        try:
            art_url = find_artwork_url(token, artist, album)
            if not art_url:
                print(f"fetch_screensaver_art_spotify: no artwork found for {artist} - {album}", file=sys.stderr)
                failed += 1
                continue
            save_art(art_url, dest_path)
            fetched += 1
            print(f"fetch_screensaver_art_spotify: saved {slug}.png ({artist} - {album})")
        except requests.RequestException as e:
            print(f"fetch_screensaver_art_spotify: request error for {artist} - {album}: {e}", file=sys.stderr)
            failed += 1
        except Exception as e:
            print(f"fetch_screensaver_art_spotify: unexpected error for {artist} - {album}: {e}", file=sys.stderr)
            failed += 1

        time.sleep(RATE_LIMIT_SLEEP)

    print(f"fetch_screensaver_art_spotify: done - {fetched} fetched, {skipped} skipped (already cached), {failed} failed")


if __name__ == "__main__":
    main()
