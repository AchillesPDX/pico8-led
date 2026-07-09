#!/bin/bash
# switch_to_main.sh
#
# Switches the Pi from the fancy overlay setup back to the plain ("main")
# setup: stops/disables/removes the three fancy units, re-enables
# now-playing.service, checks out main so files on disk match what's
# running.
#
# Run from ~/pico8-led. Needs sudo (will prompt).

set -euo pipefail

cd "$(dirname "$0")"

echo "== Checking git status before switching branches =="
if [ -n "$(git status --porcelain)" ]; then
    echo "You have uncommitted changes. Commit and push them to fancy first:"
    git status --short
    exit 1
fi

echo "== Stopping/disabling fancy services =="
sudo systemctl disable --now now-playing-fancy.service || true
sudo systemctl disable --now overlay-display.service || true
sudo systemctl disable --now picom.service || true

echo "== Removing fancy unit files =="
sudo rm -f /etc/systemd/system/now-playing-fancy.service
sudo rm -f /etc/systemd/system/overlay-display.service
sudo rm -f /etc/systemd/system/picom.service
sudo systemctl daemon-reload

echo "== Enabling and starting the plain poller =="
sudo systemctl enable --now now-playing.service

echo "== Checking out main branch =="
git checkout main

echo "== Status =="
systemctl is-active now-playing.service now-playing-fancy.service overlay-display.service picom.service || true

echo "Done. Should read: active / inactive / inactive / inactive"
