#!/bin/bash
# toggle_display.sh - switches between the PICO-8 window and the Spotify
# "now playing" album art window, both of which sit in the same 128x128
# screen region that xserver-screen captures. Only one is ever mapped
# (visible) at a time; the other is unmapped so it doesn't render.
#
# Current mode is derived directly from X11 (which window is currently
# mapped) rather than tracked in a separate state file - this avoids a
# class of bug where a file in /tmp needs to be writable across a
# privilege drop and possibly across a systemd PrivateTmp boundary,
# which turned out to be unreliable in practice.
#
# Usage:
#   ./toggle_display.sh game      # force PICO-8 to be visible
#   ./toggle_display.sh spotify   # force album art to be visible
#   ./toggle_display.sh toggle    # flip from whichever is currently showing

export DISPLAY=:3.0

PICO_TITLE="PICO-8"
SPOTIFY_TITLE="nowplaying"

get_win() {
  xdotool search --name "$1" 2>/dev/null | head -n1
}

is_mapped() {
  local win="$1"
  [ -z "$win" ] && return 1
  xwininfo -id "$win" 2>/dev/null | grep -q "IsViewable"
}

set_mode() {
  local mode="$1"
  local pico_win
  local spotify_win
  pico_win=$(get_win "$PICO_TITLE")
  spotify_win=$(get_win "$SPOTIFY_TITLE")

  if [ "$mode" = "spotify" ]; then
    [ -n "$pico_win" ] && xdotool windowunmap "$pico_win"
    [ -n "$spotify_win" ] && xdotool windowmap "$spotify_win"
  else
    mode="game"
    [ -n "$spotify_win" ] && xdotool windowunmap "$spotify_win"
    if [ -n "$pico_win" ]; then
      xdotool windowmap "$pico_win"
      # Mapping a window doesn't restore input focus on its own, and
      # there's no window manager here to do it for us. Without this,
      # PICO-8 stays unfocused and stops responding to the controller
      # (it likely pauses input handling while unfocused).
      xdotool windowfocus "$pico_win"
      xdotool windowactivate "$pico_win"
    fi
  fi
  echo "Display mode: $mode"
}

case "$1" in
  game|spotify)
    set_mode "$1"
    ;;
  toggle)
    pico_win=$(get_win "$PICO_TITLE")
    if is_mapped "$pico_win"; then
      set_mode "spotify"
    else
      set_mode "game"
    fi
    ;;
  *)
    echo "Usage: $0 {game|spotify|toggle}"
    exit 1
    ;;
esac
