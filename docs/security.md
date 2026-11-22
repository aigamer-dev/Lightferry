# Security

## Threat model

Lightferry is meant to move data across an air gap while letting the receiver trust where it came from.

In scope:

- A bystander who can show codes to the receiver's camera, or play sounds, cannot inject data: every packet carries an HMAC-SHA256 tag keyed from the pre-shared key and session nonces.
- Replayed or reordered packets are rejected (4096-wide sliding replay window per direction).
- Packets from a previous session fail the MAC because each session draws fresh nonces.
- Audio replies are keyed tokens bound to the session and the packet they answer. A recorded sender screen replayed later is refused: the receiver keeps used sender nonces in `<dest>/.lightferry-seen` (fails closed with `DEST_NOT_WRITABLE`).
- Bad audio tokens are rate limited: more than 20 in a minute pauses acceptance.
- A malicious or corrupted archive cannot write outside the destination.

Out of scope:

- Confidentiality. Data is signed, not encrypted. Anyone who sees the screen or camera feed can read it.
- Compromised endpoints, or someone who holds the key.
- Denial of service: jamming the optical or audio path stops a transfer (it aborts, it does not corrupt).
- The key's transport. You move it by hand.

## Key handling

- `lightferry keygen` writes 32 random bytes with mode 600 and never overwrites.
- Keys shorter than 16 bytes are refused.
- Prefer `--key-file` or `LIGHTFERRY_KEY` over `--key`, which is visible in the process list.
- Never commit keys. The repository ignores `*.key` and `.env`.
- The phone test client never receives the key; all key use stays on the host.
- Phone server: binds the LAN IP only (`--host` overrides), one WebSocket client (a second gets HTTP 409), a fresh self-signed cert per run in a private temp dir that is removed once loaded, a token on every request. Debug output only with `LIGHTFERRY_DEBUG=1`.

## VERIFY code

A 6-digit code derived from the session keys and manifest hash. On real hardware the operator compares it by eye and answers y/N on the console (phone server: on-screen buttons); no answer aborts with `VERIFY_TIMEOUT`. Only test channels (`--camera dir:`, `--yes`) confirm automatically, and `--yes` with a real webcam is an error.

In audio mode the sender can only hear a 16-bit token, so it reports success as "unconfirmed"; the receiver's console is authoritative (the archive sha256 is always checked there).

## Replay and sessions

Each side draws a fresh nonce per session; keys and the session id derive from both. Engines are single use. Audio message sequence numbers are 16-bit, so a session should stay well below 65536 messages per direction.

## Archive extraction safety

On the receiver the archive is validated before anything touches the destination:

- Manifest schema, total size and SHA-256 checked before extraction; decompression is bounded (1 GiB default, 256 MiB LZMA memory).
- Extraction goes to a private (mode 700) temp directory inside the destination, files and directories are cross-checked against the manifest, then moved into place; an overwrite is rolled back if it fails.
- Path rules: forward-slash, relative, NFC-normalised, no absolute paths, `..`, reserved Windows names, forbidden characters, trailing dots or spaces, case or Unicode collisions, over-long names.
- Symlinks are skipped when packing.
- Existing items are never replaced without `--overwrite`.

Everything is held in RAM; plan for a few times the transfer size in memory.

## Reporting

Report vulnerabilities privately through the repository's GitHub security advisories.
