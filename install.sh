#!/bin/sh
# Sets up the Python environment, downloads the hand model, grants your login
# session access to /dev/uinput and adds an app-menu entry.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python environment..."
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
# mediapipe pulls in the GUI build of OpenCV, whose bundled Qt clashes with PyQt6
.venv/bin/pip uninstall -y -q opencv-contrib-python opencv-python 2>/dev/null || true
.venv/bin/pip install -q --force-reinstall --no-deps opencv-contrib-python-headless

if [ ! -f hand_landmarker.task ]; then
  echo "Downloading hand landmark model..."
  curl -sSfL -o hand_landmarker.task \
    https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task
fi

echo "Granting access to /dev/uinput (needs sudo)..."
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' |
  sudo tee /etc/udev/rules.d/60-hand-gestures-uinput.rules >/dev/null
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=uinput

mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/hand-gestures.desktop <<EOD
[Desktop Entry]
Type=Application
Name=Hand Gestures
Comment=Scroll and switch tabs with hand gestures
Exec=$DIR/run.sh
Icon=input-touchpad
Terminal=false
Categories=Utility;
EOD
echo "Installed. Launch 'Hand Gestures' from the app menu or run $DIR/run.sh"
