import numpy as np
import pytest

from lightferry import bench, codec, vision

RES = bench.RESOLUTIONS["1080p"]


def _payloads(grid, n, seed):
    rng = np.random.default_rng(seed)
    return [rng.bytes(codec.max_payload_bytes(grid)) for _ in range(n)]


def _roundtrip(grid, tn, mpx, seed, transform=None):
    pl = _payloads(grid, tn * tn, seed)
    frame, lay = vision.build_frame(pl, grid, tn, mpx)
    assert lay.border >= 3 and frame.shape == (lay.size_px, lay.size_px)
    if transform is not None:
        frame = transform(frame)
    img = bench.degrade(frame, RES, bench.MILD, np.random.default_rng(seed))
    warped = vision.detect_frame(img)
    assert warped is not None
    got = vision.extract_tiles(warped, grid, tn)
    assert sorted(got) == sorted(pl)


@pytest.mark.parametrize("grid", [32, 64])
@pytest.mark.parametrize("tn", [1, 2, 3])
def test_frame_roundtrip(grid, tn):
    _roundtrip(grid, tn, 4, seed=grid + tn)


@pytest.mark.parametrize("name,fn", [
    ("rot90", lambda f: np.rot90(f, 1).copy()),
    ("rot180", lambda f: np.rot90(f, 2).copy()),
    ("rot270", lambda f: np.rot90(f, 3).copy()),
    ("mirror", lambda f: f[:, ::-1].copy()),
    ("mirror_rot90", lambda f: np.rot90(f[:, ::-1], 1).copy()),
])
def test_rotated_mirrored_frames(name, fn):
    _roundtrip(64, 2, 4, seed=7, transform=fn)


def test_fewer_payloads_than_tiles():
    pl = _payloads(32, 5, 3)
    frame, _ = vision.build_frame(pl, 32, 3, 4)
    img = bench.degrade(frame, RES, bench.MILD, np.random.default_rng(3))
    got = vision.extract_tiles(vision.detect_frame(img), 32, 3)
    assert sorted(got) == sorted(pl)


def test_garbage_returns_none():
    rng = np.random.default_rng(0)
    assert vision.detect_frame(rng.integers(0, 256, (720, 1280), dtype=np.uint8)) is None
    assert vision.detect_frame(np.full((720, 1280, 3), 128, np.uint8)) is None
    assert vision.detect_frame(np.zeros((10, 10), np.uint8)) is None


def test_build_frame_validation():
    with pytest.raises(ValueError):
        vision.build_frame([], 32, 5, 4)
    with pytest.raises(ValueError):
        vision.build_frame([b"x"] * 5, 32, 2, 4)
    with pytest.raises(ValueError):
        vision.build_frame([], 32, 1, 4, border=2)
