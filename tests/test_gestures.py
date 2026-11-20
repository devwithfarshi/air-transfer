"""Tests for gestures.py and its JavaScript port in static/receiver.html.

Run from the project folder:   python -m unittest discover -s tests -v

hand_fixtures.json holds real MediaPipe output for MediaPipe's own sample
photos (a fist, a thumbs-up, pointing, a peace sign, two open hands), so the
classifier is checked against what the model actually produces.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gestures  # noqa: E402
from gestures import FIST, NONE, OPEN_PALM  # noqa: E402

FIXTURES = json.loads((ROOT / "tests" / "hand_fixtures.json").read_text())
RECEIVER_HTML = (ROOT / "static" / "receiver.html").read_text(encoding="utf-8")


class ClassifyTests(unittest.TestCase):
    def test_real_fist_is_fist(self):
        self.assertEqual(gestures.classify(FIXTURES["fist"]["world"]), FIST)
        self.assertEqual(gestures.classify(FIXTURES["fist_mirrored"]["world"]), FIST)

    def test_real_open_hands_are_open_palm(self):
        for hand in FIXTURES["two_open_hands"]:
            self.assertEqual(gestures.classify(hand["world"]), OPEN_PALM)

    def test_thumbs_up_is_not_a_fist(self):
        self.assertEqual(gestures.classify(FIXTURES["thumb_up"]["world"]), NONE)

    def test_pointing_and_peace_sign_are_none(self):
        self.assertEqual(gestures.classify(FIXTURES["pointing_up"]["world"]), NONE)
        self.assertEqual(gestures.classify(FIXTURES["victory"]["world"]), NONE)

    def test_hand_size_and_position_do_not_matter(self):
        # Same fist, 37x bigger and moved somewhere else: still a fist.
        moved = [(x * 37 + 5, y * 37 - 3, z * 37 + 2) for x, y, z in FIXTURES["fist"]["world"]]
        self.assertEqual(gestures.classify(moved), FIST)

    def test_missing_or_broken_input_is_none(self):
        self.assertEqual(gestures.classify(None), NONE)
        self.assertEqual(gestures.classify([]), NONE)
        self.assertEqual(gestures.classify(FIXTURES["fist"]["world"][:20]), NONE)
        self.assertEqual(gestures.classify([(0.0, 0.0, 0.0)] * 21), NONE)


class PrimaryHandTests(unittest.TestCase):
    def test_two_hands_picks_the_biggest(self):
        hands = [h["image"] for h in FIXTURES["two_open_hands"]]

        def area(hand):
            xs, ys = [p[0] for p in hand], [p[1] for p in hand]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))

        expected = 0 if area(hands[0]) > area(hands[1]) else 1
        self.assertEqual(gestures.pick_primary_hand(hands), expected)
        self.assertEqual(gestures.pick_primary_hand(list(reversed(hands))), 1 - expected)

    def test_no_hands(self):
        self.assertIsNone(gestures.pick_primary_hand([]))


class DebouncerTests(unittest.TestCase):
    def test_needs_n_frames_in_a_row(self):
        d = gestures.Debouncer(frames=5)
        for _ in range(4):
            self.assertEqual(d.update(FIST), NONE)
        self.assertEqual(d.update(FIST), FIST)

    def test_single_glitch_frames_are_ignored(self):
        d = gestures.Debouncer(frames=5)
        for _ in range(5):
            d.update(OPEN_PALM)
        for label in [NONE, OPEN_PALM, FIST, FIST, OPEN_PALM]:
            self.assertEqual(d.update(label), OPEN_PALM)


class SequenceTests(unittest.TestCase):
    def feed(self, detector, timeline):
        """timeline: list of (seconds, stable label). Returns times at which it fired."""
        return [t for t, label in timeline if detector.update(label, t)]

    def test_palm_then_fist_is_one_grab(self):
        grab = gestures.SequenceDetector(OPEN_PALM, FIST, window=2.0)
        fired = self.feed(grab, [(0.0, OPEN_PALM), (0.5, OPEN_PALM), (0.8, NONE),
                                 (1.2, FIST), (1.5, FIST), (3.0, FIST)])
        self.assertEqual(fired, [1.2])  # holding the fist does not fire again

    def test_can_grab_again_after_reopening(self):
        grab = gestures.SequenceDetector(OPEN_PALM, FIST, window=2.0)
        fired = self.feed(grab, [(0, OPEN_PALM), (1, FIST), (2, OPEN_PALM), (3, FIST)])
        self.assertEqual(fired, [1, 3])

    def test_too_slow_does_not_fire(self):
        grab = gestures.SequenceDetector(OPEN_PALM, FIST, window=2.0)
        fired = self.feed(grab, [(0.0, OPEN_PALM), (0.5, NONE), (3.0, NONE), (3.1, FIST)])
        self.assertEqual(fired, [])

    def test_wrong_order_does_not_fire(self):
        grab = gestures.SequenceDetector(OPEN_PALM, FIST, window=2.0)
        self.assertEqual(self.feed(grab, [(0, FIST), (1, OPEN_PALM)]), [])

    def test_drop_is_fist_then_palm(self):
        drop = gestures.SequenceDetector(FIST, OPEN_PALM, window=2.0)
        self.assertEqual(self.feed(drop, [(0, FIST), (0.4, NONE), (1.0, OPEN_PALM)]), [1.0])


# ---- The JavaScript port in static/receiver.html must match ----------------------

JS_BEGIN = "// --- BEGIN gestures port"
JS_END = "// --- END gestures port"


def js_port_source() -> str:
    start = RECEIVER_HTML.index(JS_BEGIN)
    end = RECEIVER_HTML.index(JS_END)
    return RECEIVER_HTML[start:end]


class JsPortTests(unittest.TestCase):
    def test_constants_match(self):
        js = js_port_source()
        for name in ["EXTENDED_MIN", "CURLED_MAX", "THUMB_TUCKED_MAX",
                     "STABLE_FRAMES", "SEQUENCE_WINDOW_S", "WRIST", "THUMB_TIP", "MIDDLE_MCP"]:
            match = re.search(rf"\b{name}\s*=\s*([0-9.]+)", js)
            self.assertIsNotNone(match, f"{name} missing from the JS port")
            self.assertEqual(float(match.group(1)), float(getattr(gestures, name)), name)
        fingers = re.search(r"FINGERS\s*=\s*(\[\[.*?\]\])", js).group(1)
        self.assertEqual(json.loads(fingers), [list(f) for f in gestures.FINGERS])

    @unittest.skipIf(shutil.which("node") is None, "Node.js not installed")
    def test_js_gives_the_same_answers(self):
        hands = {k: v["world"] for k, v in FIXTURES.items() if isinstance(v, dict) and "world" in v}
        for i, h in enumerate(FIXTURES["two_open_hands"]):
            hands[f"open_{i}"] = h["world"]
        two = [h["image"] for h in FIXTURES["two_open_hands"]]
        frames = [OPEN_PALM] * 6 + [NONE] * 2 + [FIST] * 7 + [OPEN_PALM] * 6 + [FIST] * 6
        times = [i * 0.05 for i in range(len(frames))]

        harness = js_port_source() + """
        const input = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
        const labels = {};
        for (const [k, w] of Object.entries(input.hands)) labels[k] = classify(w);
        const deb = new Debouncer(), grab = new SequenceDetector(OPEN_PALM, FIST);
        const fired = [];
        input.frames.forEach((f, i) => { if (grab.update(deb.update(f), input.times[i])) fired.push(i); });
        console.log(JSON.stringify({labels, primary: pickPrimaryHand(input.two), fired}));
        """
        with tempfile.TemporaryDirectory() as tmp:
            script, data = Path(tmp, "port.cjs"), Path(tmp, "input.json")
            script.write_text(harness, encoding="utf-8")
            data.write_text(json.dumps({"hands": hands, "two": two, "frames": frames, "times": times}))
            out = subprocess.run(["node", str(script), str(data)], capture_output=True, text=True, check=True)
        js = json.loads(out.stdout)

        py_labels = {k: gestures.classify(w) for k, w in hands.items()}
        deb, grab = gestures.Debouncer(), gestures.SequenceDetector(OPEN_PALM, FIST)
        py_fired = [i for i, f in enumerate(frames) if grab.update(deb.update(f), times[i])]

        self.assertEqual(js["labels"], py_labels)
        self.assertEqual(js["primary"], gestures.pick_primary_hand(two))
        self.assertEqual(js["fired"], py_fired)
        self.assertEqual(len(py_fired), 2)  # sanity: the sequence above contains two grabs


if __name__ == "__main__":
    unittest.main()
