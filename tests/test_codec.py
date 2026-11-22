import os

import numpy as np
import pytest

from lightferry import codec

GRIDS = codec.GRIDS


@pytest.mark.parametrize("grid", GRIDS)
@pytest.mark.parametrize("module_px", [4, 6, 8])
def test_roundtrip_max_capacity(grid, module_px):
    for _ in range(5):
        payload = os.urandom(codec.max_payload_bytes(grid))
        code = codec.encode(payload, grid)
        img = codec.render(code, module_px)
        assert img.shape == ((grid + 2 * codec.QUIET) * module_px,) * 2
        assert codec.decode(img, grid) == payload


@pytest.mark.parametrize("grid", GRIDS)
@pytest.mark.parametrize("n", [0, 1, 7, 50])
def test_roundtrip_short_payloads(grid, n):
    payload = os.urandom(n)
    assert codec.decode(codec.render(codec.encode(payload, grid), 6), grid) == payload


def test_capacity_formula():
    # data cells = grid^2 - 4*(4x4 finder) - 4*(9-cell ring) ; minus 8-byte header
    for g in GRIDS:
        assert codec.max_payload_bytes(g) == ((g * g - 64 - 36) // 8) - 8
    assert codec.max_payload_bytes(64) == 491
    assert codec.max_payload_bytes(128) == 2027
    assert codec.max_payload_bytes(177) == 3895


def test_oversize_rejected():
    with pytest.raises(ValueError):
        codec.encode(b"x" * (codec.max_payload_bytes(32) + 1), 32)


def test_finders_asymmetric_ring_white_and_whitened():
    g = 64
    code = codec.encode(b"\x00" * 100, g)  # all-zero payload must not give long white runs
    f = codec.FINDER
    for c, (ys, xs) in enumerate([(slice(0, f), slice(0, f)), (slice(0, f), slice(g - f, g)),
                                  (slice(g - f, g), slice(0, f)), (slice(g - f, g), slice(g - f, g))]):
        assert np.array_equal(code[ys, xs], codec.finder_pattern(c))
        assert code[ys, xs].sum() >= 12
    assert code[:f, :f].all()
    ring = codec.reserved_mask(g) & ~codec.finder_mask(g)
    assert ring.sum() == 36 and not code[ring].any()
    data = code[~codec.reserved_mask(g)]
    assert 0.4 < data.mean() < 0.6


@pytest.mark.parametrize("grid", GRIDS)
def test_corruption_detected(grid):
    payload = os.urandom(codec.max_payload_bytes(grid))
    code = codec.encode(payload, grid)
    free = np.argwhere(~codec.reserved_mask(grid))
    used = (codec.HEADER_BYTES + len(payload)) * 8  # cells actually carrying bits
    rng = np.random.default_rng(1)
    for i in rng.choice(used, size=min(200, used), replace=False):
        bad = code.copy()
        y, x = free[i]
        bad[y, x] ^= 1
        assert codec.decode_matrix(bad) is None  # every used-cell flip is rejected


def _all_transforms(code):
    out = []
    for flip in (False, True):
        base = code[:, ::-1] if flip else code
        for k in range(4):
            out.append(np.ascontiguousarray(np.rot90(base, k)))
    return out


def test_version_is_3():
    assert codec.VERSION == 3


def test_finder_signatures_pairwise_distinct():
    code = codec.encode(b"sig", 64)
    sigs = [codec.finder_signature(t) for t in _all_transforms(code)]
    assert len(set(sigs)) == 8
    pats = {codec.finder_pattern(c).tobytes() for c in range(4)}
    assert len(pats) == 4
    assert codec.finder_pattern(0).all()


@pytest.mark.parametrize("grid", GRIDS)
def test_all_8_transforms_oriented(grid):
    for _ in range(3):
        payload = os.urandom(codec.max_payload_bytes(grid))
        code = codec.encode(payload, grid)
        for i, t in enumerate(_all_transforms(code)):
            assert codec.decode_matrix_any(t) == payload
            assert np.array_equal(codec.orient(t), code)
            if i == 0:
                assert codec.decode_matrix(t) == payload
            else:
                assert codec.decode_matrix(t) is None


def test_orient_rejects_garbage():
    assert codec.orient(np.zeros((64, 64), np.uint8)) is None
    assert codec.orient(np.zeros((3, 5), np.uint8)) is None


@pytest.mark.parametrize("grid", [32, 96])
def test_rotated_mirrored_images_decode(grid):
    payload = os.urandom(codec.max_payload_bytes(grid))
    img = codec.render(codec.encode(payload, grid), 6)
    for t in _all_transforms(img):
        assert codec.decode(t, grid) == payload


def test_bad_magic_and_bit_flip():
    g = 64
    code = codec.encode(b"hello world", g)
    assert codec.decode_matrix(code) == b"hello world"
    bad = code.copy()
    y, x = np.argwhere(~codec.reserved_mask(g))[0]  # first header bit (magic MSB)
    bad[y, x] ^= 1
    assert codec.decode_matrix(bad) is None
    assert codec.decode_matrix_any(bad) is None


def test_masks_readonly_and_grid_bounds():
    assert not codec.reserved_mask(64).flags.writeable
    with pytest.raises(ValueError):
        codec.encode(b"x", 12)
    with pytest.raises(ValueError):
        codec.encode(b"x", 5000)


def test_garbage_returns_none():
    assert codec.decode(np.full((100, 100), 255, np.uint8), 32) is None
    assert codec.decode(np.random.default_rng(0).integers(0, 256, (200, 200), dtype=np.uint8), 64) is None
    assert codec.decode(np.zeros((10, 10), np.uint8), 64) is None
