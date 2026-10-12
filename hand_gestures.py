#!/usr/bin/env python3
"""Hand-gesture control for Linux (Wayland/X11) and Windows using a webcam.

Gestures
  * 2 fingers (index + middle) up, move hand up/down  -> scroll
  * 3 fingers (index + middle + ring) swipe left/right -> previous / next tab
    (Ctrl+Shift+Tab / Ctrl+Tab, or Alt+Shift+Tab / Alt+Tab in "Windows" mode)
  * Open + close all 5 fingers twice                   -> stop / resume gestures
  * Pinch index finger and thumb together once         -> stop / resume gestures
  * Flash 4 fingers (thumb tucked) twice               -> minimize / expand the window

On Linux input is injected through /dev/uinput, so it works on KDE Wayland.
On Windows it uses SendInput.
"""
import argparse
import sys
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
SCROLL_COMMIT = 0.03          # fraction of frame height before a movement counts as a stroke
SCROLL_REVERSE = 0.02         # backwards travel that ends a stroke
SCROLL_ACCEL = 4.0            # bigger strokes scroll proportionally more (1 + ACCEL * stroke size)
SCROLL_SMOOTH = 0.5           # EMA factor for fingertip position
STILL_RANGE = 0.015           # movement range (frame height) that still counts as "still"
STILL_WINDOW = 0.2            # seconds used to judge stillness
SCROLL_STICKY = 0.5           # keep scrolling this long (s) if the 2-finger pose flickers
SWIPE_WINDOW = 0.45           # seconds of history used to detect a swipe
SWIPE_DISTANCE = 0.16         # fraction of frame width hand must travel
SWIPE_COOLDOWN = 0.8          # seconds between tab switches
TOGGLE_WINDOW = 2.5           # seconds to complete open-close-open-close (or 4-finger flashes)
THUMB_TUCKED = 0.65           # thumb tip within this many palm sizes of the palm centre = tucked
PINCH_DISTANCE = 0.25         # thumb-index tip gap (fraction of palm size) that counts as a pinch
PINCH_COOLDOWN = 0.6          # seconds before another pinch can toggle again


# --- input injection ------------------------------------------------------
class LinuxInjector:
    def __init__(self):
        from evdev import UInput, ecodes as e
        self.e = e
        self.keys = {"ctrl": e.KEY_LEFTCTRL, "shift": e.KEY_LEFTSHIFT,
                     "alt": e.KEY_LEFTALT, "tab": e.KEY_TAB}
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
        self.keys = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "tab": 0x09}
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


def classify(lm):
    palm = _d(lm[0], lm[9])
    # pinch: thumb and index tips touching, index reaching out (not curled into a fist)
    if _d3(lm[4], lm[8]) < PINCH_DISTANCE * palm and _d(lm[8], lm[0]) > 0.9 * palm:
        return "pinch"
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
    if i and m and not r and not p:
        return "two"
    return "other"


# --- scroll stroke tracking ------------------------------------------------
class ScrollTracker:
    """Turns fingertip height into scroll amounts, one stroke at a time.

    A stroke starts once the hand travels SCROLL_COMMIT in one direction (so a small
    wind-up doesn't set the direction) and ends when the hand reverses. The stroke
    right after a scroll stroke is the hand returning and is ignored. Holding still
    for `pause` seconds re-arms, so the next stroke scrolls in either direction.
    """

    def __init__(self):
        self.pause = 0.5
        self.reset()

    def reset(self):
        self.pos = None
        self.dir = 0                  # +1 up / -1 down while in a stroke
        self.role = None              # "scroll" or "return"
        self.start = self.extreme = 0.0
        self.lo = self.hi = 0.0
        self.expect_return = False
        self.hist = deque()
        self.still_since = None

    def update(self, t, y):
        """y: normalized fingertip height (0 = top). Returns scroll in frame heights, + = up."""
        up = -y
        self.pos = up if self.pos is None else SCROLL_SMOOTH * self.pos + (1 - SCROLL_SMOOTH) * up
        p = self.pos
        self.hist.append((t, p))
        while self.hist and t - self.hist[0][0] > STILL_WINDOW:
            self.hist.popleft()
        vals = [v for _, v in self.hist]
        if max(vals) - min(vals) < STILL_RANGE:
            self.still_since = self.still_since or t
        else:
            self.still_since = None
        rested = self.still_since is not None and t - self.still_since >= self.pause

        if self.dir == 0:
            if rested or not self.expect_return and self.still_since is not None:
                self.lo = self.hi = p   # forget drift while resting
                if rested:
                    self.expect_return = False
            self.lo, self.hi = min(self.lo, p), max(self.hi, p)
            if p - self.lo > SCROLL_COMMIT:
                return self._begin(+1, self.lo, p)
            if self.hi - p > SCROLL_COMMIT:
                return self._begin(-1, self.hi, p)
            return 0.0

        if rested:                    # paused mid-stroke: stroke is over, re-armed
            self.dir, self.expect_return = 0, False
            self.lo = self.hi = p
            return 0.0
        if (p - self.extreme) * self.dir > 0:
            return self._extend(p)
        if (self.extreme - p) * self.dir > SCROLL_REVERSE:
            # reversed: a scroll stroke is followed by an ignored return stroke
            nxt = "return" if self.role == "scroll" else "scroll"
            self.dir, self.start, self.role = -self.dir, self.extreme, nxt
            return self._extend(p)
        return 0.0

    def _begin(self, d, start, p):
        self.dir, self.start, self.extreme = d, start, start
        self.role = "return" if self.expect_return else "scroll"
        self.expect_return = False
        return self._extend(p)

    def _extend(self, p):
        delta = p - self.extreme
        self.extreme = p
        if self.role != "scroll":
            return 0.0
        return delta * (1 + SCROLL_ACCEL * abs(p - self.start))


# --- worker thread ----------------------------------------------------------
class GestureWorker(QThread):
    status = pyqtSignal(str)          # short status text
    event = pyqtSignal(str)           # last action performed
    paused_changed = pyqtSignal(bool)
    frame = pyqtSignal(QImage)
    minimize_toggled = pyqtSignal()   # 4 fingers flashed twice

    def __init__(self, camera, injector):
        super().__init__()
        self.camera = camera
        self.inj = injector
        self.paused = False
        self.running = True
        self.mode = "tabs"            # "tabs" or "windows"
        self.reverse_scroll = False
        self.scroll_speed = 6000      # hi-res units per full frame height
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
        if not cap.isOpened():
            self.status.emit(f"Cannot open camera {self.camera}")
            return
        landmarker = vision.HandLandmarker.create_from_options(
            vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(MODEL)),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=0.6,
                min_hand_presence_confidence=0.6,
                min_tracking_confidence=0.5,
            ))

        raw_pose, raw_count, pose = None, 0, None
        last_two = -1.0               # last time the 2-finger pose was seen
        swipe_hist = deque()
        last_swipe = 0.0
        toggle_events = deque()       # times of open->fist transitions
        four_events = deque()         # times the 4-finger pose appeared
        last_pinch = -PINCH_COOLDOWN
        prev_pose = None
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
                self.scroll.reset()
                swipe_hist.clear()
                self.status.emit("Paused · no hand" if self.paused else "No hand")
                self._emit_preview(rgb, None)
                continue

            lm = res.hand_landmarks[0]
            p = classify(lm)
            raw_count = raw_count + 1 if p == raw_pose else 1
            raw_pose = p
            if raw_count >= POSE_STABLE_FRAMES:
                pose = p

            # --- stop/resume: open-close twice -----------------------------
            if pose != prev_pose:
                if prev_pose == "open" and pose == "fist":
                    toggle_events.append(now)
                if pose == "four":
                    four_events.append(now)
                # --- stop/resume: pinch once ------------------------------
                if pose == "pinch" and now - last_pinch > PINCH_COOLDOWN:
                    last_pinch = now
                    self.set_paused(not self.paused)
                prev_pose = pose
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
            # Brief flickers to fist/other (e.g. bending the fingers) keep scrolling.
            if pose == "two" or (pose in ("fist", "other") and now - last_two < SCROLL_STICKY):
                if pose == "two":
                    last_two = now
                amount = self.scroll.update(now, (lm[8].y + lm[12].y) / 2)
                if amount:
                    if self.reverse_scroll:
                        amount = -amount
                    self.inj.scroll(amount * self.scroll_speed)
            else:
                self.scroll.reset()

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
        self.pause.setRange(0, 20)    # tenths of a second
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
        worker.minimize_toggled.connect(
            lambda: self.expand() if self.mini.isVisible() else self.minimize())
        self.mini = MiniBubble(self.expand)
        worker.frame.connect(lambda img: self.preview.setPixmap(QPixmap.fromImage(img)))

        s = self.settings
        self.mode.setCurrentIndex(int(s.value("mode", 0)))
        self.speed.setValue(int(s.value("speed", 6000)))
        self.pause.setValue(int(s.value("pause", 5)))
        self._pause_changed(self.pause.value())
        self.reverse.setChecked(s.value("reverse", "false") == "true")
        self.preview_cb.setChecked(s.value("preview", "false") == "true")
        self._mode_changed(self.mode.currentIndex())
        self._speed_changed(self.speed.value())
        self._paused_changed(False)
        self.setFixedWidth(250)
        if s.contains("pos"):
            self.move(s.value("pos", type=QPoint))

    # settings handlers
    def _mode_changed(self, i):
        self.worker.mode = "windows" if i == 1 else "tabs"
        self.settings.setValue("mode", i)

    def _speed_changed(self, v):
        self.worker.scroll_speed = v
        self.settings.setValue("speed", v)

    def _pause_changed(self, v):
        self.worker.scroll.pause = v / 10
        self.pause_label.setText(f"Pause {v / 10:.1f}s")
        self.settings.setValue("pause", v)

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
