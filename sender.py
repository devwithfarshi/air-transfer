"""Laptop side: watch the webcam; OPEN PALM then FIST within 2 s tells the server to grab.

    python sender.py                     # camera 0, server on this laptop
    python sender.py --screenshot        # each grab takes a screenshot and sends that
    python sender.py --camera 1          # a different webcam
    python sender.py --server ws://192.168.1.23:8000/ws

Keys in the preview window:  q or Esc = quit,  g = grab without gesturing (for testing),
s = switch screenshot mode on/off.

Screenshot mode saves the main screen to ./outbox/screenshot-<date>_<time>.png at the
moment of the grab and asks the server to hold exactly that file. It relies on
sender.py and server.py sharing the same project folder (and so the same outbox).
"""

from __future__ import annotations

import argparse
import json
import queue
import ssl
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision
from PIL import ImageGrab
from websockets.sync.client import connect

import gestures
from gestures import FIST, NONE, OPEN_PALM

HERE = Path(__file__).resolve().parent
OUTBOX = HERE / "outbox"  # shared with server.py
MODEL_PATH = HERE / "models" / "hand_landmarker.task"
# The same model the phone page loads.
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
WINDOW = "Air Transfer - laptop (q to quit)"

# BGR colours for OpenCV
WHITE, GREY, BLACK = (255, 255, 255), (150, 150, 150), (0, 0, 0)
GREEN, ORANGE, RED, BLUE = (94, 197, 34), (11, 158, 245), (68, 68, 239), (246, 130, 59)
GESTURE_COLOUR = {OPEN_PALM: GREEN, FIST: ORANGE, NONE: WHITE}


class ServerLink:
    """Keeps a WebSocket open to the server on a background thread.

    The main loop reads `.connected`, `.state` and `.notices`, and calls `.grab()`.
    Only this thread touches the socket, so there is no locking to think about.
    """

    def __init__(self, url: str):
        self.url = url
        self.connected = False
        self.state: dict = {}          # latest {"type": "state", ...} from the server
        self.state_at = 0.0            # time.monotonic() when it arrived (for the countdown)
        self.notices: list = []        # [(time.monotonic(), level, text)], newest last
        self._outgoing: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def grab(self, file_name: str | None = None) -> bool:
        """Ask the server to hold `file_name` from the outbox, or its newest file if None."""
        if not self.connected:
            return False
        message = {"type": "grab"}
        if file_name:
            message["file"] = file_name
        self._outgoing.put(message)
        return True

    def _ssl_context(self):
        if not self.url.startswith("wss://"):
            return None
        context = ssl.create_default_context()
        if urlparse(self.url).hostname in ("localhost", "127.0.0.1", "::1"):
            # We're talking to our own machine over loopback, where nobody can sit in
            # the middle, so skip the certificate check (no need to make Python trust mkcert).
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _run(self) -> None:
        retry_in = 0.5
        while True:
            try:
                with connect(self.url, ssl=self._ssl_context(), proxy=None, open_timeout=3) as ws:
                    while not self._outgoing.empty():  # forget grabs from before we (re)connected
                        self._outgoing.get_nowait()
                    self.connected, retry_in = True, 0.5
                    while True:
                        try:
                            self._handle(json.loads(ws.recv(timeout=0.05)))
                        except TimeoutError:
                            pass
                        while not self._outgoing.empty():
                            ws.send(json.dumps(self._outgoing.get_nowait()))
            except Exception:
                pass  # server not started yet, restarted, Wi-Fi blip... just try again
            self.connected = False
            time.sleep(retry_in)
            retry_in = min(retry_in * 2, 5.0)

    def _handle(self, message: dict) -> None:
        if message.get("type") == "state":
            self.state, self.state_at = message, time.monotonic()
        elif message.get("type") == "notice":
            self.notices = (self.notices + [(time.monotonic(), message["level"], message["text"])])[-5:]


def ensure_model() -> None:
    if MODEL_PATH.is_file():
        return
    MODEL_PATH.parent.mkdir(exist_ok=True)
    print(f"Downloading the hand-tracking model (~8 MB) to {MODEL_PATH} ...", flush=True)
    partial = MODEL_PATH.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, partial)
    partial.replace(MODEL_PATH)


def take_screenshot() -> Path:
    """Save the main screen into the outbox and return the new file's path."""
    OUTBOX.mkdir(exist_ok=True)
    path = OUTBOX / f"screenshot-{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    # compress_level=1: a slightly bigger file, but saved several times faster
    ImageGrab.grab().save(path, compress_level=1)
    return path


def hide_from_screenshots(window_title: str) -> None:
    """Keep our preview window out of screen captures; you still see it normally.

    Windows 10 (2004) and later only. Elsewhere the preview shows up in screenshots.
    """
    if sys.platform != "win32":
        return
    import ctypes

    WDA_EXCLUDEFROMCAPTURE = 0x11
    hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
    if hwnd:
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)


def grab(link: ServerLink, screenshot_mode: bool, how: str = "") -> tuple:
    """Run a grab (gesture or g key). Returns the flash to show: (until, text, colour)."""
    now = time.monotonic()
    if not link.connected:
        return now + 2, "server offline", RED
    # Only take a screenshot when the server can actually hold it; otherwise send a
    # plain grab, and the server explains why it can't ("Already holding ...").
    if screenshot_mode and link.state.get("state") == "IDLE":
        try:
            shot = take_screenshot()
        except Exception as e:
            print(f"Screenshot failed: {e}", flush=True)
            return now + 2, "screenshot failed", RED
        link.grab(shot.name)
        return now + 1.2, f"SCREENSHOT + GRAB!{how}", GREEN
    link.grab()
    return now + 1.2, f"GRAB!{how}", GREEN


def open_camera(index: int) -> cv2.VideoCapture:
    # On Windows, DirectShow opens the webcam in about a second; the default backend can take 10+.
    camera = cv2.VideoCapture(index, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(index)
    if not camera.isOpened():
        camera = cv2.VideoCapture(index)
    if not camera.isOpened():
        sys.exit(f"Couldn't open camera {index}. Close other apps using the webcam, or try --camera 1.")
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return camera


def label(img, text: str, x: int, y: int, colour=WHITE, scale: float = 0.55, bold: bool = False) -> int:
    """Text on a dark box, readable over any background. Returns the box's right edge."""
    text = text.encode("ascii", "replace").decode()  # OpenCV's fonts are ASCII-only
    thickness = 2 if bold else 1
    (w, h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    cv2.rectangle(img, (x - 6, y - h - 7), (x + w + 6, y + baseline + 3), BLACK, -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thickness, cv2.LINE_AA)
    return x + w + 6


def draw_hand(img, hand, colour, thickness: int) -> None:
    h, w = img.shape[:2]
    points = [(int(p[0] * w), int(p[1] * h)) for p in hand]
    for c in vision.HandLandmarksConnections.HAND_CONNECTIONS:
        cv2.line(img, points[c.start], points[c.end], colour, thickness, cv2.LINE_AA)
    for point in points:
        cv2.circle(img, point, thickness + 1, colour, -1, cv2.LINE_AA)


def draw_overlay(img, link: ServerLink, raw: str, stable: str, hand_count: int, ratios, flash,
                 screenshot_mode: bool) -> None:
    h, w = img.shape[:2]
    now = time.monotonic()

    # Row 1: connections
    x = label(img, "server: connected" if link.connected else "server: offline, retrying...",
              10, 24, GREEN if link.connected else RED)
    if link.connected:
        phones = link.state.get("receivers", 0)
        label(img, f"phone: {'connected' if phones else 'not connected'}", x + 12, 24,
              GREEN if phones else ORANGE)

    # Row 2: transfer state
    state = link.state.get("state") if link.connected else None
    file = link.state.get("file") or {}
    if state == "HELD":
        left = max(0.0, link.state["expires_in"] - (now - link.state_at))
        label(img, f"HOLDING {file.get('name')}  {left:4.1f}s", 10, 58, GREEN, 0.75, bold=True)
        label(img, "now make a fist, then open your palm at the phone", 10, 88, WHITE)
        bar = int((w - 20) * left / max(link.state.get("hold_seconds", 30), 1))
        cv2.rectangle(img, (10, 98), (10 + bar, 104), GREEN, -1)
    elif state == "DELIVERING":
        label(img, f"SENDING {file.get('name')} to the phone...", 10, 58, BLUE, 0.75, bold=True)
    elif state == "IDLE" and screenshot_mode:
        label(img, "IDLE  next grab: a screenshot of your screen", 10, 58)
    elif state == "IDLE":
        upcoming = link.state.get("next_file")
        label(img, f"IDLE  next file: {upcoming}" if upcoming else "IDLE  (outbox is empty)", 10, 58)

    # Latest notice from the server, for a few seconds
    if link.notices and now - link.notices[-1][0] < 4:
        _, level, text = link.notices[-1]
        label(img, text, 10, 132, {"ok": GREEN, "warn": ORANGE}.get(level, WHITE))

    # Big flash after our own grab gesture
    if flash and now < flash[0]:
        label(img, flash[1], w // 2 - 120, h // 2, flash[2], 1.1, bold=True)

    # Bottom: what the classifier sees
    gesture = f"gesture: {stable}" + (f"  (raw {raw})" if raw != stable else "")
    if hand_count > 1:
        gesture += "  - 2 hands, following the nearest"
    label(img, gesture, 10, h - 44, GESTURE_COLOUR[stable])
    if ratios:
        fingers, thumb = ratios
        label(img, "fingers " + " ".join(f"{r:.2f}" for r in fingers) + f"   thumb {thumb:.2f}",
              10, h - 16, GREY, 0.45)
    else:
        mode = "screenshot mode ON" if screenshot_mode else "screenshot mode off"
        label(img, f"palm then fist = grab   g = grab   s = {mode}   q = quit", 10, h - 16, GREY, 0.45)


def main() -> None:
    has_certs = (HERE / "certs" / "cert.pem").is_file()
    parser = argparse.ArgumentParser(description="Air Transfer laptop sender")
    parser.add_argument("--camera", type=int, default=0, help="webcam index (default 0)")
    parser.add_argument("--server", default=f"{'wss' if has_certs else 'ws'}://127.0.0.1:8000/ws",
                        help="server WebSocket URL (default: this laptop, matching server.py's https/http)")
    parser.add_argument("--screenshot", action="store_true",
                        help="each grab takes a screenshot and sends that (toggle with s)")
    args = parser.parse_args()
    screenshot_mode = args.screenshot
    server_url = args.server + ("&" if "?" in args.server else "?") + "role=sender"

    ensure_model()
    landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=2,  # see up to two, then follow the nearest (gestures.pick_primary_hand)
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.5,
    ))
    camera = open_camera(args.camera)
    link = ServerLink(server_url)
    debouncer = gestures.Debouncer()
    grab_detector = gestures.SequenceDetector(OPEN_PALM, FIST)
    flash = None          # (until, text, colour)
    last_timestamp = 0
    failed_reads = 0
    print(f"Watching camera {args.camera}; talking to {server_url}. Press q in the window to quit.")
    print(f"Screenshot mode {'ON' if screenshot_mode else 'off'} (press s in the window to switch).")

    cv2.namedWindow(WINDOW)
    window_hidden_from_captures = False
    while True:
        ok, frame = camera.read()
        if not ok:
            failed_reads += 1
            if failed_reads > 50:
                sys.exit("The webcam stopped sending frames.")
            time.sleep(0.02)
            continue
        failed_reads = 0

        frame = cv2.flip(frame, 1)  # mirror it, so it moves the way you do
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # MediaPipe's video mode needs strictly increasing millisecond timestamps.
        last_timestamp = max(last_timestamp + 1, int(time.monotonic() * 1000))
        result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                                             last_timestamp)

        hands_image = [[(p.x, p.y, p.z) for p in hand] for hand in result.hand_landmarks]
        hands_world = [[(p.x, p.y, p.z) for p in hand] for hand in result.hand_world_landmarks]
        primary = gestures.pick_primary_hand(hands_image)  # None when no hand is in frame
        raw = gestures.classify(hands_world[primary]) if primary is not None else NONE
        stable = debouncer.update(raw)
        ratios = None
        if primary is not None:
            ratios = (gestures.finger_ratios(hands_world[primary]), gestures.thumb_ratio(hands_world[primary]))

        if grab_detector.update(stable, time.monotonic()):
            flash = grab(link, screenshot_mode)

        for i, hand in enumerate(hands_image):
            if i == primary:
                draw_hand(frame, hand, GESTURE_COLOUR[stable], 3)
            else:
                draw_hand(frame, hand, GREY, 1)
        draw_overlay(frame, link, raw, stable, len(hands_image), ratios, flash, screenshot_mode)
        cv2.imshow(WINDOW, frame)
        if not window_hidden_from_captures:  # the window exists once it has shown a frame
            hide_from_screenshots(WINDOW)
            window_hidden_from_captures = True

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("g"):
            flash = grab(link, screenshot_mode, " (key)")
        if key == ord("s"):
            screenshot_mode = not screenshot_mode
            flash = (time.monotonic() + 1.5, f"screenshot mode {'ON' if screenshot_mode else 'off'}", BLUE)
        if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            break  # window closed with the X button

    camera.release()
    cv2.destroyAllWindows()
    landmarker.close()


if __name__ == "__main__":
    main()
