"""Lightferry dense binary-matrix code (not QR).

Layout of an N x N module grid (1 = black, 0 = white):
  * 4 asymmetric 4x4 finder squares, one in each corner (v3). Corner 0 (top-left)
    is solid black; corners 1 (top-right), 2 (bottom-left), 3 (bottom-right)
    are black with 1, 2 and 3 white marks at interior cells (see
    finder_pattern). The four patterns differ, so all 8 dihedral transforms
    (4 rotations x mirror) of a code are distinguishable and orient() restores
    the upright matrix exactly, with no rotation search at the data level.
  * A 1-module white separator ring on the two inner sides of every finder
    (9 cells per corner, 36 total); reserved, never carries data.
  * Remaining cells carry, row-major: header (">BBHI": magic, version,
    payload length, CRC32) + payload, zero padded, XOR-whitened.
  * No error correction; reliability is the link layer's ack/retry job.
The image rendering adds a 2-module white quiet zone around the grid.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from functools import lru_cache

import cv2
import numpy as np

MAGIC = 0xB7
VERSION = 3  # v3 = asymmetric finders (exact orientation)
HEADER_FMT = ">BBHI"
HEADER_BYTES = struct.calcsize(HEADER_FMT)  # 8
FINDER = 4
RING_CELLS_PER_CORNER = 2 * FINDER + 1  # 9
QUIET = 2
GRIDS = (32, 64, 96, 128, 177)
_MIN_GRID = 16
_MAX_GRID = 700  # keeps payload length inside the uint16 header field


# Interior white marks (row, col) per corner: 0=TL, 1=TR, 2=BL, 3=BR.
# 12+/16 modules stay black in every finder (BR has 13).
_FINDER_WHITE = {
    0: (),
    1: ((1, 1),),
    2: ((1, 1), (1, 2)),
    3: ((1, 1), (1, 2), (2, 1)),
}
_CORNERS = ("TL", "TR", "BL", "BR")


def finder_pattern(corner: int) -> np.ndarray:
    """4x4 uint8 finder block (1 = black) for corner 0=TL, 1=TR, 2=BL, 3=BR."""
    if corner not in _FINDER_WHITE:
        raise ValueError(f"corner {corner} not in 0..3")
    p = np.ones((FINDER, FINDER), dtype=np.uint8)
    for y, x in _FINDER_WHITE[corner]:
        p[y, x] = 0
    p.flags.writeable = False
    return p


def _corner_slices(grid: int):
    f = FINDER
    lo, hi = slice(0, f), slice(grid - f, grid)
    return ((lo, lo), (lo, hi), (hi, lo), (hi, hi))


def finder_signature(code: np.ndarray) -> tuple:
    """The four corner blocks of a matrix (TL, TR, BL, BR) as bytes."""
    grid = code.shape[0]
    return tuple((code[ys, xs] != 0).astype(np.uint8).tobytes() for ys, xs in _corner_slices(grid))


def _finders_ok(code: np.ndarray) -> bool:
    grid = code.shape[0]
    for c, (ys, xs) in enumerate(_corner_slices(grid)):
        if not np.array_equal(code[ys, xs] != 0, finder_pattern(c) != 0):
            return False
    return True


def _transforms(code: np.ndarray):
    """All 8 dihedral transforms of a square matrix."""
    for flip in (False, True):
        base = code[:, ::-1] if flip else code
        for k in range(4):
            yield np.rot90(base, k)


def orient(matrix: np.ndarray) -> np.ndarray | None:
    """Undo any of the 8 dihedral transforms by matching the corner finder
    signatures; returns the upright (contiguous) matrix, or None."""
    try:
        m = np.asarray(matrix)
        grid = m.shape[0]
        if m.ndim != 2 or m.shape != (grid, grid) or not (_MIN_GRID <= grid <= _MAX_GRID):
            return None
        for t in _transforms(m):
            if _finders_ok(t):
                return np.ascontiguousarray(t)
        return None
    except Exception:
        return None


def _check_grid(grid: int) -> None:
    if not (_MIN_GRID <= grid <= _MAX_GRID):
        raise ValueError(f"grid {grid} outside [{_MIN_GRID}, {_MAX_GRID}]")


@lru_cache(maxsize=None)
def reserved_mask(grid: int) -> np.ndarray:
    """Boolean mask of cells not available for data (finders + rings)."""
    _check_grid(grid)
    m = np.zeros((grid, grid), dtype=bool)
    r = FINDER + 1
    m[:r, :r] = True
    m[:r, grid - r:] = True
    m[grid - r:, :r] = True
    m[grid - r:, grid - r:] = True
    m.flags.writeable = False
    # the ring covers one extra row/col only on the inner sides, which the
    # (FINDER+1)-sized corner blocks above already include.
    return m


@lru_cache(maxsize=None)
def finder_mask(grid: int) -> np.ndarray:
    _check_grid(grid)
    m = np.zeros((grid, grid), dtype=bool)
    f = FINDER
    m[:f, :f] = True
    m[:f, grid - f:] = True
    m[grid - f:, :f] = True
    m[grid - f:, grid - f:] = True
    m.flags.writeable = False
    return m


def data_cells(grid: int) -> int:
    return int((~reserved_mask(grid)).sum())


def capacity_bytes(grid: int) -> int:
    """Raw bytes (header + payload) that fit in the grid."""
    return data_cells(grid) // 8


def max_payload_bytes(grid: int) -> int:
    return capacity_bytes(grid) - HEADER_BYTES


@lru_cache(maxsize=None)
def _keystream(n: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(b"lightferry-whiten-v2" + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    return bytes(out[:n])


def _whiten(buf: bytes) -> bytes:
    ks = np.frombuffer(_keystream(len(buf)), dtype=np.uint8)
    return (np.frombuffer(buf, dtype=np.uint8) ^ ks).tobytes()


def encode(payload: bytes, grid: int) -> np.ndarray:
    """Payload -> (grid, grid) uint8 matrix, 1 = black."""
    _check_grid(grid)
    if len(payload) > max_payload_bytes(grid):
        raise ValueError(
            f"payload {len(payload)}B exceeds capacity {max_payload_bytes(grid)}B for grid {grid}"
        )
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    cap = capacity_bytes(grid)
    raw = (header + payload).ljust(cap, b"\x00")
    bits = np.unpackbits(np.frombuffer(_whiten(raw), dtype=np.uint8))
    cells = np.zeros(data_cells(grid), dtype=np.uint8)
    cells[: bits.size] = bits
    code = np.zeros((grid, grid), dtype=np.uint8)
    code[~reserved_mask(grid)] = cells  # boolean assignment is row-major
    for c, (ys, xs) in enumerate(_corner_slices(grid)):
        code[ys, xs] = finder_pattern(c)
    return code


def decode_matrix(code: np.ndarray) -> bytes | None:
    """Strict: upright (grid, grid) matrix (1 = black) -> payload, or None.
    Rotated/mirrored input fails the finder check; use decode_matrix_any."""
    try:
        grid = code.shape[0]
        if code.ndim != 2 or code.shape != (grid, grid) or not (_MIN_GRID <= grid <= _MAX_GRID):
            return None
        if not _finders_ok(code):
            return None
        cells = (code[~reserved_mask(grid)] != 0).astype(np.uint8)
        nbytes = cells.size // 8
        raw = _whiten(np.packbits(cells[: nbytes * 8]).tobytes())
        magic, version, length, crc = struct.unpack(HEADER_FMT, raw[:HEADER_BYTES])
        if magic != MAGIC or version != VERSION:
            return None
        if length > nbytes - HEADER_BYTES:
            return None
        payload = raw[HEADER_BYTES : HEADER_BYTES + length]
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            return None
        return payload
    except Exception:
        return None


def decode_matrix_any(code: np.ndarray) -> bytes | None:
    """Like decode_matrix but accepts any of the 8 rotations/mirrors."""
    up = orient(code)
    return None if up is None else decode_matrix(up)


def render(code: np.ndarray, module_px: int = 8, quiet: int = QUIET) -> np.ndarray:
    """Matrix -> grayscale image (uint8, black 0 / white 255), nearest upscale."""
    img = np.where(code != 0, 0, 255).astype(np.uint8)
    img = np.pad(img, quiet, constant_values=255)
    return np.kron(img, np.ones((module_px, module_px), dtype=np.uint8))


def image_to_matrix(img: np.ndarray, grid: int, quiet: int = QUIET) -> np.ndarray:
    """Clean-case sampling: resize to grid+2*quiet (INTER_AREA), Otsu, crop quiet zone."""
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    side = grid + 2 * quiet
    small = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return (bw[quiet : quiet + grid, quiet : quiet + grid] == 0).astype(np.uint8)


def decode(img: np.ndarray, grid: int, quiet: int = QUIET) -> bytes | None:
    """Captured, already-cropped code image -> payload or None."""
    try:
        return decode_matrix_any(image_to_matrix(img, grid, quiet))
    except Exception:
        return None
