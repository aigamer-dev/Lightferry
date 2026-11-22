"""Always-on-top viewer for the phone TEST setup (runs on the sender machine).

Shows the link QR first (to open the phone page), then switches to the transfer
frame once the phone has pressed Start. Polls the local mobile server.

    python -m lightferry.viewer --token TOKEN [--port 8443] [--qr out/phone-qr.png]
"""
from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.request

import cv2
import numpy as np

WIN = "Lightferry viewer"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _get(url: str, timeout: float = 2.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_CTX) as r:
            return r.read()
    except Exception:
        return None


def _decode(png: bytes | None):
    if not png:
        return None
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    return img


def _fit(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Letterbox `img` into a black w x h canvas, nearest-neighbour (crisp modules)."""
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    out = np.zeros((h, w), np.uint8)
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_NEAREST)
    y, x = (h - nh) // 2, (w - nw) // 2
    out[y : y + nh, x : x + nw] = r
    return out


def _caption(canvas: np.ndarray, text: str) -> None:
    cv2.putText(canvas, text, (20, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 255, 2, cv2.LINE_AA)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--qr", default="out/phone-qr.png")
    ap.add_argument("--no-fullscreen", action="store_true")
    a = ap.parse_args(argv)

    base = f"https://{a.host}:{a.port}"
    qr = cv2.imread(a.qr, cv2.IMREAD_GRAYSCALE)
    if qr is None:
        print("cannot read", a.qr)
        return 1
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    try:
        cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    if not a.no_fullscreen:
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    state, last_poll, frame_v, frame = "IDLE", 0.0, -1, None
    while True:
        now = time.monotonic()
        if now - last_poll > 0.15:
            last_poll = now
            raw = _get(f"{base}/status.json?t={a.token}")
            if raw:
                try:
                    st = json.loads(raw)
                    state = st.get("state", "IDLE")
                    if state == "RUNNING" and st.get("frame_v") != frame_v:
                        frame_v = st.get("frame_v")
                        frame = _decode(_get(f"{base}/frame.png?t={a.token}")) or frame
                except Exception:
                    pass
        try:
            _, _, w, h = cv2.getWindowImageRect(WIN)
        except Exception:
            w, h = 1280, 720
        w, h = max(w, 320), max(h, 240)
        if state == "RUNNING" and frame is not None:
            canvas = _fit(frame, w, h)
        else:
            canvas = np.full((h, w), 255, np.uint8)
            q = _fit(qr, min(w, h) - 140, min(w, h) - 140)
            y, x = (h - q.shape[0]) // 2 + 30, (w - q.shape[1]) // 2
            canvas[y : y + q.shape[0], x : x + q.shape[1]] = q
            canvas[:70, :] = 0
            _caption(canvas, "1) Scan with phone (same Wi-Fi)  2) press Start on phone   [q = quit, f = fullscreen]")
        cv2.imshow(WIN, canvas)
        k = cv2.waitKey(30) & 0xFF
        if k in (ord("q"), 27):
            break
        if k == ord("f"):
            fs = cv2.getWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN)
            cv2.setWindowProperty(
                WIN, cv2.WND_PROP_FULLSCREEN,
                cv2.WINDOW_NORMAL if fs == cv2.WINDOW_FULLSCREEN else cv2.WINDOW_FULLSCREEN)
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
