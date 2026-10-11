# Hand Gestures

Control your computer with hand gestures through your webcam: scroll pages and
switch tabs without touching the mouse. Runs on **Linux** (Wayland and X11, tested on
CachyOS / KDE Plasma) and **Windows 10/11**.

## Gestures

| Gesture | Action |
|---|---|
| ✌️ Index + middle finger up, wave up / down | Scroll. Bigger waves scroll more; small finger flicks scroll a little |
| 🤟 Index + middle + ring finger up, swipe right / left | Next / previous tab (Ctrl+Tab), or window (Alt+Tab) |
| 🖐️✊🖐️✊ Open and close all five fingers twice | Stop / resume gestures |

When you scroll, bringing your hand back to where it started does not scroll the page
back. To scroll the other way, hold still for a moment (the **Pause** setting) first.

## Control window

A small floating window that stays on top. Drag it anywhere and it remembers its
position. It shows what the camera sees and has:

- **Stop / Resume** button
- Swipe mode: browser tabs (Ctrl+Tab) or windows (Alt+Tab)
- Scroll speed, pause length, reverse scroll direction
- Optional camera preview with the tracked hand points

## Install on Linux

Requires Python 3, a webcam, and `curl`.

```sh
git clone https://github.com/K-P-D-7/hand-gestures.git
cd hand-gestures
./install.sh
```

`install.sh` creates a virtual environment, downloads the MediaPipe hand model, adds
a udev rule so your session can use `/dev/uinput` (asks for sudo), and adds
**Hand Gestures** to your app menu. Log out and back in if it reports no permission
for `/dev/uinput`.

Launch **Hand Gestures** from the app menu, or:

```sh
./run.sh              # default camera
./run.sh --camera 2   # pick another camera
```

## Install on Windows

1. Install [Python 3](https://www.python.org/downloads/) and tick
   **Add python.exe to PATH** in the installer.
2. Download this repo (**Code → Download ZIP**, then extract it) or `git clone` it.
3. Double-click **`install.bat`**.

The installer sets up a virtual environment, downloads the hand model, and adds
**Hand Gestures** shortcuts to the Start menu and desktop. You can also start it with
`run.bat` (for another camera: `run.bat --camera 1`).

On Windows, Alt+Tab mode can't switch to apps running as administrator unless Hand
Gestures also runs as administrator (a Windows security rule).

## Tuning

The constants at the top of `hand_gestures.py` control sensitivity, for example
`SCROLL_ACCEL` (how much more big waves scroll than small ones), `SWIPE_DISTANCE`
and `TOGGLE_WINDOW`.

## License

MIT. Hand tracking uses Google's [MediaPipe](https://github.com/google-ai-edge/mediapipe)
hand landmarker model (Apache 2.0), downloaded during install.
