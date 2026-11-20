# Air Transfer

A local demo of "grab it on the laptop, drop it on the phone": move a file from
your laptop to your phone with two hand gestures.

```
 laptop webcam                                   phone front camera
 ┌───────────────┐                               ┌───────────────┐
 │  🖐  →  ✊     │  "grab"                        │  ✊  →  🖐     │  "drop"
 └──────┬────────┘                               └──────┬────────┘
        │ sender.py                                     │ receiver.html (in the phone browser)
        │   WebSocket {"type":"grab"}                   │   POST /drop, GET /file/{id}, POST /received
        ▼                                               ▼
   ┌───────────────────────── server.py (FastAPI on the laptop) ─────────────────────────┐
   │  IDLE ──grab──▶ HELD ──drop──▶ DELIVERING ──received──▶ IDLE                         │
   │                  └─ 30 s, no drop ─▶ IDLE                                           │
   └──────────────────────────────────────────────────────────────────────────────────────┘
```

Each device watches its own camera and classifies your hand as **OPEN_PALM**, **FIST**
or **NONE** from MediaPipe's 21 hand landmarks. The gestures are only triggers: the
file itself goes over plain HTTP(S) on your Wi-Fi. No database, no accounts, no
Bluetooth, no app to install.

## What you need

- A laptop with a webcam and **Python 3.11+** (tested on Windows 11 with Python 3.14).
- A phone on the **same Wi-Fi**, with Chrome (Android) or Safari (iOS).
- Internet access the first time: `pip install`, the laptop downloads the hand
  model (~8 MB) once, and the phone page loads MediaPipe from `cdn.jsdelivr.net`
  and the model from `storage.googleapis.com`.

## 1. Install

From the project folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

(macOS/Linux: `source .venv/bin/activate` instead of the second line. If PowerShell
refuses to run `Activate.ps1`, skip it and call `.venv\Scripts\python.exe` instead of
`python` in every command below.)

## 2. HTTPS with mkcert (one-time setup)

Phone browsers only allow camera access on `https://` pages (plain `http://` is fine
only for `localhost`). So the server needs a certificate the phone trusts.
[mkcert](https://github.com/FiloSottile/mkcert) makes one in a minute.

**a. Install mkcert and create your local certificate authority**

```powershell
winget install FiloSottile.mkcert
mkcert -install
```

(macOS: `brew install mkcert nss`. Linux: see the mkcert README.) Open a new terminal
after installing so `mkcert` is on your PATH.

**b. Find your laptop's LAN address.** Run `python server.py` once: it prints
`phone : http://192.168.x.y:8000/`. Stop it with Ctrl+C. (Or use `ipconfig`.)

**c. Make the certificate**, using *your* address in place of `192.168.1.23`:

```powershell
mkdir certs
mkcert -cert-file certs/cert.pem -key-file certs/key.pem 192.168.1.23 localhost 127.0.0.1
```

From now on `server.py` serves HTTPS automatically whenever `certs/cert.pem` and
`certs/key.pem` exist.

**d. Make the phone trust mkcert's authority.** Copy the authority's *public*
certificate next to yours, so the server can hand it to the phone:

```powershell
Copy-Item "$(mkcert -CAROOT)\rootCA.pem" certs\
```

(macOS/Linux: `cp "$(mkcert -CAROOT)/rootCA.pem" certs/`.) Copy only `rootCA.pem`.
**Never copy or share `rootCA-key.pem`**: anyone with it can make certificates all
your devices trust.

Start `python server.py`, then on the phone open `https://192.168.1.23:8000/ca.pem`
(your address). This one time the browser warns that the connection isn't private:
continue anyway (Android: *Advanced → Proceed*; iOS: *Show Details → visit this website*).

- **Android:** the file downloads as `mkcert-rootCA.crt`. Install it from
  *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*.
  The menu names vary by brand: search Settings for "CA certificate".
- **iOS:** allow the configuration-profile download, then *Settings → General →
  VPN & Device Management → mkcert … → Install*. Then turn on full trust in
  *Settings → General → About → Certificate Trust Settings*.

To undo later: remove the certificate in the phone's settings and run `mkcert -uninstall`
on the laptop.

**Shortcut that sometimes works:** skip step d and just tap through the certificate
warning on the phone. Android Chrome usually then allows the camera and the WebSocket.
iOS Safari often refuses the WebSocket on an untrusted certificate, so the page stays
"Offline". Installing the CA is the reliable route.

If your laptop's IP address changes later, `server.py` warns at startup and prints the
exact `mkcert` command to re-run.

### Fallback: a tunnel

If you can't install a certificate on the phone, let a tunnel provide HTTPS. Note
that the file then travels out through the tunnel provider's servers and back, so it
leaves your local network.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints an address like `https://something.trycloudflare.com`. In a second terminal:

```powershell
python server.py --no-tls --public-url https://something.trycloudflare.com
```

The QR code then points at the tunnel. `ngrok http 8000` works the same way.

## 3. Run it

**Terminal 1, the server:**

```powershell
python server.py
```

It prints the phone address and a QR code. Scan it with the phone's camera app
(if your terminal has a light background and the code won't scan, add `--light-terminal`).

**Terminal 2, the laptop camera:**

```powershell
python sender.py
```

A preview window opens with your hand skeleton, the detected gesture and the transfer
state. Use `--camera 1` if it picks the wrong webcam. In the window, `q` quits and `g`
grabs without gesturing (handy for testing).

**On the phone:** open the address and allow the camera. Prop the phone up with the
front camera facing you. Keep the screen on (the page asks the phone not to sleep).

**Transfer something:**

1. Put a file in `./outbox`, for example `Copy-Item C:\path\to\test.jpg outbox\`.
   A grab always takes the **newest** file in the folder. The file stays on the laptop;
   the phone gets a copy.
2. At the laptop: show an **open palm**, then close it into a **fist** within 2 s.
   The preview shows `GRAB!`, then `HOLDING test.jpg` with a 30 s countdown, and the
   phone turns green: **READY TO RECEIVE**.
3. Carry the fist over to the phone: hold the **fist** in front of its camera, then
   **open your palm** within 2 s.
4. The phone downloads the file and saves it. The laptop shows `Delivered test.jpg`.

Where the file ends up:

- **Android Chrome:** in *Downloads*. On the second automatic download Chrome may ask
  "allow multiple downloads?": allow it.
- **iOS Safari:** Safari asks to confirm the download; it goes to *Files → Downloads*.
  Or tap **Share…** on the page and choose *Save Image* to put a photo in Photos.

If the browser blocks the automatic save, tap **Save again** on the page.

### Send a screenshot instead

Start the laptop side in screenshot mode:

```powershell
python sender.py --screenshot
```

Now the grab gesture (open palm → fist) **takes a screenshot of your main screen at
that moment** and holds that image. Catch it on the phone exactly as before (fist →
open palm). The screenshot is saved as `outbox/screenshot-<date>_<time>.png`, so
there's a copy on the laptop too; delete old ones whenever you like.

- Press `s` in the preview window to switch screenshot mode on or off without
  restarting; the bottom line of the preview shows the current mode. `g` grabs in
  whichever mode is on.
- On Windows the preview window is left out of the screenshot (it stays visible to you),
  so it never covers what you're capturing. On macOS/Linux it will appear in the image.
- If a file is already being held, a grab doesn't take a new screenshot; the laptop
  shows "Already holding …" until the first one is caught or released.
- Only the main screen is captured. `sender.py` and `server.py` must run from the same
  folder, since the screenshot is handed over through `outbox/`.

## How the gestures are recognised

All the rules live in [gestures.py](gestures.py). The phone runs a line-for-line
JavaScript port of the same file, inside [static/receiver.html](static/receiver.html).

**One finger.** Compare how far the fingertip is from the wrist with how far that
finger's knuckle is from the wrist:

| finger state | tip-to-wrist ÷ knuckle-to-wrist |
|---|---|
| straight | about 1.4 – 2.0 → counts as **extended** if ≥ 1.35 |
| curled into the palm | about 0.7 – 1.05 → counts as **curled** if ≤ 1.15 |

Those ranges come from running MediaPipe on real photos (stored in
`tests/hand_fixtures.json`). Because it's a ratio on one hand, it doesn't depend on how
big your hand is or how far from the camera. It uses MediaPipe's 3-D *world*
landmarks, so tilting your hand towards the camera doesn't fool it.

**The whole hand.**

- **OPEN_PALM** = all four long fingers extended (the thumb is ignored).
- **FIST** = all four curled **and** the thumb tucked against the fingers, so a
  thumbs-up is *not* a fist.
- Anything else (pointing, a peace sign, a half-closed hand, no hand) = **NONE**.

**Over time.**

- A label only counts once it's been seen **5 frames in a row** (debounce).
- **Grab** = OPEN_PALM then FIST within 2 s. **Drop** = FIST then OPEN_PALM within 2 s.
  Each fires once on the change: holding the pose doesn't repeat it.
- **Two hands in view:** it follows the one that's biggest on screen, which is the
  nearest one, and draws the other in grey.

**Tips:** keep the whole hand in the frame, 30–80 cm from the camera, palm towards the
lens, in decent light. The laptop preview prints the live finger ratios at the bottom;
open the phone page with `?debug=1` on the end of the address to see them there too.
If you tune a threshold, change it in **both** `gestures.py` and `receiver.html`: the
tests fail if they differ.

## How the pieces talk

| Endpoint | Who | What |
|---|---|---|
| `GET /` | phone | the receiver page |
| `WS /ws?role=sender` | laptop | sends `{"type":"grab"}` (newest outbox file) or `{"type":"grab","file":"<name>"}` (that file, used for screenshots); receives state + notices |
| `WS /ws?role=receiver` | phone | receives state + notices |
| `POST /drop` `{"id"}` | phone | HELD → DELIVERING; returns `{"url": "/file/<id>"}` |
| `GET /file/{id}` | phone | the file, as a download (can be retried while DELIVERING) |
| `POST /received` `{"id"}` | phone | "I have the whole file": DELIVERING → IDLE |
| `GET /ca.pem` | phone | mkcert's public CA certificate, for setup |

The server pushes `{"type":"state", "state":"IDLE|HELD|DELIVERING", "file":…,
"expires_in":…, "next_file":…, "senders":n, "receivers":n}` on every change, and
`{"type":"notice", "level":…, "text":…}` for events like "Delivered test.jpg".

Why does the phone confirm with `/received`? If the phone hangs up halfway through a
download, the web server quietly drops the rest of the response, so from the
server's side a cut-off download looks the same as a finished one. Only the phone
knows it has every byte.

### Awkward situations

| Situation | What happens |
|---|---|
| No hand in frame | Reads NONE. A half-finished sequence expires after 2 s. |
| Two hands in frame | Follows the nearest (largest) hand; the screen says "2 hands". |
| Second grab while a file is held | Ignored; the laptop shows "Already holding test.jpg…". |
| Outbox empty | The laptop is told to put a file in `./outbox`. |
| No drop within 30 s | Released; both screens go back to IDLE and say why. |
| Phone disconnects while a file is held | The server keeps holding it until the 30 s runs out. The page reconnects by itself and can still catch it. |
| Phone disconnects mid-download | The server stays in DELIVERING. When the phone reconnects it downloads again, or just confirms if it already has the file. The server gives up after 2 min without a confirmation. |
| File deleted from the outbox before the download | The phone gets an error and both screens go back to IDLE. |
| Server restarted | Laptop and phone reconnect automatically. |

## Tests

```powershell
python -m unittest discover -s tests -v
```

These cover the classifier on real MediaPipe output (fist, open hands, thumbs-up,
pointing, peace sign), scale invariance, bad input, picking the nearest of two hands,
the debouncer, and the grab/drop sequences (fires once, too slow, wrong order). Two
more tests check that the JavaScript port in `receiver.html` uses the same numbers
and gives the same answers. The second one needs Node.js and is skipped without it.

## Privacy and security

- Camera frames never leave the device that captured them. Only the gesture events and
  the file go over the network, and only on your LAN (unless you use a tunnel).
- **MediaPipe reports usage statistics to Google by default** (which task ran, timings;
  not your camera images):
  - The phone page's library posts to `odml.pa.googleapis.com`. `server.py` serves the
    page with a Content-Security-Policy that only lets it talk to this server, the
    jsDelivr CDN and the model host, so those reports are blocked. You'll see one
    "violates the Content Security Policy" line in the browser console; that's it working.
  - The Python library (`sender.py`) posts to `https://play.googleapis.com/log`. There is
    no setting to turn it off. Hand tracking doesn't need it, so if you want the laptop
    fully quiet you can block that host, for example by adding
    `0.0.0.0 play.googleapis.com` to your hosts file (that also affects any other app
    that uses that address).
- This is a demo: anyone on your Wi-Fi who opens the page while a file is held can catch
  it. Don't run it on public or shared networks. That matters more in screenshot mode,
  because a screenshot shows whatever was on your screen at that moment.

## Troubleshooting

| Problem | Fix |
|---|---|
| Phone page says "Camera blocked: this page isn't HTTPS" | Set up mkcert (section 2) and use the `https://` address. |
| Phone can't open the address at all | Same Wi-Fi? Guest networks often block devices from seeing each other. On Windows, allow Python through the firewall on *Private* networks (the prompt appears on first start; if you dismissed it: *Windows Security → Firewall → Allow an app*). |
| QR code points at a `100.x` or `172.x` address | That's a VPN or virtual adapter. `server.py` lists the other addresses; use the `192.168.x.x` / `10.x.x.x` one. |
| "cert.pem is not valid for 192.168.…" at startup | Your IP changed; re-run the `mkcert` command it prints. |
| Phone stays "Offline, retrying…" on iOS | The certificate isn't trusted. Finish step 2d, including *Certificate Trust Settings*. |
| `Couldn't open camera 0` | Close Teams/Zoom/Camera, or try `python sender.py --camera 1`. |
| Gesture not recognised | Watch the finger ratios in the preview: open fingers should read ≥ 1.35, a fist ≤ 1.15. Get the whole hand in frame, with more light. |
| `ImportError: DLL load failed … The filename or extension is too long` | Windows path-length limit: move the project (and its `.venv`) to a short path such as `C:\air-transfer`. |
| `cv2` errors after installing other packages | Keep only `opencv-contrib-python`: `pip uninstall opencv-python`. |

## Files

| File | What it is |
|---|---|
| `server.py` | FastAPI app: WebSocket `/ws`, the phone page, `/drop`, `/file/{id}`, `/received`, the state machine and its timeouts, plus the LAN URL and QR code at startup |
| `sender.py` | Webcam loop, hand tracking, grab detection, screenshot mode and the preview window |
| `gestures.py` | Landmarks → OPEN_PALM / FIST / NONE, debouncer, sequence detector, nearest-hand picker |
| `static/receiver.html` | The whole phone page: camera, JS port of `gestures.py`, state display, drop and download |
| `tests/test_gestures.py` | Unit tests, including Python ↔ JavaScript parity |
| `tests/hand_fixtures.json` | Real MediaPipe landmarks for MediaPipe's sample hand photos |
| `outbox/` | Put files here to make them grabbable |
| `certs/`, `models/` | Created by you (mkcert) and by `sender.py` (hand model); git-ignored |
