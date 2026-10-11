#!/bin/sh
# XWayland (xcb) lets the floating window stay on top and remember its position on KDE Wayland.
cd "$(dirname "$(readlink -f "$0")")"
QT_QPA_PLATFORM=xcb exec .venv/bin/python hand_gestures.py "$@"
