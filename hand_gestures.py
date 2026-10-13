#!/usr/bin/env python3
"""Hand-gesture control for Linux (Wayland/X11) and Windows using a webcam.

Gestures
  * 2 fingers (index + middle) up, move hand up/down  -> scroll
  * 3 fingers (index + middle + ring) swipe left/right -> previous / next tab
    (Ctrl+Shift+Tab / Ctrl+Tab, or Alt+Shift+Tab / Alt+Tab in "Windows" mode)
  * Open + close all 5 fingers twice                   -> stop / resume gestures
  * Pinch index finger and thumb together once         -> play / pause media (videos, music)
  * Flash 4 fingers (thumb tucked) twice               -> minimize / expand the window
  * Cross both index fingers into an X and hold        -> close the app

On Linux input is injected through /dev/uinput, so it works on KDE Wayland.
On Windows it uses SendInput.
"""
import argparse
import math
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision
from PyQt6.QtCore import QSettings, QThread, Qt, pyqtSignal, QPoint
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout,
                             QLabel, QPushButton, QSlider, QVBoxLayout, QWidget)

HERE = Path(__file__).resolve().parent
MODEL = HERE / "hand_landmarker.task"

# --- tuning ---------------------------------------------------------------
POSE_STABLE_FRAMES = 3        # frames a pose must persist before it counts
# Scroll speeds are in frame heights per second, measured on a real hand at 30 fps:
# flicks peaked at 0.3-3.2/s and were always faster than the return that followed.
SCROLL_COMMIT = 0.02          # fraction of frame height before a movement can scroll
SCROLL_REVERSE = 0.015        # backwards travel that ends a movement
SCROLL_ACCEL = 4.0            # bigger strokes scroll proportionally more (1 + ACCEL * stroke size)
SCROLL_SMOOTH_TAU = 0.04      # seconds; fingertip position smoothing time constant
FLICK_MIN_SPEED = 0.25        # slower movements never scroll (drift, slow returns)
FIRST_FLICK_SPEED = 0.6       # with no scroll direction yet, a flick this fast scrolls
FIRST_FLICK_RATIO = 1.3       # ...or one this many times faster than the movement before it
MAX_MOVE_SCROLL = 0.6         # most one movement can scroll (frame heights, ~30 notches at default speed)
SCROLL_CONFIRM = 0.12         # seconds a scroll is held back; dropped if the hand leaves the frame meanwhile
STILL_RANGE = 0.015           # movement range (frame height) that still counts as "still"
STILL_WINDOW = 0.2            # seconds used to judge stillness
SETTLE_TIME = 0.1             # hand must be still this long (s) before the first scroll
SCROLL_HOLD = 0.8             # keep following the fingertips this long (s) after 2 fingers were last seen
SCROLL_ENTRY_HOLD = 0.5       # a deliberate 2-finger pose must be held this long (s) to start scrolling
HAND_LOST_RESET = 0.6         # forget the scroll stroke if the hand is missing this long (s)
SWIPE_WINDOW = 0.45           # seconds of history used to detect a swipe
SWIPE_DISTANCE = 0.16         # fraction of frame width hand must travel
SWIPE_COOLDOWN = 0.8          # seconds between tab switches
TOGGLE_WINDOW = 2.5           # seconds to complete open-close-open-close (or 4-finger flashes)
THUMB_TUCKED = 0.65           # thumb tip within this many palm sizes of the palm centre = tucked
PINCH_DISTANCE = 0.25         # thumb-index tip gap (fraction of palm size) that counts as a pinch
PINCH_COOLDOWN = 0.6          # seconds before another pinch can toggle again
PINCH_HOLD = 0.2              # pinch must be held this long (s), so passing fingers don't trigger it
CROSS_HOLD = 0.6              # index fingers crossed (X) this long (s) closes the app
CROSS_INDEX_MAX_BEND = 60     # each index finger must be roughly straight (degrees of bend)
# 2-finger pose, as finger bend in degrees (knuckle + middle joint, 0 = straight).
# Measured on a real hand held upright, tilted sideways and pointing at the screen:
# the ring finger is folded 100-185 deg and 25-135 deg more than index/middle, while
# random finger movement stays within ~50 deg. (start = strict, keep going = loose)
TWO_RING_BEND = (80, 60)      # ring finger bent at least this much
TWO_RING_MARGIN = (20, 10)    # ...and this much more than index and middle
TWO_INDEX_MAX = (70, 90)      # index bent at most this much
TWO_MIDDLE_MAX = (130, 140)   # middle bent at most this much (looks bent when pointing at the camera)
                              # (looser than 1.1: they bend a little while flicking)


# --- input injection ------------------------------------------------------
class LinuxInjector:
    def __init__(self):
        from evdev import UInput, ecodes as e
        self.e = e
        self.keys = {"ctrl": e.KEY_LEFTCTRL, "shift": e.KEY_LEFTSHIFT,
                     "alt": e.KEY_LEFTALT, "tab": e.KEY_TAB,
                     "playpause": e.KEY_PLAYPAUSE, "space": e.KEY_SPACE}
        self.kbd = UInput({e.EV_KEY: list(range(1, 249))}, name="hand-gestures-kbd")
        self.mouse = UInput({
            e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_WHEEL_HI_RES],
        }, name="hand-gestures-mouse")
        self._hires_acc = 0.0
        self._notch_acc = 0

    def scroll(self, hires_units):
        """Scroll by hi-res wheel units (120 = one notch). Positive = up."""
        e = self.e
        self._hires_acc += hires_units
        hires = int(self._hires_acc)
        if hires == 0:
            return
        self._hires_acc -= hires
        self.mouse.write(e.EV_REL, e.REL_WHEEL_HI_RES, hires)
        # legacy wheel events for apps that ignore hi-res
        self._notch_acc += hires
        notches = int(self._notch_acc / 120)
        if notches:
            self._notch_acc -= notches * 120
            self.mouse.write(e.EV_REL, e.REL_WHEEL, notches)
        self.mouse.syn()

    def combo(self, *names):
        """Press and release a key combination, e.g. combo("ctrl", "tab")."""
        keys = [self.keys[n] for n in names]
        for k in keys:
            self.kbd.write(self.e.EV_KEY, k, 1)
            self.kbd.syn()
            time.sleep(0.01)
        for k in reversed(keys):
            self.kbd.write(self.e.EV_KEY, k, 0)
            self.kbd.syn()
            time.sleep(0.01)

    def close(self):
        self.kbd.close()
        self.mouse.close()


class WindowsInjector:
    """Same interface as LinuxInjector, using the Win32 SendInput API."""

    def __init__(self):
        import ctypes
        self.ct = ctypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                        ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                        ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                        ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint32), ("u", _U)]

        self.INPUT, self.MOUSEINPUT, self.KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        self.keys = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "tab": 0x09,
                     "playpause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
                     "space": 0x20}
        self._acc = 0.0

    def _send(self, inp):
        self.ct.windll.user32.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT))

    def scroll(self, hires_units):
        """Scroll by wheel units (120 = one notch). Positive = up."""
        self._acc += hires_units
        delta = int(self._acc)
        if delta == 0:
            return
        self._acc -= delta
        MOUSEEVENTF_WHEEL = 0x0800
        inp = self.INPUT(type=0)  # INPUT_MOUSE
        inp.u.mi = self.MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL, 0, 0)
        self._send(inp)

    def _key(self, vk, up):
        inp = self.INPUT(type=1)  # INPUT_KEYBOARD
        inp.u.ki = self.KEYBDINPUT(vk, 0, 0x0002 if up else 0, 0, 0)  # KEYEVENTF_KEYUP
        self._send(inp)

    def combo(self, *names):
        keys = [self.keys[n] for n in names]
        for k in keys:
            self._key(k, False)
            time.sleep(0.01)
        for k in reversed(keys):
            self._key(k, True)
            time.sleep(0.01)

    def close(self):
        pass


IS_WINDOWS = sys.platform == "win32"
Injector = WindowsInjector if IS_WINDOWS else LinuxInjector


# --- hand pose classification ---------------------------------------------
def _d(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def fingers_up(lm):
    """Return [thumb, index, middle, ring, pinky] extended flags."""
    wrist = lm[0]
    up = []
    # thumb: tip farther from pinky base than the thumb IP joint, and away from palm
    palm = _d(lm[0], lm[9])
    up.append(_d(lm[4], lm[17]) > _d(lm[3], lm[17]) and _d(lm[4], lm[5]) > 0.35 * palm)
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        up.append(_d(lm[tip], wrist) > _d(lm[pip], wrist) * 1.1)
    return up


def _d3(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5


def _angle(a, b, c):
    """Angle at b in degrees, in 3D."""
    v1 = (a.x - b.x, a.y - b.y, a.z - b.z)
    v2 = (c.x - b.x, c.y - b.y, c.z - b.z)
    n = math.hypot(*v1) * math.hypot(*v2)
    cos = sum(x * y for x, y in zip(v1, v2)) / n if n else 1.0
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def _bend(lm, mcp, pip, dip):
    """How bent a finger is at its knuckle and middle joint (0 = straight). Angles
    hold up when the hand is tilted or points at the camera; lengths don't."""
    return (180 - _angle(lm[0], lm[mcp], lm[pip])) + (180 - _angle(lm[mcp], lm[pip], lm[dip]))


def _is_two(lm, strict):
    k = 0 if strict else 1
    i, m, r = _bend(lm, 5, 6, 7), _bend(lm, 9, 10, 11), _bend(lm, 13, 14, 15)
    return (r >= TWO_RING_BEND[k] and r - max(i, m) >= TWO_RING_MARGIN[k]
            and i <= TWO_INDEX_MAX[k] and m <= TWO_MIDDLE_MAX[k])


def _segments_cross(a1, a2, b1, b2):
    """True if 2D segments a1-a2 and b1-b2 intersect."""
    def side(p, q, r):
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)
    return (side(a1, a2, b1) * side(a1, a2, b2) < 0
            and side(b1, b2, a1) * side(b1, b2, a2) < 0)


def crossed_index_fingers(h1, h2):
    """Two hands with straight index fingers crossing each other in an X."""
    if _bend(h1, 5, 6, 7) > CROSS_INDEX_MAX_BEND or _bend(h2, 5, 6, 7) > CROSS_INDEX_MAX_BEND:
        return False
    if not _segments_cross(h1[5], h1[8], h2[5], h2[8]):
        return False
    # an X, not two fingers lying along each other: 35-145 degrees apart
    v1 = (h1[8].x - h1[5].x, h1[8].y - h1[5].y)
    v2 = (h2[8].x - h2[5].x, h2[8].y - h2[5].y)
    cos = abs(v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2) or 1)
    return cos < math.cos(math.radians(35))


def strict_two(lm):
    """A deliberate 2-finger pose (index + middle out, ring folded), held upright,
    tilted or pointing at the screen. Required to *start* scrolling."""
    return _is_two(lm, strict=True)


def classify(lm):
    palm = _d(lm[0], lm[9])
    # pinch: thumb and index tips touching, index reaching out (not curled into a fist)
    if _d3(lm[4], lm[8]) < PINCH_DISTANCE * palm and _d(lm[8], lm[0]) > 0.9 * palm:
        return "pinch"
    # two: index + middle out, ring folded; the pinky is ignored (often loosely curled)
    if _is_two(lm, strict=False):
        return "two"
    _, i, m, r, p = fingers_up(lm)
    if i and m and r and p:
        # measured: tucked thumb sits ~0.2 palm sizes from the palm centre, spread ~0.9
        centre = [sum(c) / 5 for c in zip(*((lm[k].x, lm[k].y) for k in (0, 5, 9, 13, 17)))]
        thumb_gap = ((lm[4].x - centre[0]) ** 2 + (lm[4].y - centre[1]) ** 2) ** 0.5
        return "four" if thumb_gap < THUMB_TUCKED * palm else "open"
    if not (i or m or r or p):
        return "fist"
    if i and m and r and not p:
        return "three"
    return "other"


# --- scroll stroke tracking ------------------------------------------------
class ScrollTracker:
    """Turns fingertip height into scroll amounts.

    The hand's path is split into movements at each reversal. Flicks are faster than
    the return that follows them, so speed decides which movements scroll:
      * with no scroll direction yet, a movement scrolls if it is a clear flick
        (FIRST_FLICK_SPEED, or FIRST_FLICK_RATIO x faster than the movement before);
      * after that, movements in the same direction scroll if they beat
        FLICK_MIN_SPEED, and movements the other way are returns that never scroll
        (however fast: dropping the hand at the end is fast too);
      * resting for `pause` seconds forgets the direction.
    Nothing scrolls until the hand has been still once after the tracker starts, so
    raising the hand into position (or joining mid-wave) doesn't scroll.
    A scrolling movement scrolls its whole length, accelerated by its size, up to
    MAX_MOVE_SCROLL. `move_id` changes with every movement (for one-step scrolling).
    """

    def __init__(self):
        self.pause = 1.5
        self.reset()

    def reset(self):
        self.pos = None
        self.t = None
        self.dir = 0                  # direction of the current movement, +1 up / -1 down
        self.start = self.extreme = 0.0
        self.peak = 0.0               # peak speed of the current movement
        self.scrolling = False        # current movement has qualified as a scroll
        self.scrolled_to = 0.0        # position up to which it has scrolled
        self.prev_peak = 0.0          # peak speed of the previous movement
        self.pref = 0                 # scroll direction learned from the last flick
        self.hist = deque()
        self.still_since = None
        self.settled = False          # hand has been still since the tracker started
        self.move_settled = False     # current movement began after settling
        self.move_scrolled = 0.0      # how much the current movement has scrolled
        self.move_id = getattr(self, "move_id", 0) + 1

    def update(self, t, y):
        """y: normalized fingertip height (0 = top). Returns scroll in frame heights, + = up."""
        up = -y
        if self.pos is None:
            self.pos = self.start = self.extreme = up
            self.t = t
            return 0.0
        dt = max(t - self.t, 1e-3)
        prev = self.pos
        self.pos += (1 - math.exp(-dt / SCROLL_SMOOTH_TAU)) * (up - self.pos)
        self.t = t
        p = self.pos
        speed = abs(p - prev) / dt

        self.hist.append((t, p))
        while self.hist and t - self.hist[0][0] > STILL_WINDOW:
            self.hist.popleft()
        vals = [v for _, v in self.hist]
        if max(vals) - min(vals) < STILL_RANGE:
            self.still_since = self.still_since or t
            if t - self.still_since >= SETTLE_TIME:
                self.settled = True
            if t - self.still_since >= self.pause:
                self.pref = 0
        else:
            self.still_since = None

        if self.dir == 0:             # waiting for the first movement
            if abs(p - self.start) > SCROLL_REVERSE:
                self.dir = 1 if p > self.start else -1
                self.extreme, self.peak = p, speed
                self.move_settled = self.settled
                self.move_id += 1
            return 0.0
        if (p - self.extreme) * self.dir >= 0:      # still moving the same way
            self.extreme = p
            if (p - prev) * self.dir > 0:
                self.peak = max(self.peak, speed)
        elif (self.extreme - p) * self.dir > SCROLL_REVERSE:   # reversed: new movement
            self.prev_peak = self.peak
            self.dir, self.start, self.extreme = -self.dir, self.extreme, p
            self.peak, self.scrolling = speed, False
            self.move_settled = self.settled
            self.move_id += 1

        if not self.scrolling and self._qualifies(p):
            self.scrolling, self.scrolled_to, self.move_scrolled = True, self.start, 0.0
            self.pref = self.dir
        if self.scrolling and (p - self.scrolled_to) * self.dir > 0:
            delta = p - self.scrolled_to
            self.scrolled_to = p
            amount = abs(delta) * (1 + SCROLL_ACCEL * abs(p - self.start))
            amount = min(amount, MAX_MOVE_SCROLL - self.move_scrolled)
            self.move_scrolled += amount
            return amount * self.dir
        return 0.0

    def _qualifies(self, p):
        if (not self.move_settled or abs(p - self.start) < SCROLL_COMMIT
                or self.peak < FLICK_MIN_SPEED):
            return False
        if self.pref == self.dir:
            return True
        if self.pref == -self.dir:
            return False              # a return; rest for `pause` to change direction
        # no direction yet: a clear flick, or clearly faster than the movement before
        # (the very first movement has nothing to compare with)
        return (self.peak > FIRST_FLICK_SPEED
                or self.prev_peak > 0 and self.peak > FIRST_FLICK_RATIO * self.prev_peak)


# --- media players ----------------------------------------------------------
def media_states():
    """Playback status of every MPRIS media player on Linux, or None if unknown."""
    if IS_WINDOWS:
        return None
    try:
        out = subprocess.run(["busctl", "--user", "list"], capture_output=True,
                             text=True, timeout=1).stdout
        states = {}
        for name in sorted({l.split()[0] for l in out.splitlines()
                            if l.startswith("org.mpris.MediaPlayer2.")}):
            r = subprocess.run(["busctl", "--user", "get-property", name, "/org/mpris/MediaPlayer2",
                                "org.mpris.MediaPlayer2.Player", "PlaybackStatus"],
                               capture_output=True, text=True, timeout=1)
            states[name] = r.stdout.strip()
        return states
    except (OSError, subprocess.SubprocessError):
        return None


# --- worker thread ----------------------------------------------------------
class GestureWorker(QThread):
    status = pyqtSignal(str)          # short status text
    event = pyqtSignal(str)           # last action performed
    paused_changed = pyqtSignal(bool)
    frame = pyqtSignal(QImage)
    minimize_toggled = pyqtSignal()   # 4 fingers flashed twice
    quit_requested = pyqtSignal()     # index fingers crossed

    def __init__(self, camera, injector):
        super().__init__()
        self.camera = camera
        self.inj = injector
        self.paused = False
        self.running = True
        self.mode = "tabs"            # "tabs" or "windows"
        self.reverse_scroll = False
        self.scroll_speed = 6000      # hi-res units per full frame height
        self.scroll_style = "smooth"  # "smooth", or "step": one wheel notch per flick (Shorts, Reels)
        self.pinch_action = "auto"    # "auto", "media" (play/pause key) or "space"
        self.scroll = ScrollTracker()
        self.preview = False

    def set_paused(self, value):
        if value != self.paused:
            self.paused = value
            self.paused_changed.emit(value)
            self.event.emit("Paused" if value else "Resumed")

    def run(self):
        cap = cv2.VideoCapture(self.camera, cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if not IS_WINDOWS:
            # Many webcams drop to ~10 fps in dim light when this is on; fast flicks
            # then last only 1-2 frames. Darker image at 30 fps tracks much better.
            try:
                subprocess.run(["v4l2-ctl", "-d", f"/dev/video{self.camera}",
                                "-c", "exposure_dynamic_framerate=0"],
                               capture_output=True, timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
        if not cap.isOpened():
            self.status.emit(f"Cannot open camera {self.camera}")
            return
        landmarker = vision.HandLandmarker.create_from_options(
            vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(MODEL)),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=2,
                min_hand_detection_confidence=0.6,
                min_hand_presence_confidence=0.6,
                min_tracking_confidence=0.5,
            ))

        raw_pose, raw_count, pose = None, 0, None
        last_two = -1e9               # last time the 2-finger pose was seen
        strict_since = None           # start of the current deliberate 2-finger hold
        strict_last = -1e9            # last frame the deliberate pose was seen
        scroll_active = False
        last_hand = -1e9
        pinch_armed = True
        pinch_since = None
        swipe_hist = deque()
        last_swipe = 0.0
        toggle_events = deque()       # times of open->fist transitions
        four_events = deque()         # times the 4-finger pose appeared
        last_pinch = -PINCH_COOLDOWN
        pending = deque()             # scrolls held back for SCROLL_CONFIRM: (time, amount, move id)
        last_step_move = None
        prev_pose = None
        cross_since = None
        main_wrist = None             # follow the same hand when two are in view
        t0 = time.monotonic()

        while self.running:
            ok, img = cap.read()
            if not ok:
                self.status.emit("Camera read failed")
                time.sleep(0.2)
                continue
            img = cv2.flip(img, 1)    # mirror so your right is screen right
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            now = time.monotonic()
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                int((now - t0) * 1000))

            if not res.hand_landmarks:
                raw_pose, raw_count, pose = None, 0, None
                strict_since = None
                pending.clear()       # the hand left the frame: that was no flick
                if now - last_hand > HAND_LOST_RESET:
                    self.scroll.reset()
                swipe_hist.clear()
                self.status.emit("Paused · no hand" if self.paused else "No hand")
                self._emit_preview(rgb, None)
                continue

            hands = res.hand_landmarks
            # --- close the app: index fingers crossed in an X, held -------------
            if len(hands) >= 2 and crossed_index_fingers(hands[0], hands[1]):
                cross_since = cross_since or now
                held = now - cross_since
                self.status.emit("✕ Hold to close…" if held < CROSS_HOLD else "Closing")
                if held >= CROSS_HOLD:
                    self.event.emit("Closing ✕")
                    self.quit_requested.emit()
                    self.running = False
                self._emit_preview(rgb, hands[0])
                pending.clear()
                continue
            cross_since = None
            # the other gestures use one hand: the one nearest to where it was last frame
            if len(hands) >= 2 and main_wrist is not None:
                hands = sorted(hands, key=lambda h: (h[0].x - main_wrist[0]) ** 2
                               + (h[0].y - main_wrist[1]) ** 2)
            lm = hands[0]
            main_wrist = (lm[0].x, lm[0].y)
            last_hand = now
            p = classify(lm)
            raw_count = raw_count + 1 if p == raw_pose else 1
            raw_pose = p
            if raw_count >= POSE_STABLE_FRAMES:
                pose = p

            # Scroll mode: started by holding a deliberate 2-finger pose for
            # SCROLL_ENTRY_HOLD, then the fingertips are followed whatever the pose reads
            # mid-motion (bent fingers look like a fist or a pinch), until 2 fingers
            # haven't been seen for SCROLL_HOLD or 3/4 fingers show.
            if strict_two(lm):          # a single odd frame (<0.15 s) doesn't break the hold
                if strict_since is None or now - strict_last > 0.15:
                    strict_since = now
                strict_last = now
            if not scroll_active and strict_since is not None and strict_last == now \
                    and now - strict_since >= SCROLL_ENTRY_HOLD and not self.paused:
                scroll_active, last_two = True, now
                self.scroll.reset()
                self.event.emit("Scroll mode ✌")
            if scroll_active and p == "two":
                last_two = now
            if pose in ("three", "four") or now - last_two >= SCROLL_HOLD or self.paused:
                scroll_active = False
            scrolling = scroll_active

            # --- pause / window gestures (ignored while scrolling) ----------
            if pose != "pinch":
                pinch_armed, pinch_since = True, None
            elif pinch_since is None:
                pinch_since = now
            if scrolling:
                toggle_events.clear()
                four_events.clear()
                prev_pose = pose
            elif pose != prev_pose:
                if prev_pose == "open" and pose == "fist":
                    toggle_events.append(now)
                if pose == "four":
                    four_events.append(now)
                prev_pose = pose
            # --- play/pause media: pinch once ---------------------------
            if (not scrolling and not self.paused and pose == "pinch" and pinch_armed
                    and now - pinch_since >= PINCH_HOLD and now - last_pinch > PINCH_COOLDOWN):
                pinch_armed = False
                last_pinch = now
                threading.Thread(target=self._play_pause, daemon=True).start()
            # --- stop/resume: open-close twice ----------------------------
            while toggle_events and now - toggle_events[0] > TOGGLE_WINDOW:
                toggle_events.popleft()
            if len(toggle_events) >= 2:
                toggle_events.clear()
                self.set_paused(not self.paused)

            # --- minimize / expand the window: 4 fingers flashed twice ----
            while four_events and now - four_events[0] > TOGGLE_WINDOW:
                four_events.popleft()
            if len(four_events) >= 2:
                four_events.clear()
                self.minimize_toggled.emit()

            self.status.emit(("Paused · " if self.paused else "Active · ") + (pose or "…"))
            self._emit_preview(rgb, lm)
            if self.paused:
                self.scroll.reset()
                swipe_hist.clear()
                continue

            # --- scroll: two fingers --------------------------------------
            if scrolling:
                amount = self.scroll.update(now, (lm[8].y + lm[12].y) / 2)
                if amount:
                    pending.append((now, -amount if self.reverse_scroll else amount,
                                    self.scroll.move_id))
            else:
                self.scroll.reset()
                pending.clear()
            while pending and now - pending[0][0] >= SCROLL_CONFIRM:
                _, amount, move = pending.popleft()
                if self.scroll_style == "step":
                    if move != last_step_move:        # one notch per flick
                        last_step_move = move
                        self.inj.scroll(120 if amount > 0 else -120)
                        self.event.emit("Previous ↑" if amount > 0 else "Next ↓")
                else:
                    self.inj.scroll(amount * self.scroll_speed)

            # --- swipe: three fingers -------------------------------------
            if pose == "three":
                x = (lm[8].x + lm[12].x + lm[16].x) / 3
                swipe_hist.append((now, x))
                while swipe_hist and now - swipe_hist[0][0] > SWIPE_WINDOW:
                    swipe_hist.popleft()
                dx = x - swipe_hist[0][1]
                if abs(dx) > SWIPE_DISTANCE and now - last_swipe > SWIPE_COOLDOWN:
                    last_swipe = now
                    swipe_hist.clear()
                    self._switch(right=dx > 0)
            else:
                swipe_hist.clear()

        cap.release()
        landmarker.close()

    def _play_pause(self):
        """Pinch action. In auto mode, send the media play/pause key; if no media
        player reacts (many streaming sites don't register one), press Space."""
        if self.pinch_action == "space":
            self.inj.combo("space")
            self.event.emit("Play / Pause (Space)")
            return
        before = None if self.pinch_action == "media" else media_states()
        self.inj.combo("playpause")
        if before is not None:
            time.sleep(0.4)
            if media_states() == before:
                self.inj.combo("space")
                self.event.emit("Play / Pause (Space)")
                return
        self.event.emit("Play / Pause ⏯")

    def _switch(self, right):
        mod = "alt" if self.mode == "windows" else "ctrl"
        if right:
            self.inj.combo(mod, "tab")
        else:
            self.inj.combo(mod, "shift", "tab")
        what = "window" if self.mode == "windows" else "tab"
        self.event.emit(f"{'Next' if right else 'Previous'} {what} {'→' if right else '←'}")

    def _emit_preview(self, rgb, lm):
        if not self.preview:
            return
        small = cv2.resize(rgb, (192, 144))
        if lm is not None:
            for pt in lm:
                cv2.circle(small, (int(pt.x * 192), int(pt.y * 144)), 2, (0, 255, 120), -1)
        h, w, _ = small.shape
        self.frame.emit(QImage(small.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy())

    def stop(self):
        self.running = False
        self.wait(2000)


# --- floating control window ------------------------------------------------
STYLE = """
QWidget#root { background: #1e1f24; border: 1px solid #3a3c44; border-radius: 10px; }
QLabel { color: #e6e6e6; font-size: 12px; }
QLabel#title { font-weight: 600; font-size: 13px; }
QLabel#event { color: #9aa0aa; }
QPushButton { background: #2d6cdf; color: white; border: none; border-radius: 6px;
              padding: 6px 10px; font-weight: 600; }
QPushButton#paused { background: #2e9e5b; }
QPushButton#close, QPushButton#minimize { background: transparent; color: #9aa0aa;
                                          padding: 0 4px; font-size: 14px; }
QPushButton#close:hover { color: #ff6b6b; }
QPushButton#minimize:hover { color: #ffffff; }
QComboBox, QCheckBox { color: #e6e6e6; font-size: 11px; }
"""


class MiniBubble(QWidget):
    """Small round stand-in for the control window while it is minimized."""
    SIZE = 52

    def __init__(self, on_expand):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setToolTip("Hand Gestures: click or flash 4 fingers twice to expand")
        self.on_expand = on_expand
        self.paused = False
        self._press = None
        self._dragged = False

    def set_paused(self, paused):
        self.paused = paused
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#e0a030" if self.paused else "#3ccf70"), 3))
        p.setBrush(QColor("#1e1f24"))
        p.drawEllipse(2, 2, self.SIZE - 4, self.SIZE - 4)
        f = QFont()
        f.setPointSize(16)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "✋")

    # click expands, drag moves
    def mousePressEvent(self, ev):
        self._press = ev.globalPosition().toPoint()
        self._offset = self._press - self.frameGeometry().topLeft()
        self._dragged = False

    def mouseMoveEvent(self, ev):
        if self._press is None:
            return
        pos = ev.globalPosition().toPoint()
        if not self._dragged and (pos - self._press).manhattanLength() > 5:
            self._dragged = True
            handle = self.windowHandle()
            if handle and handle.startSystemMove():
                self._press = None
                return
        if self._dragged:
            self.move(pos - self._offset)

    def mouseReleaseEvent(self, ev):
        if self._press is not None and not self._dragged:
            self.on_expand()
        self._press = None


class ControlWindow(QWidget):
    def __init__(self, worker):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.worker = worker
        self.settings = QSettings("hand-gestures", "hand-gestures")
        self._drag = None

        root = QWidget(self)
        root.setObjectName("root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)

        top = QHBoxLayout()
        self.dot = QLabel("●")
        title = QLabel("Hand Gestures")
        title.setObjectName("title")
        minimize = QPushButton("–")
        minimize.setObjectName("minimize")
        minimize.setToolTip("Minimize to a small bubble (or flash 4 fingers twice)")
        minimize.clicked.connect(self.minimize)
        close = QPushButton("✕")
        close.setObjectName("close")
        close.clicked.connect(self.close)
        top.addWidget(self.dot)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(minimize)
        top.addWidget(close)
        lay.addLayout(top)

        self.status = QLabel("Starting camera…")
        self.last = QLabel("Drag me anywhere")
        self.last.setObjectName("event")
        lay.addWidget(self.status)
        lay.addWidget(self.last)

        self.btn = QPushButton("Stop gestures")
        self.btn.clicked.connect(lambda: worker.set_paused(not worker.paused))
        lay.addWidget(self.btn)

        self.mode = QComboBox()
        self.mode.addItems(["3-finger swipe: browser tabs (Ctrl+Tab)",
                            "3-finger swipe: windows (Alt+Tab)"])
        self.mode.currentIndexChanged.connect(self._mode_changed)
        lay.addWidget(self.mode)

        self.style_box = QComboBox()
        self.style_box.addItems(["Scroll: smooth (pages, documents)",
                                 "Scroll: one step per flick (Shorts, Reels)"])
        self.style_box.currentIndexChanged.connect(self._style_changed)
        lay.addWidget(self.style_box)

        self.pinch_box = QComboBox()
        self.pinch_box.addItems(["Pinch: play/pause (auto)", "Pinch: media key only",
                                 "Pinch: Space key only"])
        self.pinch_box.setToolTip("Auto sends the media play/pause key, and presses Space\n"
                                  "if no media player reacted (e.g. some streaming sites)")
        self.pinch_box.currentIndexChanged.connect(self._pinch_changed)
        lay.addWidget(self.pinch_box)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Scroll speed"))
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(1000, 20000)
        self.speed.valueChanged.connect(self._speed_changed)
        speed_row.addWidget(self.speed)
        lay.addLayout(speed_row)

        pause_row = QHBoxLayout()
        self.pause_label = QLabel()
        pause_row.addWidget(self.pause_label)
        self.pause = QSlider(Qt.Orientation.Horizontal)
        self.pause.setRange(5, 40)    # tenths of a second
        self.pause.setToolTip("How long to rest before you can scroll back the other way")
        self.pause.valueChanged.connect(self._pause_changed)
        pause_row.addWidget(self.pause)
        lay.addLayout(pause_row)

        self.reverse = QCheckBox("Reverse scroll direction")
        self.reverse.toggled.connect(self._reverse_changed)
        lay.addWidget(self.reverse)

        self.preview_cb = QCheckBox("Show camera preview")
        self.preview_cb.toggled.connect(self._preview_changed)
        lay.addWidget(self.preview_cb)
        self.preview = QLabel()
        self.preview.setFixedSize(192, 144)
        self.preview.hide()
        lay.addWidget(self.preview, alignment=Qt.AlignmentFlag.AlignHCenter)

        worker.status.connect(self.status.setText)
        worker.event.connect(self.last.setText)
        worker.paused_changed.connect(self._paused_changed)
        worker.quit_requested.connect(self.close)
        worker.minimize_toggled.connect(
            lambda: self.expand() if self.mini.isVisible() else self.minimize())
        self.mini = MiniBubble(self.expand)
        worker.frame.connect(lambda img: self.preview.setPixmap(QPixmap.fromImage(img)))

        s = self.settings
        self.mode.setCurrentIndex(int(s.value("mode", 0)))
        self.speed.setValue(int(s.value("speed", 6000)))
        self.pause.setValue(int(s.value("return_lock", 15)))
        self._pause_changed(self.pause.value())
        self.reverse.setChecked(s.value("reverse", "false") == "true")
        self.preview_cb.setChecked(s.value("preview", "false") == "true")
        self._mode_changed(self.mode.currentIndex())
        self.style_box.setCurrentIndex(int(s.value("scroll_style", 0)))
        self._style_changed(self.style_box.currentIndex())
        self.pinch_box.setCurrentIndex(int(s.value("pinch_action", 0)))
        self._pinch_changed(self.pinch_box.currentIndex())
        self._speed_changed(self.speed.value())
        self._paused_changed(False)
        self.setFixedWidth(250)
        if s.contains("pos"):
            self.move(s.value("pos", type=QPoint))

    # settings handlers
    def _style_changed(self, i):
        self.worker.scroll_style = "step" if i == 1 else "smooth"
        self.settings.setValue("scroll_style", i)

    def _pinch_changed(self, i):
        self.worker.pinch_action = ("auto", "media", "space")[i]
        self.settings.setValue("pinch_action", i)

    def _mode_changed(self, i):
        self.worker.mode = "windows" if i == 1 else "tabs"
        self.settings.setValue("mode", i)

    def _speed_changed(self, v):
        self.worker.scroll_speed = v
        self.settings.setValue("speed", v)

    def _pause_changed(self, v):
        self.worker.scroll.pause = v / 10
        self.pause_label.setText(f"Return lock {v / 10:.1f}s")
        self.settings.setValue("return_lock", v)

    def _reverse_changed(self, v):
        self.worker.reverse_scroll = v
        self.settings.setValue("reverse", "true" if v else "false")

    def _preview_changed(self, v):
        self.worker.preview = v
        self.preview.setVisible(v)
        self.settings.setValue("preview", "true" if v else "false")
        self.adjustSize()

    def minimize(self):
        self.mini.move(self.pos())
        self.hide()
        self.mini.show()

    def expand(self):
        if not self.mini.isVisible():
            return
        self.move(self.mini.pos())
        self.mini.hide()
        self.show()
        self.raise_()
        self.last.setText("Expanded")

    def _paused_changed(self, paused):
        self.mini.set_paused(paused)
        self.btn.setText("Resume gestures" if paused else "Stop gestures")
        self.btn.setObjectName("paused" if paused else "")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.dot.setStyleSheet(f"color: {'#e0a030' if paused else '#3ccf70'};")

    # dragging
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle and handle.startSystemMove():
                return
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def moveEvent(self, ev):
        self.settings.setValue("pos", self.pos())

    def closeEvent(self, ev):
        self.settings.setValue("pos", self.pos())
        self.worker.stop()
        QApplication.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    args = ap.parse_args()

    if not MODEL.exists():
        sys.exit(f"Missing model file: {MODEL}")
    try:
        injector = Injector()
    except PermissionError:
        sys.exit("No permission for /dev/uinput. Run install.sh (adds a udev rule) and log out/in.")

    app = QApplication(sys.argv)
    app.setApplicationName("hand-gestures")
    app.setStyleSheet(STYLE)
    worker = GestureWorker(args.camera, injector)
    win = ControlWindow(worker)
    win.show()
    worker.start()
    code = app.exec()
    worker.stop()
    injector.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
