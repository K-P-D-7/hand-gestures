# Hand Gestures

Control your Linux desktop with hand gestures through your webcam: scroll pages and
switch tabs without touching the mouse. Works on **Wayland** (tested on CachyOS / KDE
Plasma) and X11, because input is sent through `/dev/uinput`.

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

## Install

Requires Python 3, a webcam, and `curl`.

```sh
git clone https://github.com/<you>/hand-gestures.git
cd hand-gestures
./install.sh
```

`install.sh` creates a virtual environment, downloads the MediaPipe hand model, adds
a udev rule so your session can use `/dev/uinput` (asks for sudo), and adds
**Hand Gestures** to your app menu. Log out and back in if it reports no permission
for `/dev/uinput`.

## Run

Launch **Hand Gestures** from the app menu, or:

```sh
./run.sh              # default camera
./run.sh --camera 2   # pick another camera
```

## Tuning

The constants at the top of `hand_gestures.py` control sensitivity, for example
`SCROLL_ACCEL` (how much more big waves scroll than small ones), `SWIPE_DISTANCE`
and `TOGGLE_WINDOW`.

## License

MIT. Hand tracking uses Google's [MediaPipe](https://github.com/google-ai-edge/mediapipe)
hand landmarker model (Apache 2.0), downloaded during install.
