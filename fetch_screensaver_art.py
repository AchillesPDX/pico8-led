#!/usr/bin/env python3
"""
fetch_screensaver_art.py

One-off/occasional offline fetch step for the screensaver mode - this is
NOT run at LED runtime. Reads a curated album list from an external JSON
file (default: curated_albums.json, kept separate rather than baked into
this script so the list can be hand-edited without touching code) and
pulls cover art for each entry from the iTunes Search API - no auth
required, generous rate limits, same no-auth-required principle as
LRCLIB for the lyrics idea.

For each {"artist", "album", "year"} entry:
  1. Query the iTunes Search API for artist+album - UNLESS the entry
     also has an "itunes_id" field, in which case the Lookup API is
     used instead, fetching that exact collection ID with no fuzzy
     matching involved. A few well-known albums (confirmed via manual
     testing) just don't surface reliably through Search's relevance
     ranking no matter how the query is phrased - itunes_id is the
     manual override for those stragglers. Find the ID in an album's
     music.apple.com URL, e.g. .../album/some-album/1760844242.
  2. Take the returned artworkUrl100 and swap "100x100bb" for
     "600x600bb" to get full-size art instead of upscaling a blurry
     100px thumbnail - same "grab the largest image, don't upscale a
     thumbnail" lesson as save_album_art() in now_playing_fancy.py.
  3. Center-crop to square and resize to 128x128, same pipeline as the
     live Spotify art, so screensaver frames are pixel-compatible with
     nowplaying.png.
  4. Save to <output_dir>/<slug>.png, where <slug> is a deterministic
     slugification of "artist_album". screensaver.py recomputes this
     same slug from curated_albums.json at runtime rather than needing
     a separate manifest file - keeps a single source of truth for
     which albums exist. If you change slugify() here, mirror the
     change in screensaver.py or cached art will stop matching.

Usage:
  ./fetch_screensaver_art.py [albums.json] [output_dir]

Defaults to curated_albums.json and ./screensaver_art if not given.
Safe to re-run - already-cached art is skipped unless --force is passed.
"""

import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

IMAGE_SIZE = 128
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
REQUEST_TIMEOUT = 10
# iTunes' search endpoint has an undocumented per-IP rate limit. 0.5s
# between calls (120/min) started drawing 429s after ~45-50 requests in
# practice. 3s keeps us well under the ~20/min ballpark other API
# consumers have reported as the informal ceiling.
RATE_LIMIT_SLEEP = 3.0
MAX_RETRIES = 4
RETRY_BASE_DELAY = 5.0  # seconds - doubles each retry (5, 10, 20, 40)

DEFAULT_ALBUMS_PATH = "curated_albums.json"
DEFAULT_OUTPUT_DIR = "screensaver_art"


def slugify(artist, album):
    """Deterministic filename stem for a given artist/album pair. Keep
    in sync with the identical function in screensaver.py - that script
    recomputes this at runtime against curated_albums.json to find the
    matching cached art file, rather than reading a separate manifest."""
    raw = f"{artist}_{album}".lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    return raw.strip("_")


def load_albums(path):
    with open(path) as f:
        return json.load(f)


def _get_json_with_retry(url, params, label):
    """Shared GET-with-429-backoff logic for both the Search and Lookup
    endpoints. Retries up to MAX_RETRIES times with exponential backoff
    (5, 10, 20, 40s), honoring a Retry-After header if iTunes sends
    one. After the last retry, raise_for_status() propagates as a
    requests.HTTPError, which main()'s per-entry try/except handles
    the same as any other request error - logged and counted as
    failed, picked back up automatically on the next run since a
    failed entry never gets cached."""
    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            print(
                f"fetch_screensaver_art: rate limited on {label}, "
                f"waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(wait)
            delay *= 2
            continue

        resp.raise_for_status()
        return resp.json()


def _artwork_from_results(results):
    if not results:
        return None
    art_url = results[0].get("artworkUrl100")
    if not art_url:
        return None
    # Swap the thumbnail resolution token for a much larger one instead
    # of upscaling a blurry 100x100 - same reasoning as
    # now_playing_fancy.py's save_album_art() grabbing the largest
    # Spotify image instead of the smallest.
    return art_url.replace("100x100bb", "600x600bb")


def find_artwork_url(artist, album):
    """Search-by-term lookup. media="music" is required, not optional:
    entity values are scoped per media type in Apple's API, and "album"
    isn't a valid entity under the default media="all" - that bug was
    silently making the entity filter meaningless.

    Even with that fixed, Search's relevance ranking can still fail to
    surface the right album at all for some queries - confirmed via
    manual testing that a handful of well-known albums (Fetch the Bolt
    Cutters, Fresh Fruit for Rotting Vegetables, It Takes a Nation of
    Millions to Hold Us Back) don't come back under any query strategy
    tried, including attribute=albumTerm. For those, use the itunes_id
    override path (see lookup_artwork_url) instead of fighting Search
    further."""
    data = _get_json_with_retry(
        ITUNES_SEARCH_URL,
        {
            "term": f"{artist} {album}",
            "media": "music",
            "entity": "album",
            "limit": 1,
        },
        label=f"{artist} - {album}",
    )
    return _artwork_from_results(data.get("results", []))


def lookup_artwork_url(itunes_id):
    """Fetches by exact collection ID via the Lookup API instead of
    Search - no relevance ranking involved, so it works for the
    handful of albums Search's index just doesn't surface well. Get
    the ID from an album's music.apple.com URL, e.g.
    https://music.apple.com/us/album/some-album/1760844242 -> 1760844242."""
    data = _get_json_with_retry(
        ITUNES_LOOKUP_URL,
        {"id": itunes_id},
        label=f"itunes_id={itunes_id}",
    )
    return _artwork_from_results(data.get("results", []))


def save_art(url, dest_path):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)

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

    fetched = skipped = failed = 0

    for entry in albums:
        artist = entry.get("artist")
        album = entry.get("album")
        itunes_id = entry.get("itunes_id")
        if not artist or not album:
            print(f"fetch_screensaver_art: skipping malformed entry: {entry}", file=sys.stderr)
            failed += 1
            continue

        slug = slugify(artist, album)
        dest_path = os.path.join(output_dir, f"{slug}.png")

        if os.path.exists(dest_path) and not force:
            skipped += 1
            continue

        try:
            if itunes_id:
                art_url = lookup_artwork_url(itunes_id)
            else:
                art_url = find_artwork_url(artist, album)
            if not art_url:
                print(f"fetch_screensaver_art: no artwork found for {artist} - {album}", file=sys.stderr)
                failed += 1
                continue
            save_art(art_url, dest_path)
            fetched += 1
            print(f"fetch_screensaver_art: saved {slug}.png ({artist} - {album})")
        except requests.RequestException as e:
            print(f"fetch_screensaver_art: request error for {artist} - {album}: {e}", file=sys.stderr)
            failed += 1
        except Exception as e:
            print(f"fetch_screensaver_art: unexpected error for {artist} - {album}: {e}", file=sys.stderr)
            failed += 1

        time.sleep(RATE_LIMIT_SLEEP)

    print(f"fetch_screensaver_art: done - {fetched} fetched, {skipped} skipped (already cached), {failed} failed")


if __name__ == "__main__":
    main()
