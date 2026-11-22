"""Camera-side vision for Lightferry: frame layout, frame detection, tile extraction.

A displayed frame is a white card carrying a thick black square border; inside
the border sits an NxN arrangement of codec codes. Every code keeps its own
2-module quiet zone, so neighbouring codes are separated by 4 white modules and
a code is 2 white modules away from the border. Each packet carries its own seq,
so tile order / rotation / mirroring of the whole frame is irrelevant to the
protocol (codec.orient() fixes every tile independently).

Module units (m):
    cell      = grid + 2*QUIET            (one tile incl. its quiet zone)
    inner     = tiles_n * cell
    outer     = inner + 2*border          (black border square, outer edge)
    total     = outer + 2*margin          (white card)
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from . import codec

BORDER_MODULES = 4  # black border thickness, in modules (>= 3)
MARGIN_MODULES = 4  # white card margin outside the border, in modules
MAX_TILES_N = 4


@dataclass(frozen=True)
class Layout:
    grid: int
    tiles_n: int
    module_px: int
    border: int = BORDER_MODULES
    margin: int = MARGIN_MODULES
    quiet: int = codec.QUIET

    @property
    def cell(self) -> int:
        return self.grid + 2 * self.quiet

    @property
    def outer(self) -> int:  # border outer edge, modules
        return self.tiles_n * self.cell + 2 * self.border

    @property
    def total(self) -> int:
        return self.outer + 2 * self.margin

    @property
    def size_px(self) -> int:
        return self.total * self.module_px

    def tile_origin_px(self, idx: int) -> tuple[int, int]:
        """(x, y) pixel of the code's top-left module (quiet zone excluded)."""
        r, c = divmod(idx, self.tiles_n)
        mx = self.margin + self.border + c * self.cell + self.quiet
        my = self.margin + self.border + r * self.cell + self.quiet
        return mx * self.module_px, my * self.module_px


def build_frame(payloads: list[bytes], grid: int, tiles_n: int, module_px: int,
                border: int = BORDER_MODULES, margin: int = MARGIN_MODULES):
    """-> (grayscale uint8 frame image, Layout). Fewer payloads than tiles leaves
    the remaining cells blank white."""
    if not (1 <= tiles_n <= MAX_TILES_N):
        raise ValueError("tiles_n must be 1..4")
    if len(payloads) > tiles_n * tiles_n:
        raise ValueError("too many payloads for tiles_n")
    if border < 3:
        raise ValueError("border must be >= 3 modules")
    lay = Layout(grid, tiles_n, module_px, border, margin)
    mpx = module_px
    img = np.full((lay.size_px, lay.size_px), 255, np.uint8)
    b0 = lay.margin * mpx
    b1 = (lay.margin + lay.outer) * mpx
    img[b0:b1, b0:b1] = 0
    i0 = (lay.margin + lay.border) * mpx
    i1 = (lay.margin + lay.outer - lay.border) * mpx
    img[i0:i1, i0:i1] = 255
    for i, p in enumerate(payloads):
        tile = codec.render(codec.encode(p, grid), mpx, quiet=0)
        x, y = lay.tile_origin_px(i)
        img[y:y + tile.shape[0], x:x + tile.shape[1]] = tile
    return img, lay


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    return img


def _odd(n: int) -> int:
    n = int(n)
    return n if n % 2 else n + 1


def _masks(gray: np.ndarray):
    """Candidate foreground (black) masks, cheapest/most reliable first (lazy)."""
    h, w = gray.shape
    g = cv2.GaussianBlur(gray, (0, 0), 1.0)
    block = _odd(max(31, min(h, w) // 5))
    yield cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, block, 12)
    _, m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    yield m
    # divide by large blurred background (lighting gradient / glare), then Otsu
    # (blur a tiny copy: a sigma of max(h, w)/12 at full size is very slow)
    f = 64.0 / max(h, w)
    tiny = cv2.resize(gray, (max(2, int(w * f)), max(2, int(h * f))), interpolation=cv2.INTER_AREA)
    tiny = cv2.GaussianBlur(tiny, (0, 0), 64 / 12, borderType=cv2.BORDER_REPLICATE)
    bg = cv2.resize(tiny, (w, h), interpolation=cv2.INTER_LINEAR)
    norm = np.clip(g.astype(np.float32) / (bg.astype(np.float32) + 8) * 160, 0, 255).astype(np.uint8)
    _, m = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    yield m


def _is_convex_quad(q: np.ndarray) -> bool:
    return len(q) == 4 and cv2.isContourConvex(q.reshape(-1, 1, 2).astype(np.float32))


def _order(pts: np.ndarray) -> np.ndarray:
    """Order 4 points clockwise starting from top-left-ish (any consistent cycle works)."""
    c = pts.mean(0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(ang)]
    s = pts.sum(1)
    k = int(np.argmin(s))
    return np.roll(pts, -k, axis=0)


def _refine_corners(cnt: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Re-fit the 4 sides to the contour points (robust line fit) and intersect."""
    pts = cnt.reshape(-1, 2).astype(np.float32)
    lines = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        d = b - a
        L = float(np.hypot(*d))
        if L < 8:
            return quad
        u = d / L
        n = np.array([-u[1], u[0]], np.float32)
        rel = pts - a
        t = rel @ u
        dist = np.abs(rel @ n)
        sel = (t > 0.12 * L) & (t < 0.88 * L) & (dist < max(3.0, 0.03 * L))
        if sel.sum() < 6:
            return quad
        vx, vy, x0, y0 = cv2.fitLine(pts[sel], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0], np.float64), np.array([vx, vy], np.float64)))
    out = []
    for i in range(4):
        p1, d1 = lines[i - 1]
        p2, d2 = lines[i]
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-6:
            return quad
        t = np.linalg.solve(A, p2 - p1)
        out.append(p1 + t[0] * d1)
    out = np.array(out, np.float32)
    if np.abs(out - quad).max() > 0.08 * np.hypot(*(quad[2] - quad[0])):
        return quad
    return out


def _quad_ok(q: np.ndarray, shape) -> bool:
    sides = np.array([np.hypot(*(q[(i + 1) % 4] - q[i])) for i in range(4)])
    if sides.min() < 24 or sides.min() / sides.max() < 0.6:
        return False
    d = [np.hypot(*(q[2] - q[0])), np.hypot(*(q[3] - q[1]))]
    return 0.7 < d[0] / d[1] < 1.43


SEARCH_LONG_SIDE = 800   # contour search runs on a copy with long side <= this
MAX_CANDIDATES = 20      # connected components examined per mask
MIN_AREA_FRAC = 0.012    # min quad area / image area (frame >= ~25% of image height)


def _ring_candidates(mask: np.ndarray, shape):
    """Yield (area, contour, quad) of ring-like quads in the mask, innermost first."""
    h, w = shape
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n < 2:
        return
    min_area = MIN_AREA_FRAC * h * w
    bx, by, bw, bh, ar = (stats[:, i] for i in range(5))
    ok = (bw * bh >= min_area) & (bw >= 20) & (bh >= 20) & (ar >= 0.002 * h * w) \
        & ~((bw >= 0.98 * w) & (bh >= 0.98 * h))
    ok[0] = False
    ids = np.flatnonzero(ok)
    if len(ids) == 0:
        return
    ids = ids[np.argsort(-(bw[ids] * bh[ids]))[:MAX_CANDIDATES]]
    cands = []
    for lab in ids:
        x, y, cw, ch = int(bx[lab]), int(by[lab]), int(bw[lab]), int(bh[lab])
        sub = (labels[y:y + ch, x:x + cw] == lab).astype(np.uint8) * 255
        cnts, hier = cv2.findContours(sub, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE, offset=(x, y))
        if hier is None:
            continue
        hier = hier[0]
        for i, c in enumerate(cnts):
            if hier[i][3] != -1 or hier[i][2] == -1:  # outer contour that has a hole (ring)
                continue
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            hull = cv2.convexHull(c)
            peri = cv2.arcLength(hull, True)
            for eps in (0.02, 0.03, 0.045):
                q = cv2.approxPolyDP(hull, eps * peri, True).reshape(-1, 2).astype(np.float32)
                if len(q) == 4:
                    break
            if len(q) != 4 or not _quad_ok(q, shape):
                continue
            # ring check: the largest hole must itself be a big convex quad
            # (a thick-border frame; data blobs only have tiny holes)
            k, hole, best_a = hier[i][2], None, 0.0
            while k != -1:
                a2 = cv2.contourArea(cnts[k])
                if a2 > best_a:
                    best_a, hole = a2, cnts[k]
                k = hier[k][0]
            if hole is None or best_a < 0.45 * cv2.contourArea(q):
                continue
            hh = cv2.convexHull(hole)
            hq = cv2.approxPolyDP(hh, 0.04 * cv2.arcLength(hh, True), True)
            if len(hq) != 4:
                continue
            cands.append((cv2.contourArea(q), c, q))
    # a dark scene/bezel around the white card is also a ring: prefer the
    # innermost candidate (the frame's own border contains no other ring)
    cands.sort(key=lambda t: -t[0])
    for a, c, q in cands:
        if not any(a2 < a and cv2.pointPolygonTest(
                q.reshape(-1, 1, 2), (float(q2.mean(0)[0]), float(q2.mean(0)[1])), False) > 0
                for a2, _, q2 in cands):
            yield c, q


def _push_out(q: np.ndarray) -> np.ndarray:
    # contour points are black pixel centres: push the edge out half a pixel
    v = q - q.mean(0)
    return q + 0.5 * v / np.linalg.norm(v, axis=1, keepdims=True)


def _refine_fullres(gray: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Re-detect the border ring in a full-res ROI around a coarse quad (full-res px)
    and refine its corners; falls back to the coarse quad."""
    try:
        h, w = gray.shape
        side = float(np.sqrt(abs(cv2.contourArea(quad.reshape(-1, 1, 2)))))
        pad = int(0.06 * side) + 6
        x0, y0 = np.maximum(np.floor(quad.min(0)).astype(int) - pad, 0)
        x1, y1 = np.minimum(np.ceil(quad.max(0)).astype(int) + pad, [w, h])
        roi = gray[y0:y1, x0:x1]
        rh, rw = roi.shape
        g = cv2.GaussianBlur(roi, (0, 0), 1.0)
        block = _odd(max(31, min(rh, rw) // 5))
        mask = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, block, 12)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        if hier is None:
            return quad
        hier = hier[0]
        target = quad - np.array([x0, y0], np.float32)
        tarea = cv2.contourArea(target.reshape(-1, 1, 2))
        tc = target.mean(0)
        best = None
        for i, c in enumerate(cnts):
            if hier[i][3] != -1 or hier[i][2] == -1 or len(c) < 100:
                continue
            area = cv2.contourArea(c)
            if not (0.6 * tarea < area < 1.4 * tarea):
                continue
            hull = cv2.convexHull(c)
            peri = cv2.arcLength(hull, True)
            for eps in (0.02, 0.03, 0.045):
                q = cv2.approxPolyDP(hull, eps * peri, True).reshape(-1, 2).astype(np.float32)
                if len(q) == 4:
                    break
            if len(q) != 4 or not _quad_ok(q, roi.shape):
                continue
            d = float(np.abs(_order(q) - _order(target)).max())
            if d < 0.08 * side and (best is None or d < best[0]):
                best = (d, c, q)
        if best is None:
            return quad
        _, c, q = best
        q = _push_out(_refine_corners(c, _order(q)))
        return (q + np.array([x0, y0], np.float32)).astype(np.float32)
    except Exception:
        return quad


def _border_quads(img: np.ndarray):
    """Lazily yield candidate border quads (full-res px), best first."""
    gray = _to_gray(img)
    h0, w0 = gray.shape
    s = min(1.0, SEARCH_LONG_SIDE / max(1, max(h0, w0)))
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    h, w = small.shape
    if min(h, w) < 48:
        return
    seen = []
    for mask in _masks(small):
        for c, q in _ring_candidates(mask, small.shape):
            q0 = _order(q)
            if any(np.abs(q0 - p).max() < 0.05 * min(h, w) for p in seen):
                continue
            seen.append(q0)
            qs = _push_out(_refine_corners(c, q0)) / s
            if s < 1:
                qs = _refine_fullres(gray, qs.astype(np.float32))
            yield qs.astype(np.float32)


def find_border_quad(img: np.ndarray):
    """-> 4x2 float32 corners (full-res px, cyclic order) of the black border's outer
    edge, or None."""
    for q in _border_quads(img):
        return q
    return None


def warp_quad(img: np.ndarray, quad: np.ndarray, side: int | None = None) -> np.ndarray:
    gray = _to_gray(img)
    if side is None:
        side = int(np.mean([np.hypot(*(quad[(i + 1) % 4] - quad[i])) for i in range(4)]))
    side = int(np.clip(side, 64, 3000))
    dst = np.array([[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]], np.float32)
    M = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    return cv2.warpPerspective(gray, M, (side, side), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def detect_frame(img: np.ndarray) -> np.ndarray | None:
    """Camera image (BGR or gray) -> canonical square grayscale image of the black
    border's outer edge (corners at the image corners), or None."""
    try:
        for n, quad in enumerate(_border_quads(img)):
            if n >= 3:
                break
            warped = warp_quad(img, quad)
            # sanity: the outer ring must be dark, the interior mostly bright
            w = warped.shape[0]
            t = max(2, w // 40)
            ring = np.concatenate([warped[:t, :].ravel(), warped[-t:, :].ravel(),
                                   warped[:, :t].ravel(), warped[:, -t:].ravel()])
            mid = warped[w // 4: 3 * w // 4, w // 4: 3 * w // 4]
            if np.median(ring) > np.percentile(mid, 75) - 20:
                continue
            return warped
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------
# tile extraction
# --------------------------------------------------------------------------

def _transforms(m: np.ndarray):
    for flip in (False, True):
        base = m[:, ::-1] if flip else m
        for k in range(4):
            yield np.rot90(base, k)


def _tolerant_orient(m: np.ndarray, max_err: int = 6) -> np.ndarray | None:
    """Orient by the least finder-hamming transform and snap finders to the exact
    pattern (grey-tolerant: finder holes are the first thing blur destroys). The
    codec CRC still validates the result."""
    g = m.shape[0]
    best, best_e = None, 99
    for t in _transforms(m):
        e = 0
        for c, (ys, xs) in enumerate(codec._corner_slices(g)):
            e += int(np.count_nonzero((t[ys, xs] != 0) != (codec.finder_pattern(c) != 0)))
        if e < best_e:
            best, best_e = t, e
    if best is None or best_e > max_err:
        return None
    best = np.array(best, np.uint8)
    for c, (ys, xs) in enumerate(codec._corner_slices(g)):
        best[ys, xs] = codec.finder_pattern(c)
    return best


def sample_values(cell_img: np.ndarray, grid: int, ppm: float, quiet: int = codec.QUIET,
                  dx: float = 0.0, dy: float = 0.0, sigma: float = 0.3) -> np.ndarray:
    """Module-centre grey levels (grid x grid float32) from a tile cell image that
    includes its quiet zone, `ppm` pixels per module. dx/dy shift in modules."""
    g = cv2.GaussianBlur(cell_img.astype(np.float32), (0, 0), max(0.3, sigma * ppm))
    idx = (quiet + np.arange(grid) + 0.5) * ppm - 0.5
    xs = (idx + dx * ppm).astype(np.float32)
    ys = (idx + dy * ppm).astype(np.float32)
    mx, my = np.meshgrid(xs, ys)
    return cv2.remap(g, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def threshold_values(vals: np.ndarray, mode: str = "local") -> np.ndarray:
    """Grey module values -> bit matrix (1 = black)."""
    if mode == "local":
        # background = local mean (data is whitened ~50% black); sigma spans ~8 modules
        loc = cv2.GaussianBlur(vals, (0, 0), 8.0, borderType=cv2.BORDER_REFLECT)
        glob = 0.5 * (np.percentile(vals, 5) + np.percentile(vals, 95))
        thr = 0.6 * loc + 0.4 * glob
        return (vals < thr).astype(np.uint8)
    if mode.startswith("loc"):  # "loc3", "loc5": threshold against the plain local mean, sigma in modules
        loc = cv2.GaussianBlur(vals, (0, 0), float(mode[3:]), borderType=cv2.BORDER_REFLECT)
        return (vals < loc).astype(np.uint8)
    if mode == "iter":
        # start from the local threshold, then re-estimate the light and dark levels from the modules of each class
        # only (masked mean, sigma 2 modules) and threshold at their mid-point: handles a sharp lighting step
        m = threshold_values(vals, "local").astype(np.float32)
        for _ in range(3):
            dark, light = m, 1.0 - m
            den_l = np.maximum(cv2.GaussianBlur(light, (0, 0), 2.0, borderType=cv2.BORDER_REFLECT), 1e-3)
            den_d = np.maximum(cv2.GaussianBlur(dark, (0, 0), 2.0, borderType=cv2.BORDER_REFLECT), 1e-3)
            lv = cv2.GaussianBlur(vals * light, (0, 0), 2.0, borderType=cv2.BORDER_REFLECT) / den_l
            dv = cv2.GaussianBlur(vals * dark, (0, 0), 2.0, borderType=cv2.BORDER_REFLECT) / den_d
            m = (vals < 0.5 * (lv + dv)).astype(np.float32)
        return m.astype(np.uint8)
    if mode == "minmax":
        # edge-preserving: threshold at the mid-point of the local light and dark levels (7x7 modules), so a
        # sharp lighting step across the code (half grey, half white) does not shift the threshold
        kern = np.ones((7, 7), np.uint8)
        hi = cv2.GaussianBlur(cv2.dilate(vals, kern), (0, 0), 1.5, borderType=cv2.BORDER_REFLECT)
        lo = cv2.GaussianBlur(cv2.erode(vals, kern), (0, 0), 1.5, borderType=cv2.BORDER_REFLECT)
        return (vals < 0.5 * (hi + lo)).astype(np.uint8)
    v = np.clip(vals, 0, 255).astype(np.uint8)
    t, _ = cv2.threshold(v.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return (vals < t).astype(np.uint8)


def tile_cells(warped: np.ndarray, grid: int, tiles_n: int, border: int = BORDER_MODULES,
               quiet: int = codec.QUIET):
    """Yield (idx, cell_img, ppm): each tile's cell (code + quiet zone) cut from the
    warped frame, resampled so a module is an integer number of pixels."""
    cell = grid + 2 * quiet
    outer = tiles_n * cell + 2 * border
    w = warped.shape[0]
    ppm = int(np.clip(round(w / outer), 3, 8))
    side = outer * ppm
    interp = cv2.INTER_AREA if side < w else cv2.INTER_CUBIC
    big = cv2.resize(warped, (side, side), interpolation=interp)
    for i in range(tiles_n * tiles_n):
        r, c = divmod(i, tiles_n)
        x0 = (border + c * cell) * ppm
        y0 = (border + r * cell) * ppm
        yield i, big[y0:y0 + cell * ppm, x0:x0 + cell * ppm], ppm


_ATTEMPTS = (
    ("local", 0.0, 0.0, 0.3),
    # real webcams (soft, uneven light): the 8-module blend above fails; a tight local mean copes
    ("loc3", 0.0, 0.0, 0.3), ("loc5", 0.0, 0.0, 0.3),
    ("minmax", 0.0, 0.0, 0.3),
    ("iter", 0.0, 0.0, 0.3),
    ("otsu", 0.0, 0.0, 0.3),
    ("local", 0.0, 0.0, 0.5),
    ("local", -0.2, 0.0, 0.3), ("local", 0.2, 0.0, 0.3),
    ("local", 0.0, -0.2, 0.3), ("local", 0.0, 0.2, 0.3),
    ("local", -0.2, -0.2, 0.3), ("local", 0.2, 0.2, 0.3),
    ("local", -0.2, 0.2, 0.3), ("local", 0.2, -0.2, 0.3),
    ("otsu", -0.2, -0.2, 0.4), ("otsu", 0.2, 0.2, 0.4),
)


def decode_cell(cell_img: np.ndarray, grid: int, ppm: float) -> bytes | None:
    """Grey-tolerant tile decode: codec.decode first, then sampled-matrix attempts."""
    side = int(round((grid + 2 * codec.QUIET) * ppm))
    if cell_img.shape[0] != side:
        cell_img = cv2.resize(cell_img, (side, side), interpolation=cv2.INTER_AREA)
    p = codec.decode(cell_img, grid)
    if p is not None:
        return p
    for mode, dx, dy, sg in _ATTEMPTS:
        vals = sample_values(cell_img, grid, ppm, dx=dx, dy=dy, sigma=sg)
        m = threshold_values(vals, mode)
        p = codec.decode_matrix_any(m)
        if p is None:
            o = _tolerant_orient(m)
            p = None if o is None else codec.decode_matrix(o)
        if p is not None:
            return p
    return None


def extract_tiles(warped: np.ndarray, grid: int, tiles_n: int, border: int = BORDER_MODULES) -> list[bytes]:
    """Warped canonical frame -> verified payloads (CRC-good tiles only)."""
    out = []
    for _, cell_img, ppm in tile_cells(warped, grid, tiles_n, border):
        p = decode_cell(cell_img, grid, ppm)
        if p is not None:
            out.append(p)
    return out


def sample_tile_matrices(warped: np.ndarray, grid: int, tiles_n: int, border: int = BORDER_MODULES):
    """Primary sampled (unoriented, pre-CRC) bit matrix per tile, for BER measurement."""
    mats = []
    for _, cell_img, ppm in tile_cells(warped, grid, tiles_n, border):
        mats.append(threshold_values(sample_values(cell_img, grid, ppm), "local"))
    return mats


def read_frame(img: np.ndarray, grid: int, tiles_n: int) -> list[bytes]:
    w = detect_frame(img)
    return [] if w is None else extract_tiles(w, grid, tiles_n)
