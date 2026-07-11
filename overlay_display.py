#!/usr/bin/env python3
"""
overlay_display.py

Creates a borderless, override-redirect, 32-bit ARGB X11 window positioned
exactly on top of the feh album-art window, and redraws it once a second
with whatever overlay elements are toggled on. Relies on picom running on
the same DISPLAY to actually composite the alpha channel - without a
compositor this window will just show as opaque garbage or fail to show
transparency at all.

Geometry MUST match your feh window (128x128+128+128 per your setup).

Reads:
  /var/lib/pico8-led/track_state.json   (written by now_playing_fancy.py)
  /var/lib/pico8-led/overlay_state.json (written by overlay_toggle.py)

This does not manage its own visibility/mode-switching yet - for now, run
it manually to test, kill it with Ctrl-C or SIGTERM. Wiring it into
toggle_display.sh (start on spotify mode, kill on game mode) is a
follow-up step once this renders correctly on your hardware.
"""

import json
import os
import time
import signal

from Xlib import X, display
from PIL import Image, ImageDraw, ImageFont

DISPLAY_NAME = ":3.0"
GEOMETRY = (128, 128, 128, 128)  # width, height, x, y - match feh's geometry
PICO_WINDOW_TITLE = "PICO-8"

# Resolved relative to this script's own location, not the current
# working directory - systemd's default WorkingDirectory is "/" unless
# explicitly set, so a bare "./fonts/..." would silently fail and fall
# back to the default font with no error.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(SCRIPT_DIR, "fonts")

STATE_DIR = "/var/lib/pico8-led"
TRACK_STATE_PATH = os.path.join(STATE_DIR, "track_state.json")
OVERLAY_STATE_PATH = os.path.join(STATE_DIR, "overlay_state.json")

REDRAW_INTERVAL = 0.05     # ~14fps - fast enough for smooth scrolling text
MODE_CHECK_INTERVAL = 1.0  # throttle the (now native, cheap) mode check anyway -
                            # mode doesn't change fast enough to need per-frame checks

SCROLL_SPEED = 20   # pixels/second
SCROLL_GAP = 16      # blank pixels between the end of one loop and the start of the next

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def find_argb_visual(screen):
    """Find a 32-bit depth TrueColor visual for real per-pixel alpha."""
    for depth_info in screen['allowed_depths']:
        if depth_info.depth == 32:
            for visual in depth_info.visuals:
                if visual.visual_class == X.TrueColor:
                    return visual.visual_id, 32
    raise RuntimeError("No 32-bit TrueColor visual available on this X server")


def make_window(disp):
    screen = disp.screen()
    visual_id, depth = find_argb_visual(screen)

    colormap = screen.root.create_colormap(visual_id, X.AllocNone)

    width, height, x, y = GEOMETRY

    window = screen.root.create_window(
        x, y, width, height, 0,
        depth,
        X.InputOutput,
        visual_id,
        background_pixel=0,
        border_pixel=0,
        colormap=colormap,
        override_redirect=True,
        event_mask=X.ExposureMask,
    )
    window.map()
    disp.sync()
    return window, depth


def draw_scrolling_text(draw, x0, y, text, font, fill, max_width,
                          stroke_width=0, stroke_fill=None):
    """Draws text left-aligned at x0 if it fits in max_width. If it's
    wider, scrolls it continuously instead of truncating - PIL clips
    anything drawn outside the image's own bounds automatically, so no
    manual clipping/cropping is needed even with negative x values."""
    text_width = font.getlength(text)
    available = max_width - (x0 * 2)  # symmetric margin on both sides

    if text_width <= available:
        draw.text((x0, y), text, font=font, fill=fill,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)
        return

    period = text_width + SCROLL_GAP
    offset = (time.time() * SCROLL_SPEED) % period
    x = x0 - offset
    draw.text((x, y), text, font=font, fill=fill,
               stroke_width=stroke_width, stroke_fill=stroke_fill)
    draw.text((x + period, y), text, font=font, fill=fill,
               stroke_width=stroke_width, stroke_fill=stroke_fill)


def render_frame(width, height):
    """Build the RGBA frame to blit - translucent bars behind text."""
    track = load_json(TRACK_STATE_PATH, {
        "is_playing": False, "title": None, "artist": None,
        "progress_ms": 0, "duration_ms": 0, "polled_at": 0,
    })
    overlay = load_json(OVERLAY_STATE_PATH, {
        "clock": True, "title": True, "artist": True, "progress": True,
    })

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    try:
        font_small = ImageFont.truetype(
            os.path.join(FONTS_DIR, "04b_25.ttf"), 12)
        font_tiny = ImageFont.truetype(
            os.path.join(FONTS_DIR, "04b_03.ttf"), 8)
    except Exception as e:
        print(f"overlay_display: could not load custom font, falling back to default: {e}")
        font_small = ImageFont.load_default()
        font_tiny = font_small

    BAR_BG = (0, 0, 0, 140)  # translucent black backing, alpha 140/255
    STROKECOLOR = (0, 0, 0, 140) # translucent black stroke, alpha 140/255
    TEXTCOLOR = (255, 255, 255, 255) # Text color

    if overlay.get("clock"):
        text = time.strftime("%I:%M").lstrip("0").lower()
        draw.text((width - 2, 0), text, font=font_small, fill=(TEXTCOLOR),
                   stroke_width=1, stroke_fill=(STROKECOLOR), anchor="ra")

    if track.get("is_playing"):
        # extrapolate progress locally so it ticks smoothly between polls
        elapsed_since_poll = time.time() - track.get("polled_at", time.time())
        progress_ms = track.get("progress_ms", 0) + elapsed_since_poll * 1000
        duration_ms = max(track.get("duration_ms", 0), 1)
        progress_ms = min(progress_ms, duration_ms)
        frac = progress_ms / duration_ms

        progress_enabled = overlay.get("progress")
        PROGRESS_BAR_HEIGHT = 2
        bottom_reserved = PROGRESS_BAR_HEIGHT if progress_enabled else 0

        if overlay.get("title") or overlay.get("artist"):
            lines = []
            if overlay.get("title") and track.get("title"):
                lines.append(track["title"])
            if overlay.get("artist") and track.get("artist"):
                lines.append(track["artist"])

            bar_h = 12 * len(lines) + 2
            if len(lines) > 1:
                bar_h -= 5
            bar_bottom = height - bottom_reserved
            """draw.rectangle([0, bar_bottom - bar_h, width, bar_bottom], fill=BAR_BG)"""
            ty = bar_bottom - bar_h + 1
            for i, line in enumerate(lines):
                f = font_small if i == 0 else font_tiny
                draw_scrolling_text(draw, 2, ty, line, f, TEXTCOLOR, width,
                                     stroke_width=1, stroke_fill=STROKECOLOR)
                ty += 12

        if progress_enabled:
            bar_y = height - PROGRESS_BAR_HEIGHT
            draw.rectangle([0, bar_y, width, height], fill=(255, 255, 255, 60))
            draw.rectangle([0, bar_y, int(width * frac), height],
                            fill=(30, 215, 96, 220))  # spotify green-ish

    return img


def image_to_ximage_data(img):
    """Convert RGBA PIL image to BGRA byte order X typically expects (little-endian)."""
    return img.tobytes("raw", "BGRA")


def blit(window, gc, img, width, height, depth):
    data = image_to_ximage_data(img)
    window.put_image(gc, 0, 0, width, height, X.ZPixmap, depth, 0, data)


def pico8_is_visible(disp):
    """Queries the X window tree directly instead of shelling out to
    xdotool/xwininfo. Same underlying signal as toggle_display.sh's
    detect_mode() (is PICO-8's window mapped/viewable), but native -
    spawning two subprocesses every MODE_CHECK_INTERVAL was blocking the
    render loop long enough to cause a visible once-a-second stutter in
    the scrolling text."""
    try:
        root = disp.screen().root
        for win in root.query_tree().children:
            try:
                name = win.get_wm_name()
            except Exception:
                continue
            if name and PICO_WINDOW_TITLE in name:
                return win.get_attributes().map_state == X.IsViewable
        return False
    except Exception:
        # if we can't tell, err on the side of NOT covering gameplay
        return True


def main():
    disp = display.Display(DISPLAY_NAME)
    window, depth = make_window(disp)
    gc = window.create_gc()

    width, height = GEOMETRY[0], GEOMETRY[1]
    currently_mapped = True
    game_mode = pico8_is_visible(disp)
    last_mode_check = time.time()

    while _running:
        now = time.time()
        if now - last_mode_check >= MODE_CHECK_INTERVAL:
            game_mode = pico8_is_visible(disp)
            last_mode_check = now

        if game_mode:
            if currently_mapped:
                window.unmap()
                disp.sync()
                currently_mapped = False
        else:
            if not currently_mapped:
                window.map()
                disp.sync()
                currently_mapped = True
            frame = render_frame(width, height)
            blit(window, gc, frame, width, height, depth)
            disp.sync()
        time.sleep(REDRAW_INTERVAL)

    window.unmap()
    disp.sync()


if __name__ == "__main__":
    main()
