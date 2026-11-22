"""Degradation benchmark for the screen -> camera path.

Simulates a webcam/phone capture of a displayed frame (screen render ->
camera-resolution downscale -> slight perspective -> lighting gradient ->
gaussian blur -> sensor noise -> JPEG), then measures tile decode success and
bit error rate of the sampled (pre-CRC) matrix per (resolution, grid, tiles_n,
module_px). `recommend()` picks the densest config with >=95% tile success.

    python -m lightferry.bench [--quick] [--out out/bench.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

from . import codec, vision

RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "4k": (3840, 2160)}
SCREEN_H = 1080  # the displaying screen's height in px (frame must fit)


@dataclass(frozen=True)
class Degrade:
    blur: float = 0.9          # gaussian sigma, camera px
    persp: float = 0.025       # corner jitter, fraction of frame side
    rot: float = 3.0           # degrees of in-plane rotation (max abs)
    light: float = 0.3         # lighting gradient strength (0..1)
    jpeg: int = 80             # JPEG quality
    noise: float = 3.0         # gaussian sensor noise sigma (grey levels)
    frac: float = 0.7          # frame height / canvas height


MILD = Degrade(blur=0.6, persp=0.015, rot=2.0, light=0.2, jpeg=88, noise=1.5, frac=0.7)
DEFAULT = Degrade()


def degrade(frame: np.ndarray, res: tuple[int, int], d: Degrade = DEFAULT,
            rng: np.random.Generator | None = None) -> np.ndarray:
    """Displayed grayscale frame -> simulated camera image (BGR uint8, res=(W,H))."""
    rng = rng or np.random.default_rng(0)
    W, H = res
    side = int(d.frac * H)
    interp = cv2.INTER_AREA if side < frame.shape[0] else cv2.INTER_LINEAR
    f = cv2.resize(frame, (side, side), interpolation=interp)
    canvas = np.full((H, W), 70, np.uint8)  # dark desk / bezel scene
    ox, oy = (W - side) // 2, (H - side) // 2
    canvas[oy:oy + side, ox:ox + side] = f
    # perspective + rotation about frame centre
    c = np.array([W / 2, H / 2], np.float32)
    src = np.array([[ox, oy], [ox + side, oy], [ox + side, oy + side], [ox, oy + side]], np.float32)
    th = np.deg2rad(rng.uniform(-d.rot, d.rot))
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]], np.float32)
    dst = (src - c) @ R.T + c + rng.uniform(-d.persp, d.persp, (4, 2)).astype(np.float32) * side
    M = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
    img = cv2.warpPerspective(canvas, M, (W, H), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=70).astype(np.float32)
    # lighting gradient (random direction), multiplicative
    ang = rng.uniform(0, 2 * np.pi)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    g = ((xx - W / 2) * np.cos(ang) + (yy - H / 2) * np.sin(ang)) / max(W, H)
    img *= 1.0 + d.light * g * 1.6 - 0.0
    img *= 1.0 - 0.5 * d.light * 0.5
    if d.blur > 0:
        img = cv2.GaussianBlur(img, (0, 0), d.blur)
    if d.noise > 0:
        img += rng.normal(0, d.noise, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(d.jpeg)])
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _ber(sampled: np.ndarray, truth: np.ndarray) -> float:
    """Bit error rate vs truth, minimised over the 8 dihedral transforms (tile
    orientation in the frame is arbitrary)."""
    best = 1.0
    for t in vision._transforms(truth):
        best = min(best, float(np.mean(sampled != t)))
    return best


def run_trial(grid: int, tiles_n: int, module_px: int, res: tuple[int, int],
              d: Degrade, rng: np.random.Generator, frame_rot: int | None = None):
    """-> (tiles_ok, tiles_total, ber_sum, ber_n, detected)."""
    cap = codec.max_payload_bytes(grid)
    n = tiles_n * tiles_n
    payloads = [rng.bytes(cap) for _ in range(n)]
    frame, lay = vision.build_frame(payloads, grid, tiles_n, module_px)
    if frame_rot:
        frame = np.rot90(frame, frame_rot).copy()
    img = degrade(frame, res, d, rng)
    warped = vision.detect_frame(img)
    if warped is None:
        return 0, n, 0.0, 0, False
    got = set(vision.extract_tiles(warped, grid, tiles_n))
    ok = sum(p in got for p in payloads)
    truths = [codec.encode(p, grid) for p in payloads]
    # BER: tile i of the (possibly rotated) frame vs its own truth; frame rotation
    # permutes tile positions, so match against all truths and take the closest.
    bsum = 0.0
    for m in vision.sample_tile_matrices(warped, grid, tiles_n):
        bsum += min(_ber(m, t) for t in truths)
    return ok, n, bsum, n, True


def feasible(grid: int, tiles_n: int, module_px: int, res: tuple[int, int], frac: float = 0.7) -> bool:
    lay = vision.Layout(grid, tiles_n, module_px)
    return lay.size_px <= SCREEN_H


def sweep(resolutions, grids, tiles_list, mpx_list, trials=3, d: Degrade = DEFAULT, seed=1234, log=None):
    rows = []
    for rname in resolutions:
        res = RESOLUTIONS[rname]
        for grid in grids:
            for tn in tiles_list:
                for mpx in mpx_list:
                    if not feasible(grid, tn, mpx, res):
                        continue
                    lay = vision.Layout(grid, tn, mpx)
                    cam_ppm = d.frac * res[1] / lay.total
                    rng = np.random.default_rng(seed + grid * 31 + tn * 7 + mpx)
                    ok = tot = bn = det = 0
                    bs = 0.0
                    for _ in range(trials):
                        o, t, b, n, dt = run_trial(grid, tn, mpx, res, d, rng)
                        ok += o; tot += t; bs += b; bn += n; det += dt
                    row = dict(res=rname, grid=grid, tiles_n=tn, module_px=mpx,
                               cam_px_per_module=round(cam_ppm, 2),
                               payload_B=codec.max_payload_bytes(grid),
                               frame_B=codec.max_payload_bytes(grid) * tn * tn,
                               detect_rate=round(det / trials, 3),
                               tile_success=round(ok / tot, 3),
                               ber=round(bs / bn, 5) if bn else 1.0)
                    rows.append(row)
                    if log:
                        log(row)
    return rows


def recommend(res: str = "1080p", rows: list[dict] | None = None, min_success: float = 0.95,
              quick: bool = True) -> tuple[int, int, int] | None:
    """Best (grid, tiles_n, module_px) for the resolution: max bytes per frame with
    tile success >= min_success (ties -> larger module_px, i.e. more margin)."""
    if rows is None:
        rows = sweep([res], (32, 48, 64, 96, 128), (1, 2, 3, 4), (4, 6, 8) if quick else (3, 4, 5, 6, 8, 10),
                     trials=3 if quick else 6)
    good = [r for r in rows if r["res"] == res and r["tile_success"] >= min_success]
    if not good:
        return None
    best = max(good, key=lambda r: (r["frame_B"], r["module_px"]))
    return best["grid"], best["tiles_n"], best["module_px"]


def max_reliable_density(rows: list[dict], min_success: float = 0.95) -> dict:
    """Per resolution: smallest camera-px-per-module that still gave >= min_success."""
    out = {}
    for r in rows:
        if r["tile_success"] >= min_success:
            k = r["res"]
            out[k] = min(out.get(k, 1e9), r["cam_px_per_module"])
    return out


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def print_table(rows: list[dict]) -> None:
    cols = ["res", "grid", "tiles_n", "module_px", "cam_px_per_module", "frame_B", "detect_rate", "tile_success", "ber"]
    print("  ".join(f"{c:>9}" for c in cols))
    for r in rows:
        print("  ".join(f"{r[c]:>9}" for c in cols))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lightferry.bench")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="out/bench.csv")
    ap.add_argument("--trials", type=int, default=None)
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.quick:
        res = ["720p", "1080p"]
        grids, tiles, mpx, trials = (32, 64, 96), (1, 2, 3, 4), (4, 6), 2
    else:
        res = ["720p", "1080p", "4k"]
        grids, tiles, mpx, trials = (32, 48, 64, 96, 128), (1, 2, 3, 4), (3, 4, 5, 6, 8, 10), 5
    trials = a.trials or trials
    rows = sweep(res, grids, tiles, mpx, trials=trials)
    print_table(rows)
    write_csv(rows, a.out)
    print("\nmin camera px/module with >=95% tile success:", max_reliable_density(rows))
    for r in res:
        print(f"recommend({r}) = {recommend(r, rows)}  (grid, tiles_n, module_px)")
    print(f"\nwrote {a.out} ({len(rows)} rows) in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
