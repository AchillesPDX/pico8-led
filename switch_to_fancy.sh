#!/bin/bash
# switch_to_fancy.sh
#
# Switches the Pi from the plain ("main") setup to the fancy overlay setup:
# stops/disables now-playing.service, installs and starts the three fancy
# units, checks out the fancy branch so files on disk match what's running.
#
# Run from ~/pico8-led. Needs sudo (will prompt).

set -euo pipefail

cd "$(dirname "$0")"

echo "== Checking git status before switching branches =="
if [ -n "$(git status --porcelain --ignore-submodules=dirty)" ]; then
    echo "You have uncommitted changes. Commit or stash them first:"
    git status --short --ignore-submodules=dirty
    exit 1
fi

echo "== Checking out fancy branch =="
git checkout fancy

echo "== Stopping/disabling the plain poller =="
sudo systemctl disable --now now-playing.service

echo "== Installing fancy systemd units =="
sudo cp now-playing-fancy.service /etc/systemd/system/
sudo cp overlay-display.service /etc/systemd/system/
sudo cp picom.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "== Enabling and starting fancy services =="
sudo systemctl enable --now now-playing-fancy.service
sudo systemctl enable --now picom.service
sudo systemctl enable --now overlay-display.service

echo "== Status =="
systemctl is-active now-playing.service now-playing-fancy.service overlay-display.service picom.service || true

echo "Done. Should read: inactive / active / active / active"
