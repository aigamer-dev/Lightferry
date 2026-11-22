import copy
import hashlib
import io
import lzma
import os
import stat
import tarfile
import unicodedata
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from lightferry import archive
from lightferry.archive import ArchiveError, from_wire_path, pack, to_wire_path, unpack


def make_tree(root: Path) -> Path:
    d = root / "proj"
    (d / "sub" / "deep").mkdir(parents=True)
    (d / "empty").mkdir()
    (d / "a.txt").write_bytes(b"hello" * 100)
    (d / "sub" / "deep" / "ünï-文件.bin").write_bytes(bytes(range(256)) * 10)
    (d / "sub" / "b.md").write_text("# b\n")
    (d / "c.md").write_text("# c\n")
    return d


def hostile(members, comp="lzma"):
    """members: list of (TarInfo, bytes|None). Returns (blob, manifest) with honest hashes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for ti, data in members:
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    tar = buf.getvalue()
    blob = lzma.compress(tar) if comp == "lzma" else tar
    files = [{"path": ti.name.split("/", 1)[1], "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
             for ti, d in members if d is not None and "/" in ti.name]
    dirs = [ti.name.split("/", 1)[1] for ti, d in members if d is None and ti.type == tarfile.DIRTYPE and "/" in ti.name]
    man = {"name": "x", "kind": "dir", "files": files, "dirs": dirs, "skipped": [], "archive_size": len(blob),
           "archive_sha256": hashlib.sha256(blob).hexdigest(), "compression": comp, "raw_size": len(tar)}
    return blob, man


def reg(name, data=b"d"):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    return ti, data


def no_tmp(dest: Path):
    if not dest.exists():
        return
    assert not [p for p in dest.iterdir() if p.name.startswith(archive.TMP_PREFIX)]


def test_file_roundtrip(tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"data " * 50)
    blob, man = pack(f)
    assert man["kind"] == "file" and man["name"] == "note.txt"
    assert man["files"] == [{"path": "note.txt", "size": 250, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}]
    assert man["archive_size"] == len(blob) and man["archive_sha256"] == hashlib.sha256(blob).hexdigest()
    out = tmp_path / "out"
    written = unpack(blob, out, man)
    assert written == [out / "note.txt"]
    assert (out / "note.txt").read_bytes() == f.read_bytes()
    no_tmp(out)


def test_folder_roundtrip_unicode_nested(tmp_path):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    assert man["kind"] == "dir" and man["name"] == "proj"
    assert [f["path"] for f in man["files"]] == sorted(f["path"] for f in man["files"])
    assert all("\\" not in f["path"] for f in man["files"])
    out = tmp_path / "out"
    written = unpack(blob, out, man)
    assert len(written) == 4
    for f in man["files"]:
        assert (out / "proj" / f["path"]).read_bytes() == (d / f["path"]).read_bytes()
    assert (out / "proj" / "empty").is_dir()
    no_tmp(out)


def test_two_md_files(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "one.md").write_text("# one\n")
    (d / "two.md").write_text("# two\n")
    blob, man = pack(d)
    assert [f["path"] for f in man["files"]] == ["one.md", "two.md"]
    out = tmp_path / "o"
    unpack(blob, out, man)
    assert (out / "docs" / "one.md").read_text() == "# one\n"
    assert (out / "docs" / "two.md").read_text() == "# two\n"


def test_deterministic(tmp_path):
    d = make_tree(tmp_path)
    b1, m1 = pack(d)
    b2, m2 = pack(d)
    assert b1 == b2 and m1 == m2
    # a changed mtime is now part of the archive
    st = (d / "a.txt").stat()
    os.utime(d / "a.txt", (st.st_atime, st.st_mtime + 1000))
    b3, _ = pack(d)
    assert b3 != b1


@pytest.mark.parametrize("mode", ["auto", "lzma", "zlib", "none"])
def test_compression_modes(tmp_path, mode):
    d = make_tree(tmp_path)
    blob, man = pack(d, mode)
    if mode != "auto":
        assert man["compression"] == mode
    unpack(blob, tmp_path / "o", man)


def test_auto_picks_smallest(tmp_path):
    import os
    f = tmp_path / "r.bin"
    f.write_bytes(os.urandom(50_000))
    blob, man = pack(f)
    sizes = {m: pack(f, m)[1]["archive_size"] for m in ("lzma", "zlib", "none")}
    assert man["archive_size"] == min(sizes.values())
    assert man["compression"] != "lzma"  # random data: lzma never wins


def test_symlinks_skipped(tmp_path):
    d = make_tree(tmp_path)
    (d / "link").symlink_to(d / "a.txt")
    (d / "dlink").symlink_to(d / "sub")
    blob, man = pack(d)
    assert man["skipped"] == ["dlink", "link"]
    assert "link" not in [f["path"] for f in man["files"]]
    unpack(blob, tmp_path / "o", man)
    assert not (tmp_path / "o" / "proj" / "link").exists()


def test_tamper_detection(tmp_path):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    out = tmp_path / "o"
    bad = bytearray(blob)
    bad[len(bad) // 2] ^= 1
    with pytest.raises(ArchiveError):
        unpack(bytes(bad), out, man)
    with pytest.raises(ArchiveError):
        unpack(blob + b"x", out, man)
    assert not out.exists() or list(out.iterdir()) == []


def test_manifest_file_mismatch(tmp_path):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    man["files"][0]["sha256"] = "0" * 64
    out = tmp_path / "o"
    with pytest.raises(ArchiveError):
        unpack(blob, out, man)
    assert not (out / "proj").exists()
    no_tmp(out)


@pytest.mark.parametrize("bad", ["../evil", "x/../../evil", "/abs/evil", "x\\evil", "x/C:evil",
                                 "x/a.txt:stream", "x/trail.", "x/trail ", "x/CON", "C:/evil"])
def test_hostile_names(tmp_path, bad):
    blob, man = hostile([reg("x/ok.txt"), reg(bad)])
    out = tmp_path / "o"
    out.mkdir()
    with pytest.raises(ArchiveError):
        unpack(blob, out, man)
    assert list(out.iterdir()) == []
    assert not (tmp_path / "evil").exists()


def test_member_outside_name(tmp_path):
    blob, man = hostile([reg("other/f.txt")])
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


def test_symlink_member(tmp_path):
    ti = tarfile.TarInfo("x/l")
    ti.type = tarfile.SYMTYPE
    ti.linkname = "/etc/passwd"
    blob, man = hostile([reg("x/ok.txt"), (ti, None)])
    out = tmp_path / "o"
    with pytest.raises(ArchiveError):
        unpack(blob, out, man)
    assert list(out.iterdir()) == []


def test_hardlink_and_device_members(tmp_path):
    for t in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
        ti = tarfile.TarInfo("x/h")
        ti.type = t
        ti.linkname = "x/ok.txt"
        blob, man = hostile([reg("x/ok.txt"), (ti, None)])
        with pytest.raises(ArchiveError):
            unpack(blob, tmp_path / f"o{ord(t)}", man)


def test_case_collision(tmp_path):
    blob, man = hostile([reg("x/File.txt"), reg("x/file.TXT")])
    out = tmp_path / "o"
    with pytest.raises(ArchiveError):
        unpack(blob, out, man)
    no_tmp(out)


def test_unicode_normalization_collision(tmp_path):
    blob, man = hostile([reg("x/\u00e9.txt"), reg("x/e\u0301.txt")])
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


def test_decompression_bomb(tmp_path):
    big = b"\0" * (50 * 1024 * 1024)
    blob, man = hostile([reg("x/bomb", big)])
    assert len(blob) < 100_000
    man["raw_size"] = 10_000  # lying manifest
    out = tmp_path / "o"
    out.mkdir()
    with pytest.raises(ArchiveError):
        unpack(blob, out, man)
    no_tmp(out)
    # honest manifest but hard cap
    blob, man = hostile([reg("x/bomb", big)])
    with pytest.raises(ArchiveError):
        unpack(blob, out, man, max_bytes=1024 * 1024)


def test_zlib_bomb(tmp_path):
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as tf:
        ti, d = reg("x/b", b"\0" * (20 * 1024 * 1024))
        tf.addfile(ti, io.BytesIO(d))
    blob = zlib.compress(tar.getvalue(), 9)
    man = {"name": "x", "kind": "dir", "files": [], "dirs": [], "skipped": [], "archive_size": len(blob),
           "archive_sha256": hashlib.sha256(blob).hexdigest(), "compression": "zlib", "raw_size": 5000}
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


def test_temp_cleanup_on_failure(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    out = tmp_path / "o"
    real = archive.os.replace

    def boom(*a, **k):
        raise OSError("disk exploded")

    monkeypatch.setattr(archive.os, "replace", boom)
    with pytest.raises(OSError):
        unpack(blob, out, man)
    monkeypatch.setattr(archive.os, "replace", real)
    no_tmp(out)
    assert not (out / "proj").exists()


def test_overwrite_refused_then_allowed(tmp_path):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    out = tmp_path / "o"
    unpack(blob, out, man)
    (out / "proj" / "a.txt").write_bytes(b"changed")
    with pytest.raises(FileExistsError):
        unpack(blob, out, man)
    assert (out / "proj" / "a.txt").read_bytes() == b"changed"
    no_tmp(out)
    unpack(blob, out, man, overwrite=True)
    assert (out / "proj" / "a.txt").read_bytes() == d.joinpath("a.txt").read_bytes()
    no_tmp(out)


def test_wire_paths():
    assert to_wire_path(PureWindowsPath("a\\b\\c.txt")) == "a/b/c.txt"
    assert to_wire_path(PurePosixPath("a/b/c.txt")) == "a/b/c.txt"
    with pytest.raises(ArchiveError):
        to_wire_path(PureWindowsPath("C:\\a\\b"))
    with pytest.raises(ArchiveError):
        to_wire_path(PurePosixPath("/a/b"))
    assert from_wire_path("a/b/c.txt", PureWindowsPath) == PureWindowsPath("a\\b\\c.txt")
    assert from_wire_path("a/b/c.txt", PurePosixPath) == PurePosixPath("a/b/c.txt")
    for bad in ["../x", "a\\b", "/a", "C:/a", "a/b:s", "", "a//b"]:
        with pytest.raises(ArchiveError):
            from_wire_path(bad)


# ------------------------------------------------------------ review fixes
def honest(tmp_path):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    return blob, man


def test_mtime_preserved_and_clamped(tmp_path):
    d = make_tree(tmp_path)
    os.utime(d / "a.txt", (1_600_000_000, 1_600_000_123))
    os.utime(d / "c.md", (-500, -500))
    blob, man = pack(d)
    out = tmp_path / "o"
    unpack(blob, out, man)
    assert int((out / "proj" / "a.txt").stat().st_mtime) == 1_600_000_123
    assert int((out / "proj" / "c.md").stat().st_mtime) == 0


def test_exec_bit(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "run.sh").write_text("#!/bin/sh\n")
    (d / "run.sh").chmod(0o741)
    (d / "data").write_text("x")
    (d / "data").chmod(0o600)
    blob, man = pack(d)
    out = tmp_path / "o"
    unpack(blob, out, man)
    assert stat.S_IMODE((out / "p" / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((out / "p" / "data").stat().st_mode) == 0o644


@pytest.mark.parametrize("key,val", [
    ("raw_size", -1), ("raw_size", True), ("raw_size", 1.5), ("raw_size", "5"), ("raw_size", None),
    ("raw_size", 2 ** 70), ("archive_size", -1), ("archive_size", False), ("archive_size", "1"),
    ("archive_sha256", "zz" * 32), ("archive_sha256", "ab"), ("archive_sha256", 5),
    ("name", 5), ("name", "a/b"), ("name", ""), ("kind", "link"), ("kind", None),
    ("compression", "bz2"), ("compression", 3), ("files", "nope"), ("files", [5]), ("files", [{"path": "a"}]),
    ("dirs", "nope"), ("dirs", [1]), ("dirs", ["zzz"]), ("skipped", "x"), ("skipped", [1]),
])
def test_bad_manifest_values(tmp_path, key, val):
    blob, man = honest(tmp_path)
    man = copy.deepcopy(man)
    man[key] = val
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


@pytest.mark.parametrize("fkey,val", [
    ("size", -1), ("size", True), ("size", 1.0), ("size", "3"), ("size", 2 ** 70),
    ("sha256", "g" * 64), ("sha256", None), ("path", 7), ("path", "../x"),
])
def test_bad_file_entry(tmp_path, fkey, val):
    blob, man = honest(tmp_path)
    man = copy.deepcopy(man)
    man["files"][0][fkey] = val
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


@pytest.mark.parametrize("key", ["name", "kind", "files", "dirs", "archive_size", "archive_sha256",
                                 "compression", "raw_size"])
def test_missing_manifest_key(tmp_path, key):
    blob, man = honest(tmp_path)
    man = copy.deepcopy(man)
    del man[key]
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


def test_bad_top_level_inputs(tmp_path):
    blob, man = honest(tmp_path)
    for bad in (None, [], "x", 5):
        with pytest.raises(ArchiveError):
            unpack(blob, tmp_path / "o", bad)
    with pytest.raises(ArchiveError):
        unpack("not bytes", tmp_path / "o", man)
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man, max_bytes=True)
    with pytest.raises(ArchiveError):
        unpack(blob, 123.5, man)


def test_dirs_cross_check(tmp_path):
    blob, man = honest(tmp_path)
    for mutate in (lambda m: m["dirs"].remove("empty"), lambda m: m["dirs"].append("ghost"),
                   lambda m: m["dirs"].remove("sub")):
        bad = copy.deepcopy(man)
        mutate(bad)
        with pytest.raises(ArchiveError):
            unpack(blob, tmp_path / "o", bad)
    assert man["dirs"] == ["empty", "sub", "sub/deep"]
    # tar carries an extra dir the manifest does not list
    d = tinfo_dir("x/extra")
    blob, man = hostile([reg("x/ok.txt"), (d, None)])
    man["dirs"] = []
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o2", man)


def tinfo_dir(name):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE
    return ti


def test_nfd_names_rejected_on_wire(tmp_path):
    blob, man = hostile([reg("x/ok.txt"), reg("x/e\u0301.txt")])
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)
    with pytest.raises(ArchiveError):
        archive.check_wire_path("e\u0301")
    assert to_wire_path("e\u0301.txt") == "\u00e9.txt"


def test_pack_normalises_nfd_names(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "e\u0301.txt").write_text("hi")
    blob, man = pack(d)
    assert [f["path"] for f in man["files"]] == ["\u00e9.txt"]
    out = tmp_path / "o"
    unpack(blob, out, man)
    assert (out / "p" / "\u00e9.txt").read_text() == "hi"


def test_overwrite_failure_restores_original(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    blob, man = pack(d)
    out = tmp_path / "o"
    unpack(blob, out, man)
    (out / "proj" / "a.txt").write_bytes(b"precious")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) == 2:  # the move of the new tree into place
            raise OSError("disk exploded")
        return real(src, dst)

    monkeypatch.setattr(archive.os, "replace", flaky)
    with pytest.raises(OSError):
        unpack(blob, out, man, overwrite=True)
    monkeypatch.setattr(archive.os, "replace", real)
    assert (out / "proj" / "a.txt").read_bytes() == b"precious"
    assert len(calls) == 3
    no_tmp(out)


def test_overwrite_old_name_collision(tmp_path):
    for kind in ("file", "dir"):
        src = tmp_path / kind
        src.mkdir()
        target = src / ".old"
        if kind == "file":
            target.write_text("new")
        else:
            target.mkdir()
            (target / "f").write_text("new")
        blob, man = pack(target)
        out = tmp_path / f"o-{kind}"
        unpack(blob, out, man)
        if kind == "file":
            (out / ".old").write_text("stale")
        else:
            (out / ".old" / "f").write_text("stale")
        unpack(blob, out, man, overwrite=True)
        got = (out / ".old").read_text() if kind == "file" else (out / ".old" / "f").read_text()
        assert got == "new"
        no_tmp(out)


def test_unreadable_subdir_is_skipped_with_reason(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    (d / "locked").mkdir()
    (d / "locked" / "secret").write_text("s")
    real = os.scandir

    def scandir(p="."):
        if str(p).endswith("locked"):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p)

    with monkeypatch.context() as mp:
        mp.setattr(os, "scandir", scandir)
        blob, man = pack(d)
    assert "locked" in man["skipped"]
    assert "unreadable" in man["skipped_reasons"]["locked"]
    assert "locked/secret" not in [f["path"] for f in man["files"]]
    unpack(blob, tmp_path / "o", man)


def test_unreadable_root_raises(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    real = os.scandir

    def scandir(p="."):
        if str(p) == str(d):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p)

    with monkeypatch.context() as mp:
        mp.setattr(os, "scandir", scandir)
        with pytest.raises(ArchiveError):
            pack(d)


def test_unreadable_file_is_skipped(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    real = archive._read_file

    def rf(full):
        if full.endswith("c.md"):
            raise PermissionError(13, "Permission denied")
        return real(full)

    monkeypatch.setattr(archive, "_read_file", rf)
    _, man = pack(d)
    assert "c.md" in man["skipped"] and "c.md" not in [f["path"] for f in man["files"]]


def test_pack_time_collisions(tmp_path):
    for i, (a, b) in enumerate([("File.txt", "file.TXT"), ("\u00e9.txt", "e\u0301.txt")]):
        d = tmp_path / f"c{i}"
        d.mkdir()
        (d / a).write_text("1")
        (d / b).write_text("2")
        with pytest.raises(ArchiveError, match="collision|duplicate"):
            pack(d)
    d = tmp_path / "pfx"
    (d / "Dir").mkdir(parents=True)
    (d / "dir").mkdir()
    (d / "Dir" / "x").write_text("1")
    (d / "dir" / "y").write_text("2")
    with pytest.raises(ArchiveError, match="collid|collision"):
        pack(d)


def test_pack_skips_invalid_names(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "ok.txt").write_text("ok")
    (d / "bad?.txt").write_text("1")
    (d / "CON").write_text("2")
    (d / "trail.").write_text("3")
    (d / "baddir<").mkdir()
    (d / "baddir<" / "f").write_text("4")
    (d / ("a" * 250)).write_text("5")
    blob, man = pack(d)
    assert [f["path"] for f in man["files"]] == ["ok.txt"]
    assert set(man["skipped"]) == {"bad?.txt", "CON", "trail.", "baddir<", "a" * 250}
    assert all(man["skipped_reasons"][k].startswith("invalid name") for k in man["skipped"])
    unpack(blob, tmp_path / "o", man)


def test_pack_dot_and_dotdot(tmp_path, monkeypatch):
    d = make_tree(tmp_path)
    monkeypatch.chdir(d)
    blob, man = pack(".")
    assert man["name"] == "proj" and len(man["files"]) == 4
    unpack(blob, tmp_path / "o1", man)
    monkeypatch.chdir(d / "sub")
    blob, man = pack("..")
    assert man["name"] == "proj"
    unpack(blob, tmp_path / "o2", man)
    assert (tmp_path / "o2" / "proj" / "a.txt").exists()
    monkeypatch.chdir("/")
    with pytest.raises(ArchiveError):
        pack("/")


def test_lzma_memlimit(tmp_path, monkeypatch):
    assert archive._LZMA_MEMLIMIT == 256 * 1024 * 1024
    d = make_tree(tmp_path)
    blob, man = pack(d, "lzma")
    unpack(blob, tmp_path / "ok", man)
    monkeypatch.setattr(archive, "_LZMA_MEMLIMIT", 1024 * 1024)  # preset-9 stream needs a 64 MiB dict
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)
    no_tmp(tmp_path / "o")


@pytest.mark.parametrize("bad", [
    "x/a<b", "x/a>b", 'x/a"b', "x/a|b", "x/a?b", "x/a*b", "x/CONIN$", "x/CONOUT$", "x/conin$.txt",
    "x/COM\u00b9", "x/LPT\u00b2.txt", "x/com\u00b3", "x/" + "a" * 256, "x/a\x7f",
])
def test_windows_names_rejected(tmp_path, bad):
    blob, man = hostile([reg("x/ok.txt"), reg(bad)])
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)
    with pytest.raises(ArchiveError):
        archive.check_wire_path(bad)


def test_length_caps():
    archive._check_component("a" * 255)
    with pytest.raises(ArchiveError):
        archive._check_component("a" * 256)
    ok = "/".join(["a" * 59] * 4)  # 239 chars
    archive.check_wire_path(ok)
    with pytest.raises(ArchiveError):
        archive.check_wire_path(ok + "/bb")
    with pytest.raises(ArchiveError):
        archive.check_wire_path("/".join(["a" * 50] * 5))


def test_short_names_optional(monkeypatch):
    archive.check_wire_path("PROGRA~1")
    monkeypatch.setattr(archive, "REJECT_SHORT_NAMES", True)
    with pytest.raises(ArchiveError):
        archive.check_wire_path("PROGRA~1")
    archive.check_wire_path("tilde~x")


def test_case_collision_key_nfc_casefold():
    with pytest.raises(ArchiveError):
        archive.check_collisions(["x/\u00c9.txt", "x/\u00e9.txt"])
    with pytest.raises(ArchiveError):
        archive.check_collisions(["x/Stra\u00dfe", "x/STRASSE"])
    with pytest.raises(ArchiveError):
        archive.check_collisions(["x/A/f", "x/a/g"])


def test_no_filter_is_hard_error(tmp_path, monkeypatch):
    blob, man = honest(tmp_path)
    monkeypatch.delattr(tarfile, "data_filter")
    with pytest.raises(ArchiveError):
        unpack(blob, tmp_path / "o", man)


def test_exists_checked_before_decompress(tmp_path, monkeypatch):
    blob, man = honest(tmp_path)
    out = tmp_path / "o"
    unpack(blob, out, man)

    def boom(*a, **k):
        raise AssertionError("decompressed before exists check")

    monkeypatch.setattr(archive, "_decompress", boom)
    with pytest.raises(FileExistsError):
        unpack(blob, out, man)


def test_tmp_dir_is_private(tmp_path, monkeypatch):
    blob, man = honest(tmp_path)
    real = os.replace
    seen = []

    def spy(src, dst):
        seen.append(stat.S_IMODE(Path(src).parent.parent.stat().st_mode))
        return real(src, dst)

    monkeypatch.setattr(archive.os, "replace", spy)
    unpack(blob, tmp_path / "o", man)
    assert seen == [0o700]


def test_default_max_bytes():
    assert archive.DEFAULT_MAX_BYTES == 1024 ** 3
