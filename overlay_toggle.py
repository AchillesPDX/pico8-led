#!/usr/bin/env python3
"""
overlay_toggle.py <clock|title|artist|progress>

Flips one overlay flag in /var/lib/pico8-led/overlay_state.json.
Meant to be called from the joystick-reading loop (xserver-screen.cc) on
D-pad press, or run by hand while testing:

  python3 overlay_toggle.py clock
"""

import json
import os
import sys
import tempfile

STATE_DIR = "/var/lib/pico8-led"
STATE_PATH = os.path.join(STATE_DIR, "overlay_state.json")

DEFAULTS = {"clock": True, "title": True, "artist": True, "progress": True}


def load():
    if not os.path.exists(STATE_PATH):
        return dict(DEFAULTS)
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        # backfill any missing keys rather than blowing up on a partial file
        for k, v in DEFAULTS.items():
            state.setdefault(k, v)
        return state
    except Exception:
        return dict(DEFAULTS)


def save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)
    os.chmod(STATE_PATH, 0o666)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in DEFAULTS:
        print(f"Usage: {sys.argv[0]} {{{'|'.join(DEFAULTS)}}}")
        sys.exit(1)

    key = sys.argv[1]
    state = load()
    state[key] = not state[key]
    save(state)
    print(f"{key}: {state[key]}")
