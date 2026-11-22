# Lightferry: architecture and spec (POC)

Visual sneakernet: files/folders and a signed command channel between two machines. Sender S shows binary-matrix
codes on screen; receiver R reads them with a webcam (R MUST have one). R -> S goes by display-and-scan (S has a
camera) or acoustically (R speaker -> S mic, section 11). Half-duplex. User decisions of 2026-11-22 are in the body;
a phone test client (section 12) stands in for R's webcam on this dev machine.

## 1. Packaging and layout
One pip-installable package `lightferry` (src layout, `pyproject.toml`, hatchling). `python -m build` or `uv build`
-> `dist/lightferry-<ver>-py3-none-any.whl`; `pip install lightferry-*.whl`. Entry points: `lightferry =
"lightferry.cli:main"` and `python -m lightferry`. `requires-python = ">=3.11.4"` (first 3.11 with `tarfile`
`filter="data"`). Dependencies `numpy>=1.26`, `opencv-python>=4.8` (not `-headless`: needs `imshow`). Optional
extras: `lightferry[audio]` = `sounddevice` (PortAudio, cross-platform); `lightferry[mobile]` = `websockets`,
`cryptography` (self-signed cert), `segno` (terminal QR). Air-gapped: `pip download -d wheelhouse lightferry-*.whl`,
then `pip install --no-index --find-links wheelhouse lightferry`; wheels must match target OS/arch/Python.
```
Lightferry/  pyproject.toml README.md .env.example .gitignore (.env, *.key, logs/, dist/)  tests/  docs/PLAN.md
  src/lightferry/  __init__.py __main__.py  cli.py argparse · codec.py one code · detect.py finders + warp
    tiler.py bytes <-> tiles · archive.py tar/compress/safe extract · packet.py wire, HMAC, keys, replay window
    handshake.py HANDOFF · link.py stop-and-wait ARQ · channels.py webcam/screen, loop, dir · acoustic.py MFSK modem
    io_platform.py OS camera/display/audio backends (not "platform.py": shadows stdlib) · paths.py wire paths, jail
    command.py command policy · log.py audit log · bench/{__init__,degrade,benchmark}.py · mobile/{server.py,static/}
```

## 2. CLI
```
lightferry send    --path=<file-or-folder>     [common]   (older spelling: --send)
lightferry receive --path=<destination folder> [common]   (older spelling: --receive)
lightferry cmd   [common] -- python tools/report.py --day 1                  # send one command
lightferry serve [common] [--jail DIR --scripts DIR --allowlist FILE]        # command server
lightferry mobile-serve --path=<file-or-folder> [--key ...] [--host <LAN IP>] [--port 8443]   # phone test, S in-process
lightferry mobile-serve --remote-sender --path=<dest> [...]   # phone = R for another machine's `send --no-camera`
lightferry bench [--sweep | --sweep-audio | --throughput] [--camera loop|dir:<p>] | selftest | keygen --out FILE
common: --key <keyfile> (or env LIGHTFERRY_KEY) --camera webcam|dir:<path>|loop
        --backchannel auto|camera|audio  --no-camera  --audio default|<device>|audio-dir:<path>|loop
        --grid 64 --module-px 4 --tiles 3x3 --compress auto|lzma|zlib|none --timeout 1.5 --retries 8
        --overwrite --yes (test hook only)
```
`--path=X`/`--path X` both work; `send`/`receive` exclusive. `--grid/--module-px/--tiles` = S's data-phase
proposal. `--backchannel auto` = audio if S has no camera or `--no-camera`, else camera. `receive` needs a camera;
its `--path` is the output dir (created if missing), item at `<path>/<name>`, no overwrite without `--overwrite`.
`--key`: key file (32 B `secrets.token_bytes`, mode 600, git-ignored, moved by hand); `LIGHTFERRY_KEY` = hex or
`@path`; warn on hex in argv. `--yes` only with `--camera loop|dir:` (error with `webcam`); never skips VERIFY or
y/N on real hardware. `loop` is in-process only (selftest, bench, pytest); two processes use `dir:`/`audio-dir:`.

## 3. codec.py (layer 0: one code; v2 implemented, v3 next)
N x N modules, 4x4 finders in the corners, each with a 1-module white separator ring on its two inner sides (9
cells per corner, 36 total, never data); 2-module white quiet zone (image = (N+4)^2 modules). Data cells = the rest,
row-major, bit 1 = black. Header `struct ">BBHI"`: magic 0xB7, version, uint16 payload_len, uint32
`zlib.crc32(payload)`; payload follows, zero padded. The whole data buffer is XOR-whitened with the fixed public
keystream `SHA-256(b"lightferry-whiten-v2" || ctr_be32)`, ctr = 0, 1, ... (implemented; not a secret). No ECC. Grid
is not encoded: it is fixed per session by the HANDOFF. Allowed grids 32/64/96/128/177.
**v3 (decided): asymmetric finders, no rotation search.** Each corner finder gets a distinct signature inside its
own 4x4 (e.g. solid / 2x2 white centre / white centre cross / white corner notch; final patterns chosen in M1b to
survive blur). `orient()` reads the 4 signatures and rotates/mirrors the matrix to canonical form before
`decode_matrix`, so one decode per code. Finder and ring cells are unchanged, so capacity is unchanged:
`max_payload_bytes(g) = ((g*g - 100) // 8) - 8` (100 = 64 finder + 36 ring cells); body = payload - 56 B.

| grid | 32 | 64 | 96 | 128 | 177 |
|---|---:|---:|---:|---:|---:|
| data cells | 924 | 3996 | 9116 | 16284 | 31229 |
| max payload (B) | 107 | 491 | 1131 | 2027 | 3895 |
| packet body (B) | 51 | 435 | 1075 | 1971 | 3839 |
```python
def max_payload_bytes(grid: int) -> int
def encode(payload: bytes, grid: int) -> np.ndarray                              # uint8 NxN matrix
def render(code: np.ndarray, module_px: int = 8, quiet: int = 2) -> np.ndarray   # gray image
def decode(img: np.ndarray, grid: int, quiet: int = 2) -> bytes | None           # aligned image
def decode_matrix(code: np.ndarray) -> bytes | None
def orient(code: np.ndarray) -> np.ndarray | None                                # v3: canonical orientation
```
`decode`: gray -> `resize((N+4,N+4), INTER_AREA)` -> Otsu -> strip quiet zone -> (v3: `orient`) -> `decode_matrix`
(de-whiten, reject bad magic/version or `payload_len > max`, CRC) -> payload or None.

## 4. detect.py (perspective correction for camera frames)
1. Gray -> `adaptiveThreshold` -> `findContours(RETR_TREE)`; finder candidates: `approxPolyDP` 4 vertices, convex,
   aspect ~1, next to white (fill test allows the v3 signature holes).
2. Group 4 similar-size candidates into a convex quad whose side/finder ratio matches the session grid (3x3/4x4
   layouts; tiles separated by >= 2 quiet modules); the finder signatures label each corner.
3. Outer finder vertices = grid corners -> `findHomography` over all 16 finder corners (`cornerSubPix`-refined) ->
   `warpPerspective` -> sample module centres with a per-code threshold from finder (black) and quiet-zone (white)
   levels (global Otsu fails under lighting gradients).
```python
def find_codes(frame: np.ndarray, grid: int) -> list[CodeQuad]
def decode_frame(frame: np.ndarray, grid: int) -> list[bytes]
```
Rule of thumb: >= 3 camera px per module. Grid 177 in 3x3 needs 3 x 181 x 3 = 1629 px per side, more than a 1080p
frame's height: infeasible at 720p-1080p (grid 177 is 1x1 only; grid 64 3x3 needs ~620 px and fits).

## 5. Wire format (layer 1: one packet per code payload)
```
0 1 ptype  HELLO=1 HELLO_ACK=2 SYNC=3 SYNC_ACK=4 READY=5 START=6 DATA=7 ACK=8 CMD=9 RESULT=10
           DONE=11 ABORT=12 DONE_ACK=13          1 1 flags (bit0 LAST)
2 8 session_id (0 in HELLO/HELLO_ACK; then SHA-256(nonce_S || nonce_R)[:8])
10 4 counter (u32 per direction, assigned once at first seal)   14 2 obj_id (0 manifest, 1 archive, 2.. cmd)
16 4 seq   20 4 total   24 n body   24+n 32 tag = HMAC-SHA256(K_dir, b"LF1" || bytes[0:24+n])
```
`struct ">BBQIHII"` = 24 B + 32 B full tag (never truncated on visual packets) = 56 B overhead, plus the 8 B codec
header; grid 64 -> 435 B body. Verify with `hmac.compare_digest` before parsing. CRC is noise detection only.
**Keys and direction binding** (distinct labels: no packet can be reflected or verified under another role):
`K_hello_<role> = HMAC(PSK, b"lf-hello-" || role)`, role s|r (client = s, server = r);
`K_sess = HMAC(PSK, b"lf-session" || nonce_S || nonce_R)` (never MACs directly);
`K_s2r = HMAC(K_sess, b"lf-s2r")`, `K_r2s = HMAC(K_sess, b"lf-r2s")`; audio tags use `K_audio` (section 11).
**Replay and duplicates.** Old sessions fail because K_sess depends on fresh nonces; nothing persists across runs.
Tiling and retransmission legitimately repeat packets, so "counter > last" is wrong. Retransmissions re-show the
cached sealed bytes (same counter and tag). Per direction the receiver keeps the highest counter and a sliding
bitmap of the last W = 4096: above -> accept, slide; inside and unseen -> accept, mark; inside and seen -> DUPLICATE
(re-show the cached reply, never re-apply); below -> REPLAY, reject and log. DATA tiles are also deduped by
(obj_id, seq) and CMDs by obj_id, so nothing is re-applied even under a new counter.
```python
def derive_keys(psk: bytes, nonce_s: bytes, nonce_r: bytes) -> SessionKeys
def seal(k: bytes, hdr: Header, body: bytes) -> bytes
def open_packet(k: bytes, raw: bytes, window: ReplayWindow) -> Packet  # AuthError / ReplayError; .duplicate
```

## 6. HANDOFF phase (handshake.py), required before any data
**Bootstrap profile** (fixed; every visual packet until START is acknowledged, and ABORT while no data profile is
agreed): grid 64, module_px 6, tiles 1x1, quiet 2, one code centred full-screen; HELLO/HELLO_ACK bodies fit in 435 B
(compact JSON); SYNC/SYNC_ACK too (manifest tiled if needed). The agreed data profile starts with the first DATA
frame. With `--backchannel audio` every R -> S message below goes over audio (section 11).
1. **HELLO** (shown until a reply; MAC `K_hello_<own role>`): role, proto version, `nonce` (16 random B), mode
   file|cmd, backchannel, caps {grids, module_px, tiles, compress (lzma only if it imports), max_res}. Camera mode:
   both sides show HELLO; a valid HELLO with our own role (both `--send`) -> ABORT ROLE_COLLISION on both; mode
   mismatch -> MODE_MISMATCH. Audio mode: only S shows HELLO; R requires role s (else ROLE_COLLISION on R's console).
2. **HELLO_ACK** (R -> S, MAC `K_hello_r`): nonce_S echo, nonce_R, chosen data profile (best common setting). S
   derives keys -> AUTHED. R derives keys when sending but is AUTHED only on the **first valid K_s2r packet** (SYNC,
   or READY/CMD in cmd mode), re-sending HELLO_ACK until then (proves S is live, not a replayed HELLO).
3. **SYNC** (S -> R, tiled object 0): manifest JSON {name, kind, entries [{path, size}], total_size, archive_size,
   compress, tile_count, archive_sha256}. R checks: dest writable (create + delete a temp file), free space >=
   total_size + archive_size, no collision (exact and case-insensitive, NFC + `casefold`, among entries and against
   dest), Windows path length (dest + longest entry < 260 unless long paths are enabled), sizes match tile_count.
   **SYNC_ACK**: {manifest_sha256 echo (audio: first 8 B), status OK | DEST_NOT_WRITABLE | NO_SPACE | EXISTS |
   CASE_COLLISION | PATH_TOO_LONG | BAD_MANIFEST}; echo != S's hash -> ABORT MANIFEST_MISMATCH.
4. **VERIFY**: both screens show 6 digits; each operator compares and presses y. File mode: `int(HMAC(K_sess,
   b"verify" || manifest_sha256)[:4]) % 10**6`; cmd mode: `int(HMAC(K_sess, b"verify-cmd" || nonce_S ||
   nonce_R)[:4]) % 10**6` (nonces only). A human check against mixing up machines/files, not extra crypto.
5. **READY / START**: R enters READY after its y and sends READY; S enters START after its own y and R's READY and
   shows START; R ACKs START -> both DATA (section 8).
6. **DONE** (R -> S: {status, sha256 ok, files}) once R has verified and moved the item into place; S answers
   **DONE_ACK** (shown 3 s); R repeats DONE until DONE_ACK or `--retries` (data committed: missing DONE_ACK = warning).
States S: `IDLE -> HELLO -> AUTHED -> SYNCED -> VERIFY -> START -> DATA -> DONE -> CLOSED`; R: `IDLE -> HELLO ->
HELLO_ACKED -> AUTHED -> SYNCED -> VERIFY -> READY -> DATA -> DONE -> CLOSED`. Cmd mode skips SYNCED and enters CMD
after START. Any state -> `ABORTED`. Re-show the last message every `--timeout` (1.5 s; audio: from airtime) up to
`--retries`. Phase limits: HELLO 120 s, SYNC 30 s (audio 90 s), VERIFY 120 s. ABORT {reason}: best key available,
shown 3 s; Esc/Ctrl-C aborts. Duplicate HELLO after AUTHED -> re-send HELLO_ACK; new-nonce HELLO mid-session ignored.
**KEY MISMATCH (decided):** a CRC-good/MAC-bad frame (wrong key; noise fails CRC instead) shows a "KEY MISMATCH?"
warning and listening continues; abort KEY_MISMATCH only after 10 s (configurable) of MAC failures with no valid
frame, so stray frames from an old session or a third machine cannot kill a session. Audio tag failures never count.
```python
class Handoff:
    def __init__(self, role: Role, link: Link, psk: bytes, caps: Caps, confirm: Callable[[str], bool]): ...
    def run(self, manifest: Manifest | None) -> Session    # raises HandoffError(reason)
```

## 7. archive.py
S always builds a tar (file or folder, one code path), then compresses. Members: relative, forward-slash, NFC,
sorted, mtime kept; symlinks/devices/FIFOs skipped and listed as `skipped`; nothing followed outside the root.
`auto` (default) tries lzma (zlib if the optional `lzma` module does not import; then it is not offered in caps)
and sends `none` if the result is not smaller than the tar. Extract: streaming decompression
(`LZMADecompressor`/`zlib.decompressobj`, `max_length`), aborting once output exceeds the manifest's tar size;
`tarfile` `filter="data"` plus own checks (absolute, `..`, drive letters, backslashes, links, non-regular files,
reserved names, case-insensitive duplicates); total size must match. Extract into `<dest>/.lightferry-tmp-<session>`,
verify sha256 + file list, `os.replace` into place. Any abort/exception removes the temp dir (`try/finally`,
`shutil.rmtree`); a crash leftover is reported at startup, not auto-deleted.
```python
def pack(src: Path, compress: str) -> tuple[Path, Manifest]
def unpack(archive: Path, manifest: Manifest, dest: Path, overwrite: bool = False) -> Path
```

## 8. tiler.py and link.py
```python
def split(data: bytes, chunk: int, obj_id: int) -> list[Tile]
def join(tiles: Iterable[Tile], expect_sha256: bytes | None) -> bytes
def layout(codes: list[np.ndarray], module_px: int, cols: int, rows: int) -> np.ndarray
class Link:
    def __init__(self, src: FrameSource, sink: FrameSink, back: BackChannel, cfg: LinkConfig): ...
    def exchange(self, packets: list[bytes]) -> list[bytes]   # show frame until ACK/reply, retry
```
Stop-and-wait per frame: S shows cols*rows tiles until an ACK arrives (camera or audio). ACK body = frame number +
bitmap of tiles received (<= 16 tiles = 2 B). The next frame carries only missing (cached sealed bytes) or new
tiles. Timeout -> re-show; after `--retries` -> ABORT. R repeats its last ACK until a newer DATA frame arrives.
With audio acks (~2-4 s each) prefer large frames (3x3/4x4 tiles).

## 9. Channels and testability (channels.py; this dev machine has no webcam)
```python
class FrameSink(Protocol):   def show(self, frame: np.ndarray) -> None
class FrameSource(Protocol): def read(self, timeout_s: float) -> np.ndarray | None
def open_channel(spec: str, role: str) -> tuple[FrameSource, FrameSink]   # from --camera
```
- `webcam`: WebcamSource (`cv2.VideoCapture`; DSHOW/MSMF on Windows, AVFOUNDATION on macOS, V4L2 on Linux; MJPG,
  buffer 1, focus/exposure locked if possible) + ScreenSink (fullscreen `imshow`, main thread on macOS). Stale
  frames: `CAP_PROP_BUFFERSIZE` is not always honoured, so drain before each read (`grab()` until one returns faster
  than the frame interval, or 2-4 grabs) and use only frames captured after our last `show()`. Torn frames: accept
  a code only when the same code (same tag) appears in 2 consecutive frames.
- `loop`: in-process queue pairs (selftest, bench, pytest) with a seeded `Impairment` hook (degrade.py, drops, bit
  corruption, delay, reordering).
- `dir:<path>`: DirChannel for two processes. Atomic PNG writes (`tmp` + `os.replace`) to `<path>/<role>/`; the
  reader takes the newest file, reads it fully, closes, deletes consumed ones. On Windows `os.replace`/delete can
  raise `PermissionError` while the peer has the file open: retry with backoff (10 ms doubling, ~1 s total).
Hardware checklist: (1) camera 720p/1080p; (2) grid-64 1x1 at 30-60 cm, ~20 deg, rotated and mirrored; (3) densest
grid x tiles with > 95% tile success; (4) lights on/off, brightness 50/100%; (5) right key, wrong key (warn, abort
after 10 s), both `--send`; (6) 1 MB file + nested folder both ways: sha256, bytes/s, retries, RTT; (7) cover camera:
clean ABORT, temp dir gone; (8) commands: pwd, ls, cd outside jail, pinned/modified script, unknown (y/n), replayed
CMD, log; (9) audio: R speaker 25/50/100% x S mic gain at 0.5/1/2 m, quiet and noisy (speech/fan/music); (10) echo
and feedback, OS echo cancellation/AGC; (11) `--no-camera` S completes 100 KB; avoid Bluetooth; (12) phone client.

## 10. Command channel (command.py; `lightferry cmd` / `lightferry serve`)
After HELLO/HELLO_ACK (mode=cmd; no SYNC; nonce-only VERIFY code). Needs the camera back-channel (RESULTs are too
big for 50 bps audio). CMD body: JSON `{"argv": [...], "cwd": wire_path}`, never a shell string. RESULT:
`{"rc","stdout","stderr","truncated"}`.
- `pwd`, `cd`, `ls`: in-process (`os.scandir`), confined to `--jail`; cwd kept per session.
- `python <script.py> [args]`: only if (a) the script resolves inside `--scripts` and ends in `.py`, (b) its SHA-256
  is in the allowlist file (kept outside the scripts root and the receive folder), (c) the tree hash of the whole
  scripts root (sorted relpath + SHA-256 of every file) matches the pinned value, so a changed or added helper also
  blocks it. No interpreter flags from the wire. Runs `subprocess.run([sys.executable, "-I", script, *args],
  shell=False, cwd=..., env=clean_env, timeout=T, capture_output=True)`: `-I` ignores `PYTHON*` vars, user
  site-packages and the script's dir on `sys.path`; `clean_env` = PATH (+ SYSTEMROOT, TEMP/TMP on Windows; LANG on
  POSIX). Output truncated to a cap.
- Windows pitfalls: reject `:` after the drive (alternate data streams, `x.py:evil`), trailing dots/spaces (`x.py.`
  opens `x.py`), 8.3 names (`SCRIPT~1.PY`); compare the resolved long path (`os.path.realpath`), never the wire string.
- Scope: a pinned script is arbitrary code with the server user's rights and can touch anything that user can,
  regardless of `--jail`; the setup is only as safe as the pinned scripts.
- Anything else: show argv + cwd on the server console, `y/N` (default No, timeout = No), `shell=False`.
- Log `logs/commands.jsonl`: ts (ISO-8601 UTC), session_id, counter, argv, cwd, decision (allow/confirm_yes/
  confirm_no/deny/hash_mismatch/bad_mac/replay/duplicate), rc, duration_ms. Never log the key.
Also: no `shell=True`/joined argv; traversal (`..`, absolute, `C:`, `\\host`, symlink, NUL, CON/COM1) via `resolve()` +
`commonpath`, re-resolved right before use. Signed, not encrypted.

## 11. Acoustic back-channel (acoustic.py; R speaker -> S microphone)
Carries every R -> S message (HELLO_ACK, SYNC_ACK, READY, ACK, DONE, ABORT); S -> R stays visual. S needs a mic,
R a speaker; `sounddevice` (`lightferry[audio]`), clear startup error if missing. Cmd mode stays camera-only.
- **Modem**: 16-ary MFSK, 4 bits/symbol, ~80 ms symbols (~50 bps) in 1.2-3.5 kHz, raised-cosine edges; consecutive
  symbols alternate between two interleaved 16-tone sets (~74 Hz spacing) so a reverb tail misses the next symbol's
  bins. A linear chirp preamble, found by matched filter, gives timing and equalises per-tone gain (speaker/mic/room)
  before the Goertzel/FFT energy decision. `fs` is required (44.1/48 kHz tested); +-0.1% clock offset tolerated.
- **Frame** = [len u8][body][tag 4 B], body = [ptype u8][seq u16][payload], sent N times (default 2, up to 4),
  deduped by seq. Tag = `HMAC-SHA256(K_audio, context || direction || session_id || seq || len || body)[:4]`,
  `K_audio = HMAC(K_r2s, b"lf-audio-tag")`, context = label per message use, direction = r2s. HELLO_ACK (no session
  keys yet) uses `HMAC(K_hello_r, b"lf-audio" || nonce_S)` and sends only nonce_R + profile. ACK = frame_no u8 +
  2 B bitmap -> 11 B frame -> ~1.8 s + ~0.3 s preamble.
- **As built: note tokens** (supersedes the frame format above). R never sends packets over audio. Its whole
  reply is a 2-byte token, `HMAC(K_r2s, b"lf-note" || kind || ptype || counter of the S packet it answers [||
  manifest sha])[:2]`, played as a 0.1 s chirp + 4 notes of 50 ms, twice (`acoustic.encode_notes`, ~0.7 s). No seq, length or
  acoustic tag: S only accepts a token it expects for a packet it showed recently (`Sender.expected`), which is
  also what rejects noise (`acoustic.NoteListener`). R speaks three times per transfer: `ok` (HELLO seen), `sync`
  (manifest complete, accepted, ready), `done` / `fail` (archive extracted or not). Meanwhile S cycles the
  manifest, then START + every data tile, one code per `dwell_s` (0.25 s), with no per-tile acks.
  No HELLO_ACK: keys come from the PSK and S's nonce only (R's nonce is fixed zero), S's proposal is the profile,
  and R's error reasons are not sent (S times out). A token is bound to the session (S's fresh nonce) and to the
  packet's counter, so it can't be replayed; 16 bits only resist guessing within one packet's short window, and a
  forgery can at worst advance S without R (the archive sha256 check on R still guards integrity).
- **When R speaks** (audio): only for a never-seen packet that completes a step (a code still on screen asks
  nothing; no timer resends). When a full cycle ends and nothing was heard for 5 s (`link.for_audio`), S re-signs
  its codes with fresh counters, so a lost reply is asked for again.
- **Untrusted audio**: 4 bytes is too short for a long-lived key, hence the session/direction/seq binding; 2^-32
  odds per forgery attempt, capped by airtime and a rate limit (> 20 bad tags/min -> log, ignore that minute). A
  forgery can at worst stall a session (fake HELLO_ACK -> R rejects S's SYNC); integrity rests on the visual tags and
  archive sha256. Noise and bad tags never abort; only timeouts drive retries; audio ABORT needs a valid tag.
```python
def encode_message(body: bytes, key: bytes, fs: int, repeat: int = 2, **ctx) -> np.ndarray   # float32 samples
def decode_audio(samples: np.ndarray, key: bytes, fs: int, **ctx) -> list[bytes]             # valid bodies
class BackChannel(Protocol): def send(self, body: bytes) -> None; def recv(self, timeout_s: float) -> bytes | None
```
Test channels: `loop` (in-memory, seeded: white noise to SNR 0 dB and below, 50/60 Hz hum + harmonics, synthetic
reverb, clock drift by resampling, gain, clipping, dropped blocks) and `audio-dir:<path>` (WAV files between two
processes, atomic write + `os.replace`, same retry rules as `dir:`).

## 12. Mobile test client (mobile/; `lightferry mobile-serve`; test only)
Exercises the real camera + acoustic path on this webcam-less dev machine. S runs here (`--send --backchannel
audio`); a phone is R's camera and speaker, while R's logic (handshake, decode, archive, keys) runs in the server.
- **Flow**: phone opens `https://<LAN IP>:8443/?t=<token>` (URL + terminal QR from `mobile-serve`); the PWA streams
  `getUserMedia` rear-camera JPEGs (~1280x720, q ~0.7, 5-10 fps, one in flight) over a WebSocket into R as a
  `PhoneFrameSource` (same stale/two-frame rules), and shows progress. R's acks are sealed in Python (the phone never
  holds the key), sent as frame bytes, played as MFSK via WebAudio and heard by this machine's mic (`sounddevice`).
  No mic here (test shortcut): the server also writes each ack to `audio-dir:<path>` and S uses `--audio audio-dir:`.
- **Endpoints**: `GET /?t=` (page; 403 without token), `GET /static/{app.js,modem.js,sw.js,manifest.webmanifest}`,
  `WS /ws?t=`: phone -> server binary JPEGs + `{"type":"hello","sampleRate":..}`; server -> phone `progress`
  {state, tiles, bytes}, `play` {frame hex, repeat}, `done`/`abort`.
- **JS modem** (`static/modem.js`): encoder only (chirp, alternating tone sets, raised-cosine, repeats) from tagged
  frame bytes to a `Float32Array` at the AudioContext rate (fs required); decoding stays in Python. Parity: Python
  golden vectors vs JS under node (skipped without node). Camera/audio start on a tap; screen wake lock.
- **Remote sender** (`--remote-sender`): S is another machine (`lightferry send --no-camera --backchannel audio`);
  the phone scans that machine's window. Only R runs in the server, and each sealed reply is played in
  `io_platform.AudioBack`'s wire format (fragments <= 46 B, key `HMAC(psk, "lf-audio-tag")`, no context), so S's
  mic reassembles it like any audio R. The in-process mode's hash-reference shortcut cannot cross machines.
- **Security**: off by default, separate subcommand, never in the real air-gapped path. Self-signed cert per run
  (phones need HTTPS for camera/mic), fingerprint printed. 128-bit URL token, constant-time check on every HTTP/WS
  request, one client, dies with the server. LAN interface only, no CORS, frame size/rate capped; PSK stays in Python.
```python
async def serve(dest: Path, psk: bytes, host: str, port: int, audio_dir: Path | None = None) -> None
def make_self_signed(host: str, out_dir: Path) -> tuple[Path, Path, str]   # cert, key, sha256 fingerprint
```

## 13. Paths (paths.py) and benchmarks (`lightferry bench`)
Wire paths are relative, `/`-separated, UTF-8 NFC, no `.`/`..`/empty segments, no trailing dots/spaces, no `:`, no
reserved device names; converted only at the edges. `to_wire(local: Path, root: Path) -> str`, `from_wire(wire:
str, root: Path) -> Path` (raises UnsafePath), `collision_key(wire: str) -> str` (NFC + casefold); tested on Linux
with `PureWindowsPath`/`PurePosixPath`. Bench `--sweep`: encode + render -> canvas 720p/1080p/4k -> perspective
jitter 0-8%, rotation/mirror -> lighting gradient + noise -> blur sigma 0-2 -> JPEG q 95/75/50 -> decode, over grid x
module_px {2,3,4,6,8} x resolution x tiles (misfits skipped and reported), 50 seeded trials; BER, tile success,
goodput -> CSV + summary. `--sweep-audio`: frame success vs SNR (+10 .. -6 dB), RT60, drift, repeat N.
`--throughput`: end-to-end bytes/s incl. HANDOFF, acks, retries, mean RTT on `loop`, `dir:`/`audio-dir:`, devices.

## 14. Milestones
M0 skeleton (pyproject `>=3.11.4` + pins, entry points, wheel, `.env.example`, private repo `aigamer-dev/Lightferry`)
· M1 codec v2 (done) · **M1b codec v3 asymmetric finders + `orient`** · M2 degrade + sweep (aligned) · M3 detect.py +
sweep · M4 tiler + multi-code frames · M5 packet + paths + archive · M6 channels (loop, dir) + link · **M6a acoustic
modem (hardened) + BackChannel + `--sweep-audio`** · M7 HANDOFF + `--send/--receive` end to end on loop/dir, camera
and audio back-channels · M8 command channel · **M8a mobile test client + JS encoder parity** · M9 real hardware
(checklist) + throughput · M10 security review.

## 15. Test plan (pytest, no hardware)
- codec: max-capacity round trip per grid; empty; oversize raises; flipped bit or bad magic/version -> None; capacity
  table; ring white; fixed whitening stream; v3: all 8 rotations/mirrors decode via `orient`, damaged signature -> None.
- detect: homographies within 0.5 module; 3x3/4x4 frames give every payload; no false positives on blank/noise.
- tiler/link: sizes 0, 1, chunk+-1, 10 MB; duplicate/out-of-order tiles; 30% drop/corruption; lost/duplicated ACKs
  (camera and audio); retry exhaustion -> ABORT. packet: flipped byte, wrong key, reflected packet -> AuthError;
  retransmission -> duplicate; below window -> ReplayError; in-window reorder accepted once; session A into B rejected.
- handshake: happy path on loop and two-process `dir:`/`audio-dir:`; wrong key -> warning, abort after the
  no-valid-frame period (a valid frame resets it), no data sent; ROLE_COLLISION; tampered manifest; a dropped message
  at every step (HELLO .. DONE_ACK) recovered; R not AUTHED before the first K_s2r packet; each SYNC_ACK status;
  operator n; phase timeout; same 6 digits both sides; `--yes` rejected with `webcam`.
- acoustic: every R -> S message; SNR 0/-3 dB with N=2..4 (>= 99% at 0 dB), hum, reverb, +-0.1% drift, clipping,
  coloured channel; noise/speech/music only -> no frames, no abort; tag from another session/direction/seq/context
  rejected; rate limit; HELLO_ACK key bound to nonce_S; `fs` required.
- archive: round trips; hostile tars (abs, `..`, links, device, size/decompression bombs, `A`/`a`) rejected; `auto` ->
  `none` if incompressible; no-lzma -> zlib; temp dir gone after abort. paths: ADS, trailing dot/space, 8.3, reserved.
- command: allowlist; `-I`, clean env, `shell=False`; unpinned/modified script, extra tree file, outside `--scripts`,
  `python -c` denied; confirm defaults No; retransmitted CMD not re-run; log schema.
- mobile: token required on HTTP and WS, second client refused, oversize frame dropped; recorded JPEG frames through
  `PhoneFrameSource` complete a transfer with `audio-dir:` loopback; JS/Python encoder parity.
- channels/cli: replace retry on `PermissionError`; webcam two-frame rule; `--path=X`/`--path X`; exclusive
  `--send`/`--receive`; key file/env; `--backchannel auto`; fresh-venv wheel + `lightferry selftest`.

## 16. Risks
No code-level ECC (fallback: smaller grids, RS/fountain coding) · camera realities (focus, exposure, tearing, moire,
glare) · stop-and-wait RTT (audio ~2-4 s per ack) · v3 signatures must survive blur · finder look-alikes in data ·
pinned scripts are still arbitrary code · no confidentiality (any camera/mic in the room) · manual PSK distribution ·
offline wheels per target · OS quirks (macOS `imshow` thread, permissions, DSHOW/MSMF, echo cancellation/AGC) ·
acoustic noise/echo/feedback, 4-byte tags = DoS-level forgery only · mobile LAN server widens attack surface (test
only, off by default) · VERIFY friction (`--yes` test-only).

## Changes in this revision (2026-11-22)
Decisions merged (v1 table, 16-byte tag, old `--yes`, old codec API removed); orientation = asymmetric finders v3;
KEY MISMATCH = warn then abort after sustained no-valid-frames; replay window + dedupe; direction keys; bootstrap
profile; START/READY/DONE_ACK, R AUTHED on first K_s2r packet, nonce-only cmd VERIFY, ROLE_COLLISION; `-I` + clean
env + tree hash + Windows path checks; Python >= 3.11.4, compression bounds; camera stale-frame/two-frame rules, grid
177 3x3 infeasible; loop vs dir:, pins; collisions, MAX_PATH, cleanup; webcam required on R; hardened acoustic
back-channel (11); mobile test client (12).
