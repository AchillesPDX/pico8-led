#!/bin/sh
export DISPLAY=:3.0

Xvfb $DISPLAY -screen 0 800x600x24 &

# Wait for Xvfb to actually be accepting connections before launching
# anything that needs the display. A fixed sleep here was fine on a
# warm restart but not reliable enough right after a cold boot, when
# there's more contention for CPU/disk.
for i in $(seq 1 50); do
  xdpyinfo >/dev/null 2>&1 && break
  sleep 0.2
done

killall pico8_64
./pico-8/pico8_64 -windowed 1 -window_x 128 -window_y 128 -width 128 -height 128 -frameless 1 -splore &

# feh refuses to start if the image file doesn't exist yet. now_playing.py
# writes a placeholder immediately on its own startup, but there's no
# guarantee it's finished doing that by the time we get here - wait for
# the file rather than assuming.
for i in $(seq 1 50); do
  [ -f ./nowplaying.png ] && break
  sleep 0.2
done
feh --title nowplaying --geometry 128x128+128+128 --borderless --reload 3 ./nowplaying.png > /tmp/feh.log 2>&1 &

# Wait for both windows to actually exist before deciding which one
# should start on top. This is the fix for the specific bug where a
# cold boot would sometimes land in Spotify mode: toggle_display.sh's
# mode detection depends on being able to find PICO-8's window, and a
# fixed sleep wasn't consistently long enough for it to exist yet.
for i in $(seq 1 50); do
  pico_win=$(xdotool search --name "PICO-8" 2>/dev/null | head -n1)
  spotify_win=$(xdotool search --name "nowplaying" 2>/dev/null | head -n1)
  [ -n "$pico_win" ] && [ -n "$spotify_win" ] && break
  sleep 0.2
done

./toggle_display.sh spotify   # default to album art on boot
./run_led.sh
