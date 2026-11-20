"""Air Transfer server: WebSocket signalling + HTTP file hand-off for the gesture demo.

    python server.py                    # HTTPS if certs/cert.pem + certs/key.pem exist, else HTTP
    python server.py --port 8443
    python server.py --no-tls --public-url https://abc.trycloudflare.com   # behind a tunnel

There is one transfer at a time, kept in memory:

    IDLE --grab (laptop)--> HELD --POST /drop (phone)--> DELIVERING --POST /received--> IDLE
                              |                              |
                              +-- 30 s without a drop        +-- 2 min without a confirmation
                                  --> IDLE (released)            --> IDLE (gave up)

Endpoints
    GET  /           the phone page (static/receiver.html)
    WS   /ws         ?role=sender (laptop) or ?role=receiver (phone). The server pushes
                     {"type": "state", ...} on every change and {"type": "notice", ...}
                     for one-off events. The laptop sends {"type": "grab"}, or
                     {"type": "grab", "file": "<name in outbox>"} after taking a screenshot.
    POST /drop       the phone says "drop": HELD -> DELIVERING. Returns the file's URL.
    GET  /file/{id}  the dropped file, as a download. Can be retried while DELIVERING.
    POST /received   the phone has the whole file: DELIVERING -> IDLE.
    GET  /ca.pem     optional: certs/rootCA.pem, so the phone can install mkcert's CA.

Why the phone confirms with /received instead of the server noticing the download
finished: if the phone hangs up halfway, uvicorn quietly discards the rest of the
response, so from the server's side a cut-off download looks just like a complete one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import socket
import ssl
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
OUTBOX = HERE / "outbox"
RECEIVER_PAGE = HERE / "static" / "receiver.html"
CERT_DIR = HERE / "certs"
CA_FILE = CERT_DIR / "rootCA.pem"

IDLE, HELD, DELIVERING = "IDLE", "HELD", "DELIVERING"

# Files in the outbox a grab never picks: OS clutter and half-written downloads.
IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
IGNORED_SUFFIXES = {".tmp", ".part", ".crdownload", ".download"}


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def newest_outbox_file() -> Path | None:
    """The file a grab will take: the most recently modified one in ./outbox."""
    newest, newest_mtime = None, -1.0
    for path in OUTBOX.iterdir():
        name = path.name.lower()
        if name.startswith((".", "~$")) or name in IGNORED_NAMES or path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:  # deleted while we were looking
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


@dataclass
class Transfer:
    id: str           # random, so a URL can't be guessed
    path: Path
    name: str
    size: int
    deadline: float   # time.monotonic() after which we give up on it


class Hub:
    """All server state: who is connected, and the one transfer in flight."""

    def __init__(self):
        self.clients: dict[WebSocket, str] = {}   # socket -> "sender" or "receiver"
        self.phase = IDLE
        self.transfer: Transfer | None = None
        self.hold_seconds = 30.0      # HELD: how long to wait for a drop
        self.deliver_seconds = 120.0  # DELIVERING: how long the phone gets to download + confirm
                                      # (restarted by every download attempt)

    # ---- talking to the screens ------------------------------------------------

    def snapshot(self) -> dict:
        t = self.transfer
        roles = list(self.clients.values())
        upcoming = newest_outbox_file()
        return {
            "type": "state",
            "state": self.phase,
            "file": {"id": t.id, "name": t.name, "size": t.size} if t else None,
            # Seconds left rather than a timestamp, so the phone's clock doesn't matter.
            "expires_in": round(max(0.0, t.deadline - time.monotonic()), 1) if t else None,
            "hold_seconds": self.hold_seconds,
            "next_file": upcoming.name if upcoming else None,
            "senders": roles.count("sender"),
            "receivers": roles.count("receiver"),
        }

    async def send(self, ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_json(message)
        except Exception:
            self.clients.pop(ws, None)  # it went away; the /ws handler will tidy up

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.clients):
            await self.send(ws, message)

    async def broadcast_state(self) -> None:
        await self.broadcast(self.snapshot())

    async def notice(self, text: str, level: str = "info", to: WebSocket | None = None) -> None:
        """A one-off message for the screens ("Delivered x", "Outbox is empty"...)."""
        message = {"type": "notice", "level": level, "text": text}
        if to is None:
            await self.broadcast(message)
        else:
            await self.send(to, message)

    # ---- state changes ---------------------------------------------------------

    async def grab(self, ws: WebSocket, file_name: str | None = None) -> None:
        """Laptop gesture: hold a file (the one named, else the newest) and tell the phone."""
        if self.phase != IDLE:
            # A second grab while one is already in flight: ignore it, explain why.
            log(f"ignored a second grab: {self.transfer.name} is still {self.phase.lower()}")
            await self.notice(f"Already holding {self.transfer.name}. Drop it on the phone, "
                              f"or wait for it to be released.", "warn", to=ws)
            return

        if file_name:
            # sender.py names the screenshot it just saved. Only a bare file name is
            # accepted, so a grab can never reach outside ./outbox.
            path = OUTBOX / Path(str(file_name)).name
            if not path.is_file():
                await self.notice(f"{path.name} isn't in the outbox.", "warn", to=ws)
                return
        else:
            path = newest_outbox_file()
        if path is None:
            await self.notice("The outbox is empty. Put a file in ./outbox first.", "warn", to=ws)
            return
        try:
            size = path.stat().st_size
        except OSError:
            await self.notice(f"Couldn't read {path.name}.", "warn", to=ws)
            return

        self.transfer = Transfer(secrets.token_urlsafe(9), path, path.name, size,
                                 time.monotonic() + self.hold_seconds)
        self.phase = HELD
        log(f"grabbed {path.name} ({size:,} bytes), waiting {self.hold_seconds:g}s for a drop")
        if "receiver" not in self.clients.values():
            await self.notice("Grabbed, but no phone is connected yet. Open the URL on your phone.",
                              "warn", to=ws)
        await self.broadcast_state()

    async def drop(self, wanted_id: str | None) -> tuple[Transfer | None, str | None]:
        """Phone gesture: HELD -> DELIVERING. Returns (transfer, None) or (None, error)."""
        t = self.transfer
        if self.phase != HELD or t is None:
            return None, "Nothing is being held right now (it may have just expired)."
        if wanted_id and wanted_id != t.id:
            return None, "That file is no longer held."
        self.phase = DELIVERING
        t.deadline = time.monotonic() + self.deliver_seconds
        log(f"dropped {t.name}, phone is downloading it")
        await self.broadcast_state()
        return t, None

    async def received(self, transfer_id: str) -> bool:
        """The phone confirms it has the whole file: DELIVERING -> IDLE."""
        t = self.transfer
        if self.phase != DELIVERING or t is None or t.id != transfer_id:
            return False
        log(f"delivered {t.name}")
        self.phase, self.transfer = IDLE, None
        await self.notice(f"Delivered {t.name}", "ok")
        await self.broadcast_state()
        return True

    async def release(self, text: str) -> None:
        """Abandon the current transfer and go back to IDLE, telling everyone why."""
        log(text)
        self.phase, self.transfer = IDLE, None
        await self.notice(text, "warn")
        await self.broadcast_state()

    async def expire_if_due(self) -> None:
        t = self.transfer
        if t is None or time.monotonic() < t.deadline:
            return
        if self.phase == HELD:
            await self.release(f"Released {t.name}: no drop within {self.hold_seconds:g} s.")
        else:
            await self.release(f"Gave up on {t.name}: the phone never confirmed it got it.")


hub = Hub()


async def ticker() -> None:
    """Background loop: enforce the timeouts, and notice new files in the outbox."""
    last_upcoming = None
    while True:
        await asyncio.sleep(0.25)
        try:
            await hub.expire_if_due()
            upcoming = newest_outbox_file()
            name = upcoming.name if upcoming else None
            if name != last_upcoming:  # a file was added/removed: update "next file" on screen
                last_upcoming = name
                await hub.broadcast_state()
        except Exception as e:  # never let one bad tick kill the timeouts
            log(f"ticker error: {e!r}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    OUTBOX.mkdir(exist_ok=True)
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def phone_page(request: Request):
    # The page may only talk to this server and to the two places MediaPipe loads its
    # code and model from. Besides keeping things local, this stops MediaPipe's web
    # library from sending usage statistics to Google (odml.pa.googleapis.com), which
    # it does by default with no setting to turn it off.
    host = request.headers.get("host", "")
    allowed = f"ws://{host} wss://{host} " if re.fullmatch(r"[\w.:\[\]-]+", host) else ""
    policy = f"connect-src 'self' {allowed}https://cdn.jsdelivr.net https://storage.googleapis.com"
    return FileResponse(RECEIVER_PAGE, headers={"Cache-Control": "no-cache",
                                                "Content-Security-Policy": policy})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, role: str = "receiver"):
    await ws.accept()
    role = "sender" if role == "sender" else "receiver"
    hub.clients[ws] = role
    log(f"{role} connected from {ws.client.host if ws.client else '?'}")
    await hub.broadcast_state()  # the newcomer gets the state; everyone sees the new device count
    try:
        while True:
            try:
                message = json.loads(await ws.receive_text())
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("type") == "grab" and role == "sender":
                await hub.grab(ws, message.get("file"))
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(ws, None)
        log(f"{role} disconnected")
        if role == "receiver" and hub.phase != IDLE and "receiver" not in hub.clients.values():
            # Don't cancel: phones drop Wi-Fi or lock the screen all the time. If it comes
            # back before the timer runs out it gets the current state and can carry on.
            log(f"no phone connected while {hub.transfer.name} is {hub.phase.lower()}; "
                f"keeping it until the timer runs out")
        await hub.broadcast_state()


class TransferRequest(BaseModel):
    id: str | None = None  # the transfer the phone is talking about


@app.post("/drop")
async def drop(request: TransferRequest):
    transfer, error = await hub.drop(request.id)
    if error:
        return JSONResponse({"error": error}, status_code=409)
    return {"id": transfer.id, "name": transfer.name, "size": transfer.size,
            "url": f"/file/{transfer.id}"}


@app.get("/file/{file_id}")
async def get_file(file_id: str):
    t = hub.transfer
    if t is None or t.id != file_id:
        return JSONResponse({"error": "No such transfer (it may have expired)."}, status_code=404)
    if hub.phase != DELIVERING:
        return JSONResponse({"error": "Drop it first (POST /drop)."}, status_code=409)
    if not t.path.is_file():
        await hub.release(f"{t.name} disappeared from the outbox before it was delivered.")
        return JSONResponse({"error": f"{t.name} is no longer on the laptop."}, status_code=410)
    # We stay in DELIVERING until the phone confirms with POST /received, so if this
    # download gets cut off the phone can simply ask again. Each attempt restarts the clock.
    t.deadline = time.monotonic() + hub.deliver_seconds
    return FileResponse(t.path, filename=t.name, headers={"Cache-Control": "no-store"})


@app.post("/received")
async def received(request: TransferRequest):
    if not await hub.received(request.id):
        return JSONResponse({"error": "That transfer isn't being delivered any more."}, status_code=409)
    return {"ok": True}


@app.get("/ca.pem")
async def ca_certificate():
    """mkcert's *public* root certificate, if you copied it to certs/rootCA.pem."""
    if not CA_FILE.is_file():
        return JSONResponse({"error": "Copy mkcert's rootCA.pem into certs/ first (see README)."},
                            status_code=404)
    return FileResponse(CA_FILE, media_type="application/x-x509-ca-cert",
                        filename="mkcert-rootCA.crt")


# ---- Startup ------------------------------------------------------------------


def lan_addresses() -> list[str]:
    """This laptop's IPv4 address(es) on the local network, most likely first."""
    found = []
    try:
        # "Connecting" a UDP socket sends nothing. It just asks the OS which
        # interface it would use to reach the internet: normally the Wi-Fi/LAN one.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found or ["127.0.0.1"]


def names_in_certificate(cert_file: Path) -> set[str]:
    """Hostnames/IPs a certificate is valid for (empty set if we can't tell)."""
    try:
        info = ssl._ssl._test_decode_cert(str(cert_file))  # stdlib helper, no extra package
        return {value for _kind, value in info.get("subjectAltName", ())}
    except Exception:
        return set()


def print_qr(url: str, light_terminal: bool) -> None:
    try:
        import qrcode

        # The QR is drawn with block characters. Windows often hands Python a
        # non-UTF-8 stdout (Git Bash, IDE consoles, pipes), which can't print them.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        # On a dark terminal the blocks must be drawn for the *light* squares
        # (invert=True) for the code to come out dark-on-light, the way scanners expect.
        qr.print_ascii(invert=not light_terminal)
    except Exception as e:  # e.g. output redirected to a file that can't take block characters
        print(f"  (couldn't draw a QR code here: {e})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Air Transfer server")
    parser.add_argument("--host", default="0.0.0.0", help="interface to listen on (default: all)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cert", type=Path, default=CERT_DIR / "cert.pem")
    parser.add_argument("--key", type=Path, default=CERT_DIR / "key.pem")
    parser.add_argument("--no-tls", action="store_true",
                        help="serve plain HTTP even if certs exist (e.g. behind a tunnel)")
    parser.add_argument("--hold-seconds", type=float, default=30.0,
                        help="how long a grabbed file waits for a drop (default 30)")
    parser.add_argument("--public-url", help="address the phone should open instead of the LAN one, "
                                             "e.g. a tunnel URL")
    parser.add_argument("--light-terminal", action="store_true",
                        help="draw the QR code for a light-background terminal")
    args = parser.parse_args()

    hub.hold_seconds = args.hold_seconds
    OUTBOX.mkdir(exist_ok=True)
    use_tls = args.cert.is_file() and args.key.is_file() and not args.no_tls
    scheme = "https" if use_tls else "http"
    addresses = lan_addresses()
    phone_url = args.public_url or f"{scheme}://{addresses[0]}:{args.port}/"
    upcoming = newest_outbox_file()

    print()
    print("  Air Transfer server")
    print(f"  outbox : {OUTBOX}  (next file: {upcoming.name if upcoming else 'empty'})")
    print(f"  phone  : {phone_url}")
    for other in addresses[1:]:
        print(f"           {scheme}://{other}:{args.port}/   (other network adapter)")
    print(f"  laptop : python sender.py   (connects to {'wss' if use_tls else 'ws'}://127.0.0.1:{args.port}/ws)")
    print()

    if use_tls:
        covered = names_in_certificate(args.cert)
        if covered and addresses[0] not in covered and not args.public_url:
            print(f"  WARNING: {args.cert.name} is not valid for {addresses[0]} (it covers: "
                  f"{', '.join(sorted(covered))}).")
            print(f"  Your IP probably changed. Re-run:  mkcert -cert-file certs/cert.pem "
                  f"-key-file certs/key.pem {addresses[0]} localhost 127.0.0.1")
            print()
    elif not args.public_url:
        print("  WARNING: no certs/cert.pem + certs/key.pem, so this is plain HTTP.")
        print("  Phone browsers block the camera on plain HTTP. See README -> 'HTTPS with mkcert'.")
        print()

    print_qr(phone_url, args.light_terminal)
    print("  Scan with the phone's camera. Ctrl+C to stop.\n", flush=True)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=str(args.cert) if use_tls else None,
        ssl_keyfile=str(args.key) if use_tls else None,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
