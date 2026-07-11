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

# pico8_64 is no longer launched here. It used to start unconditionally
# at boot and just sit there rendering (unmapped but still running)
# whenever we were in Spotify mode, which is what was keeping the Pi
# warm at idle. pico8_supervisor.sh now owns its whole lifecycle,
# launching it only when we're actually in game mode - see that script
# for why this couldn't just live in switch_mode.sh instead.
killall pico8_64 2>/dev/null

# feh refuses to start if the image file doesn't exist yet. now_playing.py
# writes a placeholder immediately on its own startup, but there's no
# guarantee it's finished doing that by the time we get here - wait for
# the file rather than assuming.
for i in $(seq 1 50); do
  [ -f ./nowplaying.png ] && break
  sleep 0.2
done
feh --title nowplaying --geometry 128x128+128+128 --borderless --reload 3 ./nowplaying.png > /tmp/feh.log 2>&1 &

# Wait for the spotify window to actually exist before mapping it.
# There's no PICO-8 window to wait for anymore at boot - it's no longer
# launched here, so this no longer needs to poll for both.
for i in $(seq 1 50); do
  spotify_win=$(xdotool search --name "nowplaying" 2>/dev/null | head -n1)
  [ -n "$spotify_win" ] && break
  sleep 0.2
done

./toggle_display.sh spotify   # default to album art on boot

# Owns pico8_64's whole lifecycle from here - launches it on demand
# when switch_mode.sh flips into game mode, kills it cleanly when we
# switch away. See pico8_supervisor.sh for the full rationale.
./pico8_supervisor.sh &

./run_led.sh
