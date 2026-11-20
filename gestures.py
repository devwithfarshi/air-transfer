"""Hand landmarks -> OPEN_PALM / FIST / NONE, plus debouncing and gesture sequences.

sender.py imports this file. static/receiver.html carries a line-for-line
JavaScript port of it (between the "BEGIN gestures port" / "END gestures port"
markers). tests/test_gestures.py fails if the two drift apart, so if you change
a number here, change it there too.

MediaPipe gives 21 landmarks per hand, numbered like this:

            8   12  16  20       fingertips (TIP)
            7   11  15  19       DIP joints
        4   6   10  14  18       PIP joints
        3   5   9   13  17       knuckles (MCP)
         2
          1
            0                    wrist

The rules:
  * A long finger is EXTENDED when its tip is much farther from the wrist than
    its knuckle is, and CURLED when the tip folds back to roughly the knuckle's
    distance from the wrist, or closer.
  * OPEN_PALM = all four long fingers extended. The thumb can do what it likes.
  * FIST      = all four long fingers curled AND the thumb tucked in against
    the fingers (otherwise a thumbs-up would count as a fist).
  * Anything else (pointing, peace sign, a half-closed hand, no hand) = NONE.

Landmarks are passed around as plain lists of (x, y, z) tuples.
"""

from __future__ import annotations

import math

OPEN_PALM = "OPEN_PALM"
FIST = "FIST"
NONE = "NONE"

WRIST = 0
THUMB_TIP = 4
MIDDLE_MCP = 9
# (knuckle, fingertip) landmark index pairs for the four long fingers.
FINGERS = (
    (5, 8),    # index
    (9, 12),   # middle
    (13, 16),  # ring
    (17, 20),  # pinky
)

# ---- Thresholds --------------------------------------------------------------
#
# finger ratio = distance(wrist, fingertip) / distance(wrist, knuckle)
#
# Straight finger: the tip sits a whole finger-length past the knuckle, so the
# ratio is about 1.5-2.0. Curled finger: the tip folds back down towards the
# palm, so the ratio drops to about 0.7-1.0. Measured on real MediaPipe output
# (world landmarks, see tests/hand_fixtures.json):
#     straight fingers   1.44 - 1.96
#     curled fingers     0.71 - 1.05
#
# Because it is a ratio of two distances on the same hand, it does not care how
# big the hand is or how far it is from the camera. Values between CURLED_MAX
# and EXTENDED_MIN are a dead zone: a half-bent finger counts as neither, so a
# hand caught halfway between poses reads as NONE rather than flickering.
EXTENDED_MIN = 1.35
CURLED_MAX = 1.15

# thumb ratio = distance(thumb tip, middle knuckle) / distance(wrist, middle knuckle)
#     thumb wrapped over a fist   0.27 - 0.46
#     thumbs-up                   0.90 - 0.96
THUMB_TUCKED_MAX = 0.75

# ---- Timing ------------------------------------------------------------------
STABLE_FRAMES = 5         # a raw label must repeat this many frames in a row to count
SEQUENCE_WINDOW_S = 2.0   # max seconds between the two poses of a grab or drop


def _dist(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def finger_ratios(world) -> list[float]:
    """Tip-to-wrist divided by knuckle-to-wrist, for index, middle, ring, pinky."""
    wrist = world[WRIST]
    ratios = []
    for knuckle, tip in FINGERS:
        palm = _dist(wrist, world[knuckle])
        ratios.append(_dist(wrist, world[tip]) / max(palm, 1e-9))
    return ratios


def thumb_ratio(world) -> float:
    """How far the thumb tip is from the middle knuckle, in palm lengths."""
    palm = _dist(world[WRIST], world[MIDDLE_MCP])
    return _dist(world[THUMB_TIP], world[MIDDLE_MCP]) / max(palm, 1e-9)


def classify(world) -> str:
    """Classify one hand from its 21 landmarks.

    Pass MediaPipe's *world* landmarks (3D, in metres). They keep their true
    shape when the hand tilts towards the camera, whereas 2D image landmarks
    would make an open hand pointed at the lens look like a fist.
    """
    if world is None or len(world) != 21:
        return NONE
    if _dist(world[WRIST], world[MIDDLE_MCP]) < 1e-6:
        return NONE  # degenerate input (all points on top of each other)

    ratios = finger_ratios(world)
    if all(r >= EXTENDED_MIN for r in ratios):
        return OPEN_PALM
    if all(r <= CURLED_MAX for r in ratios) and thumb_ratio(world) <= THUMB_TUCKED_MAX:
        return FIST
    return NONE


def pick_primary_hand(hands_image) -> int | None:
    """Which of several detected hands to follow. Returns an index, or None.

    We use the hand that covers the most of the frame, which is almost always
    the one closest to the camera: yours, not someone else's in the background.
    Takes *image* landmarks (x, y in 0..1 of the frame).
    """
    best_index, best_area = None, -1.0
    for i, hand in enumerate(hands_image):
        xs = [p[0] for p in hand]
        ys = [p[1] for p in hand]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if area > best_area:
            best_index, best_area = i, area
    return best_index


class Debouncer:
    """Only accept a new label after it has been seen STABLE_FRAMES frames in a row.

    One or two misclassified frames (motion blur, a finger briefly hidden) then
    can't flip the result.
    """

    def __init__(self, frames: int = STABLE_FRAMES):
        self.frames = frames
        self.candidate = NONE   # the label we are currently counting
        self.count = 0          # how many frames in a row we have seen it
        self.stable = NONE      # the last label that made it all the way

    def update(self, label: str) -> str:
        if label == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = label, 1
        if self.count >= self.frames:
            self.stable = self.candidate
        return self.stable


class SequenceDetector:
    """Fires once when the stable label goes `first` -> `second` within `window` seconds.

    Grab (laptop): SequenceDetector(OPEN_PALM, FIST)
    Drop (phone):  SequenceDetector(FIST, OPEN_PALM)

    The window runs from the last moment `first` was seen to the moment
    `second` appears, so a short NONE in between (hand in motion) is fine.
    It fires on the transition only: holding the second pose does not repeat it.
    """

    def __init__(self, first: str, second: str, window: float = SEQUENCE_WINDOW_S):
        self.first = first
        self.second = second
        self.window = window
        self.last_first_at = None   # when we last saw `first`
        self.previous = NONE        # stable label on the previous update

    def update(self, stable: str, now: float) -> bool:
        fired = False
        if stable == self.first:
            self.last_first_at = now
        elif stable == self.second and self.previous != self.second:
            if self.last_first_at is not None and now - self.last_first_at <= self.window:
                fired = True
            self.last_first_at = None  # the next fire needs a fresh `first`
        self.previous = stable
        return fired
