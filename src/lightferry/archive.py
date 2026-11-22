"""Pack a file or folder into a deterministic tar, compress it, and unpack it safely.

pack()   -> (blob, manifest)   tar (sorted, uids zeroed, int mtimes kept, 0o755/0o644 modes)
                               then 'auto' compression
unpack() -> list of written files; validates the manifest schema, verifies size + sha256
            BEFORE extracting, bounds decompression (size and LZMA memory), extracts into a
            0o700 temp dir inside dest, cross-checks files AND directories against the
            manifest, and moves into place last (restoring the original if an overwrite fails).

Memory use: everything is held in RAM.  pack() peaks at roughly the total file data plus the
tar plus every compression candidate (about 3-4x the tree size; LZMA preset 9 adds ~700 MiB of
encoder state).  unpack() peaks at blob + decompressed tar + one extracted file at a time
(about 2-3x raw_size), plus up to 256 MiB of LZMA decoder state.  DEFAULT_MAX_BYTES (1 GiB)
therefore means "plan for a few GiB of RAM"; lower max_bytes on small machines.

Wire paths are forward-slash, NFC-only, and must be valid on Windows, macOS and Linux.
Set REJECT_SHORT_NAMES = True to additionally refuse Windows 8.3 style names (``FOO~1.TXT``).
"""
from __future__ import annotations

import hashlib
import io
import lzma
import os
import re
import secrets
import shutil
import tarfile
import unicodedata
import zlib
from pathlib import Path, PurePath

TMP_PREFIX = ".lightferry-tmp-"
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB; see module docstring for RAM use
_LZMA_MEMLIMIT = 256 * 1024 * 1024
MAX_COMPONENT = 255
MAX_PATH = 240
REJECT_SHORT_NAMES = False
_MODES = ("auto", "lzma", "zlib", "none")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{p}{d}" for p in ("COM", "LPT") for d in "123456789\u00b9\u00b2\u00b3"),
}
_FORBIDDEN = set('\\:<>"|?*\x00\x7f')
_SHORT_NAME = re.compile(r"~\d")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_REQUIRED = ("name", "kind", "files", "dirs", "archive_size", "archive_sha256", "compression", "raw_size")


class ArchiveError(Exception):
    """Any packing/unpacking failure or hostile input."""


# ---------------------------------------------------------------- wire paths
def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _ckey(s: str) -> str:
    """Collision key for case-insensitive / normalising filesystems."""
    return _nfc(_nfc(s).casefold())


def _check_component(c: str) -> None:
    if not isinstance(c, str):
        raise ArchiveError(f"path component is not a string: {c!r}")
    if c in ("", ".", ".."):
        raise ArchiveError(f"bad path component {c!r}")
    if len(c) > MAX_COMPONENT:
        raise ArchiveError(f"path component longer than {MAX_COMPONENT} characters")
    if any(ch in _FORBIDDEN or ord(ch) < 32 for ch in c):
        raise ArchiveError(f"forbidden character in {c!r}")
    try:
        c.encode("utf-8")
    except UnicodeError:
        raise ArchiveError(f"name is not valid unicode: {c!r}") from None
    if _nfc(c) != c:
        raise ArchiveError(f"name is not NFC-normalised: {c!r}")
    if c.endswith((".", " ")):
        raise ArchiveError(f"trailing dot/space in {c!r}")
    if c.split(".")[0].strip().upper() in _RESERVED:
        raise ArchiveError(f"reserved name {c!r}")
    if REJECT_SHORT_NAMES and _SHORT_NAME.search(c):
        raise ArchiveError(f"8.3 short-name pattern in {c!r}")


def check_wire_path(s: str) -> list[str]:
    """Validate a forward-slash, NFC, relative path; return its components."""
    if not isinstance(s, str):
        raise ArchiveError(f"path is not a string: {s!r}")
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise ArchiveError(f"absolute or empty path {s!r}")
    if len(s) > MAX_PATH:
        raise ArchiveError(f"path longer than {MAX_PATH} characters")
    parts = s.split("/")
    for c in parts:
        _check_component(c)
    return parts


def to_wire_path(p: PurePath | str) -> str:
    """Relative OS path -> forward-slash wire path (NFC-normalised, then validated)."""
    p = PurePath(p) if isinstance(p, str) else p
    if p.anchor or getattr(p, "drive", ""):
        raise ArchiveError(f"absolute path {p}")
    s = _nfc("/".join(p.parts))
    check_wire_path(s)
    return s


def from_wire_path(s: str, cls: type[PurePath] = Path) -> PurePath:
    """Wire path -> OS path of flavour `cls`, after validation."""
    return cls(*check_wire_path(s))


def check_collisions(paths: list[str]) -> None:
    """Reject duplicates and case/Unicode collisions, including between parent prefixes."""
    full: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    for p in paths:
        k = _ckey(p)
        if k in full:
            raise ArchiveError(f"duplicate or case/Unicode-colliding paths {full[k]!r} and {p!r}")
        full[k] = p
        parts = p.split("/")
        for i in range(1, len(parts) + 1):
            pre = "/".join(parts[:i])
            prev = prefixes.setdefault(_ckey(pre), pre)
            if prev != pre:
                raise ArchiveError(f"case/Unicode collision between {prev!r} and {pre!r}")


# ---------------------------------------------------------------- compression
def _compress(tar: bytes, mode: str) -> tuple[bytes, str]:
    if mode not in _MODES:
        raise ArchiveError(f"unknown compression {mode!r}")
    cands: dict[str, bytes] = {}
    if mode in ("auto", "lzma"):
        cands["lzma"] = lzma.compress(tar, format=lzma.FORMAT_XZ, preset=9)
    if mode in ("auto", "zlib"):
        cands["zlib"] = zlib.compress(tar, 9)
    if mode in ("auto", "none"):
        cands["none"] = tar
    name = min(cands, key=lambda k: (len(cands[k]), k == "none"))
    if mode == "auto" and name != "none" and len(cands[name]) >= len(tar):
        name = "none"
    return cands[name], name


def _decompress(blob: bytes, comp: str, limit: int) -> bytes:
    if comp == "none":
        if len(blob) > limit:
            raise ArchiveError("decompressed size exceeds limit")
        return blob
    if comp == "lzma":
        d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=_LZMA_MEMLIMIT)
    elif comp == "zlib":
        d = zlib.decompressobj()
    else:
        raise ArchiveError(f"unknown compression {comp!r}")
    out = bytearray()
    data = blob
    try:
        while True:
            out += d.decompress(data, max(1, limit + 1 - len(out)))
            if len(out) > limit:
                raise ArchiveError("decompressed size exceeds limit")
            if d.eof:
                break
            if comp == "lzma":
                if d.needs_input:
                    raise ArchiveError("truncated lzma stream")
                data = b""
            else:
                if not d.unconsumed_tail:
                    raise ArchiveError("truncated zlib stream")
                data = d.unconsumed_tail
        if d.unused_data:
            raise ArchiveError("trailing data after compressed stream")
    except (lzma.LZMAError, zlib.error) as e:
        raise ArchiveError(f"bad compressed data: {e}") from e
    return bytes(out)


# ---------------------------------------------------------------- pack
def _info(name: str, *, is_dir: bool, size: int, mtime: int, mode: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.mode = mode
    ti.size = 0 if is_dir else size
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _mtime(st: os.stat_result) -> int:
    return max(0, int(st.st_mtime))


def _read_file(full: str) -> tuple[bytes, int, int]:
    with open(full, "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read()
    return data, _mtime(st), (0o755 if st.st_mode & 0o111 else 0o644)


def _disp(s: str) -> str:
    return s.encode("utf-8", "replace").decode("utf-8")


def pack(path: str | os.PathLike, compress: str = "auto") -> tuple[bytes, dict]:
    if compress not in _MODES:
        raise ArchiveError(f"unknown compression {compress!r}")
    try:
        ap = os.path.abspath(os.fsdecode(os.fspath(path)))
    except TypeError as e:
        raise ArchiveError(f"bad path: {e}") from e
    if not os.path.exists(ap):
        raise ArchiveError(f"no such path: {path}")
    name = _nfc(os.path.basename(ap))
    if not name:
        raise ArchiveError("cannot pack the filesystem root")
    check_wire_path(name)
    kind = "dir" if os.path.isdir(ap) else "file"
    skipped: dict[str, str] = {}
    files: list[dict] = []
    dirs: list[str] = []
    members: list[tuple[str, bytes | None, int, int]] = []  # (tar name, data|None for dir, mtime, mode)

    def rel_wire(parts: tuple[str, ...]) -> str:
        w = to_wire_path(PurePath(*parts))
        check_wire_path(f"{name}/{w}")
        return w

    if kind == "file":
        if not os.path.isfile(ap):
            raise ArchiveError(f"not a regular file: {ap}")
        try:
            data, mt, mode = _read_file(ap)
        except OSError as e:
            raise ArchiveError(f"cannot read {ap}: {e}") from e
        members.append((name, data, mt, mode))
        files.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    else:
        def onerror(e: OSError) -> None:
            fn = os.fsdecode(e.filename) if e.filename else ap
            rel = os.path.relpath(fn, ap)
            reason = e.strerror or str(e)
            if rel == ".":
                raise ArchiveError(f"cannot read {ap}: {reason}")
            skipped[_disp(rel.replace(os.sep, "/"))] = f"unreadable directory: {reason}"

        try:
            members.append((name, None, _mtime(os.stat(ap)), 0o755))
        except OSError as e:
            raise ArchiveError(f"cannot stat {ap}: {e}") from e
        for dirpath, dirnames, filenames in os.walk(ap, followlinks=False, onerror=onerror):
            rp = os.path.relpath(dirpath, ap)
            base = () if rp == "." else tuple(rp.split(os.sep))
            keep = []
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                disp = _disp("/".join(base + (d,)))
                if os.path.islink(full):
                    skipped[disp] = "symlink"
                    continue
                try:
                    w = rel_wire(base + (d,))
                    mt = _mtime(os.stat(full))
                except ArchiveError as e:
                    skipped[disp] = f"invalid name: {e}"
                    continue
                except OSError as e:
                    skipped[disp] = f"unreadable: {e.strerror or e}"
                    continue
                keep.append(d)
                dirs.append(w)
                members.append((f"{name}/{w}", None, mt, 0o755))
            dirnames[:] = keep
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                disp = _disp("/".join(base + (f,)))
                if os.path.islink(full):
                    skipped[disp] = "symlink"
                    continue
                if not os.path.isfile(full):
                    skipped[disp] = "not a regular file"
                    continue
                try:
                    w = rel_wire(base + (f,))
                except ArchiveError as e:
                    skipped[disp] = f"invalid name: {e}"
                    continue
                try:
                    data, mt, mode = _read_file(full)
                except OSError as e:
                    skipped[disp] = f"unreadable: {e.strerror or e}"
                    continue
                members.append((f"{name}/{w}", data, mt, mode))
                files.append({"path": w, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    if kind == "dir":
        check_collisions([name] + [f"{name}/{p}" for p in dirs + [f["path"] for f in files]])
    members.sort(key=lambda m: m[0])
    files.sort(key=lambda f: f["path"])
    dirs.sort()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for tname, data, mt, mode in members:
            if data is None:
                tf.addfile(_info(tname, is_dir=True, size=0, mtime=mt, mode=mode))
            else:
                tf.addfile(_info(tname, is_dir=False, size=len(data), mtime=mt, mode=mode), io.BytesIO(data))
    tar = buf.getvalue()
    del members
    blob, comp = _compress(tar, compress)
    manifest = {
        "name": name,
        "kind": kind,
        "files": files,
        "dirs": dirs,
        "skipped": sorted(skipped),
        "skipped_reasons": {k: skipped[k] for k in sorted(skipped)},
        "archive_size": len(blob),
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "compression": comp,
        "raw_size": len(tar),
    }
    return blob, manifest


# ---------------------------------------------------------------- unpack
def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_field(v: object, what: str, hi: int) -> int:
    if not _is_int(v) or v < 0 or v > hi:  # type: ignore[operator]
        raise ArchiveError(f"bad {what}: {v!r}")
    return v  # type: ignore[return-value]


def _sha_field(v: object, what: str) -> str:
    if not isinstance(v, str) or not _HEX64.fullmatch(v):
        raise ArchiveError(f"bad {what}")
    return v.lower()


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _parse_manifest(manifest: object, max_bytes: int) -> dict:
    """Validate the manifest schema completely; raise ArchiveError on anything off."""
    if not isinstance(manifest, dict):
        raise ArchiveError("manifest is not a dict")
    for k in _REQUIRED:
        if k not in manifest:
            raise ArchiveError(f"manifest missing {k!r}")
    name = manifest["name"]
    if not isinstance(name, str):
        raise ArchiveError("bad manifest name")
    _check_component(name)
    kind = manifest["kind"]
    if kind not in ("file", "dir"):
        raise ArchiveError("bad manifest kind")
    archive_size = _int_field(manifest["archive_size"], "archive_size", max_bytes)
    raw = _int_field(manifest["raw_size"], "raw_size", max_bytes)
    archive_sha = _sha_field(manifest["archive_sha256"], "archive_sha256")
    comp = manifest["compression"]
    if not isinstance(comp, str) or comp not in ("none", "lzma", "zlib"):
        raise ArchiveError(f"bad compression {comp!r}")
    skipped = manifest.get("skipped", [])
    if not isinstance(skipped, list) or not all(isinstance(s, str) for s in skipped):
        raise ArchiveError("bad manifest skipped list")
    if not isinstance(manifest["files"], list) or not isinstance(manifest["dirs"], list):
        raise ArchiveError("manifest files/dirs must be lists")

    expected: dict[str, dict] = {}
    total = 0
    for f in manifest["files"]:
        if not isinstance(f, dict) or not all(k in f for k in ("path", "size", "sha256")):
            raise ArchiveError("bad manifest file entry")
        p = f["path"]
        check_wire_path(p)
        size = _int_field(f["size"], f"size of {p!r}", max_bytes)
        sha = _sha_field(f["sha256"], f"sha256 of {p!r}")
        if p in expected:
            raise ArchiveError(f"duplicate manifest file {p!r}")
        expected[p] = {"size": size, "sha256": sha}
        total += size
    if total > raw:
        raise ArchiveError("file sizes exceed raw_size")
    dirs: set[str] = set()
    for d in manifest["dirs"]:
        check_wire_path(d)
        if d in dirs:
            raise ArchiveError(f"duplicate manifest dir {d!r}")
        dirs.add(d)

    if kind == "file":
        if list(expected) != [name] or dirs:
            raise ArchiveError("file manifest must hold exactly its own file and no dirs")
    else:
        for p in list(expected) + list(dirs):
            check_wire_path(f"{name}/{p}")
            for par in _parents(p):
                if par not in dirs:
                    raise ArchiveError(f"undeclared parent directory {par!r}")
        check_collisions([name] + [f"{name}/{p}" for p in list(dirs) + list(expected)])
    return {"name": name, "kind": kind, "expected": expected, "dirs": dirs, "raw": raw,
            "comp": comp, "archive_size": archive_size, "archive_sha": archive_sha}


parse_manifest = _parse_manifest  # public alias


def _validate_members(tf: tarfile.TarFile, m: dict, limit: int) -> list[tarfile.TarInfo]:
    name, kind, expected = m["name"], m["kind"], m["expected"]
    names: list[str] = []
    mfiles: dict[str, int] = {}
    mdirs: set[str] = set()
    total = 0
    out = []
    for t in tf.getmembers():
        if not (t.isreg() or t.isdir()):
            raise ArchiveError(f"non-regular member {t.name!r}")
        wire = t.name.rstrip("/") if t.isdir() else t.name
        parts = check_wire_path(wire)
        if parts[0] != name:
            raise ArchiveError(f"member outside {name!r}: {t.name!r}")
        names.append(wire)
        rel = "/".join(parts[1:])
        if kind == "file":
            if len(parts) != 1 or not t.isreg():
                raise ArchiveError(f"unexpected member {t.name!r}")
            mfiles[name] = t.size
        elif not rel:
            if not t.isdir():
                raise ArchiveError("root member is not a directory")
        elif t.isdir():
            mdirs.add(rel)
        else:
            mfiles[rel] = t.size
        total += t.size
        if total > limit:
            raise ArchiveError("members exceed size limit")
        out.append(t)
    check_collisions(names)
    if set(mfiles) != set(expected) or any(mfiles[p] != expected[p]["size"] for p in mfiles):
        raise ArchiveError("archive members do not match manifest files")
    if kind == "dir" and mdirs != m["dirs"]:
        raise ArchiveError("archive directories do not match manifest dirs")
    return out


def unpack(blob: bytes, dest: str | os.PathLike, manifest: dict, overwrite: bool = False,
           max_bytes: int = DEFAULT_MAX_BYTES) -> list[Path]:
    """Verify and extract `blob`.  Every malformed input raises ArchiveError (never KeyError etc.);
    a pre-existing destination without overwrite raises FileExistsError."""
    try:
        return _unpack(blob, dest, manifest, overwrite, max_bytes)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError,
            tarfile.TarError, lzma.LZMAError, zlib.error) as e:
        raise ArchiveError(f"invalid input: {type(e).__name__}: {e}") from e


def _unpack(blob, dest, manifest, overwrite, max_bytes) -> list[Path]:
    if not hasattr(tarfile, "data_filter"):
        raise ArchiveError("this Python's tarfile lacks extraction filters; upgrade Python")
    if not _is_int(max_bytes) or max_bytes < 1:
        raise ArchiveError("bad max_bytes")
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ArchiveError("blob must be bytes")
    m = _parse_manifest(manifest, max_bytes)
    name, kind, expected, raw = m["name"], m["kind"], m["expected"], m["raw"]

    # 1. verify the blob against the manifest before touching anything
    if len(blob) != m["archive_size"]:
        raise ArchiveError("archive size mismatch")
    if hashlib.sha256(blob).hexdigest() != m["archive_sha"]:
        raise ArchiveError("archive sha256 mismatch")

    dest = Path(dest)
    final = dest / name
    if (final.exists() or final.is_symlink()) and not overwrite:
        raise FileExistsError(str(final))

    tar = _decompress(bytes(blob), m["comp"], raw)
    if len(tar) != raw:
        raise ArchiveError("decompressed size does not match manifest raw_size")

    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / f"{TMP_PREFIX}{secrets.token_hex(6)}"
    tmp.mkdir(mode=0o700)
    os.chmod(tmp, 0o700)
    keep_tmp = False
    try:
        new = tmp / "new"
        new.mkdir()
        try:
            with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as tf:
                members = _validate_members(tf, m, raw)
                tf.extractall(new, members=members, filter="data")
        except ArchiveError:
            raise
        except (tarfile.TarError, OSError, ValueError) as e:
            raise ArchiveError(f"extraction failed: {e}") from e
        del tar

        item = new / name
        if kind == "file":
            if not item.is_file() or item.is_symlink():
                raise ArchiveError("expected a file")
            actual = {name: item}
        else:
            if not item.is_dir() or item.is_symlink():
                raise ArchiveError("expected a directory")
            actual = {}
            actual_dirs = set()
            for dp, dn, fn in os.walk(item):
                for d in dn:
                    p = Path(dp) / d
                    if p.is_symlink():
                        raise ArchiveError("symlink in extracted tree")
                    actual_dirs.add(to_wire_path(p.relative_to(item)))
                for f in fn:
                    p = Path(dp) / f
                    if p.is_symlink():
                        raise ArchiveError("symlink in extracted tree")
                    actual[to_wire_path(p.relative_to(item))] = p
            if actual_dirs != m["dirs"]:
                raise ArchiveError("extracted directory list does not match manifest")
        if set(actual) != set(expected):
            raise ArchiveError("extracted file list does not match manifest")
        for rel, p in actual.items():
            data = p.read_bytes()
            f = expected[rel]
            if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise ArchiveError(f"file mismatch: {rel}")

        # 2. move into place only after everything verified
        old = None
        if final.exists() or final.is_symlink():
            if not overwrite:
                raise FileExistsError(str(final))
            (tmp / "old").mkdir()
            old = tmp / "old" / name
            os.replace(final, old)
        try:
            os.replace(item, final)
        except BaseException:
            if old is not None:
                try:
                    os.replace(old, final)
                except OSError:
                    keep_tmp = True  # leave the original recoverable inside tmp/old
                    raise
            raise
    finally:
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if kind == "file":
        return [final]
    return sorted(final / rel for rel in expected)
