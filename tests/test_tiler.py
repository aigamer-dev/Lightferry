import hashlib
import random

import pytest

from lightferry import tiler


@pytest.mark.parametrize("n", [0, 1, 434, 435, 436, 870, 5000])
def test_split_join_sizes(n):
    data = random.Random(n).randbytes(n)
    tiles = tiler.split(data, 435, obj_id=1)
    assert all(t.total == len(tiles) and t.obj_id == 1 for t in tiles)
    assert [t.seq for t in tiles] == list(range(len(tiles)))
    assert len(tiles) == max(1, -(-n // 435))
    assert tiler.join(tiles, hashlib.sha256(data).digest()) == data


def test_out_of_order_and_duplicates():
    data = random.Random(1).randbytes(3000)
    tiles = tiler.split(data, 100, 1)
    shuffled = tiles + tiles[:10]
    random.Random(2).shuffle(shuffled)
    assert tiler.join(shuffled) == data


def test_reassembler_dedupe_and_bitmap():
    data = bytes(range(200)) * 5
    tiles = tiler.split(data, 100, 7)
    r = tiler.Reassembler(7)
    assert r.add(7, 0, len(tiles), tiles[0].data)
    assert not r.add(7, 0, len(tiles), tiles[0].data)
    assert r.duplicates == 1 and r.base == 1
    r.add(7, 3, len(tiles), tiles[3].data)
    r.add(7, 5, len(tiles), tiles[5].data)
    assert r.base == 1 and r.bitmap() == (1 << 1) | (1 << 3)
    assert not r.complete
    for t in tiles:
        r.add(t.obj_id, t.seq, t.total, t.data)
    assert r.complete and r.base == len(tiles) and r.assemble() == data


def test_sha_mismatch_and_inconsistency():
    tiles = tiler.split(b"abcdef", 2, 1)
    with pytest.raises(tiler.TilerError):
        tiler.join(tiles, b"\x00" * 32)
    r = tiler.Reassembler(1)
    r.add(1, 0, 3, b"a")
    with pytest.raises(tiler.TilerError):
        r.add(1, 1, 4, b"b")
    with pytest.raises(tiler.TilerError):
        r.add(2, 1, 3, b"b")
    with pytest.raises(tiler.TilerError):
        r.assemble()
    with pytest.raises(tiler.TilerError):
        tiler.join([])


def test_pack_frames_and_capacity():
    assert tiler.pack_frames(list(range(10)), 4) == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert tiler.pack_frames([], 4) == []
    assert tiler.body_capacity(64) == 435
    assert tiler.body_capacity(32) == 51
