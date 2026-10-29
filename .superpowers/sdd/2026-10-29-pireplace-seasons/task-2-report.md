# Task 2 report

## Implemented
- `Track.duration_s` (last field, default None) from `RunTimeTicks`/10_000_000; bool, non-int, <=0 -> None.
- `JellyfinClient.download_transcoded(item_id, dest, bitrate_kbps)`: GET `/Audio/<id>/stream.ogg?audioCodec=opus&audioBitRate=<kbps*1000>&audioChannels=2&static=false&userId=...`.
  Accepts only 200, Content-Type starting `audio/` (parameters allowed), first 4 received bytes `OggS`, Content-Length (if present) matching.
  Same opener/deadline/read1 loop, `open(dest,"xb")`, fsync, dest removed on any failure, errors raised outside except, messages contain path only.
  `download()` untouched.
- Fake: `transcodes`, `transcode_type`, route `/Audio/<id>/stream.ogg` (honours fail, html, truncate, trickle; no Content-Length by default), plus a new `redirects` knob (path -> 302 Location) for the redirect test (no existing redirect test/knob existed for /Download).

## TDD
- RED: `.venv/bin/python -m pytest -q tests/test_jellyfin.py` -> 11 failed, 19 passed.
- GREEN: `.venv/bin/python -m pytest -q` -> 482 passed, 1 skipped.

## Concerns
- For item Ids in `truncate`, the transcode route sends a full Content-Length with half the bytes; without it a short body on a close-delimited stream is undetectable. Default transcodes send no Content-Length.
- Real Jellyfin mid-stream cuts (no Content-Length) are only caught by the OggS check; Task 4 may want a sanity check.
- Brief's "mirrors the existing redirect test": none existed; wrote a new one using a second FakeJellyfin that must receive zero requests.

## Fix round 1
- `download_transcoded(..., max_bytes=64 MiB)`: aborts and removes dest once written > max_bytes.
- Both downloads unlink dest only if this call created it (`created` set after `open(dest,"xb")`); pre-existing dest (FileExistsError, symlink, HTTP/content-type reject) is left untouched. Stale-file test updated (file now survives); its item id changed from "z"*32 to "f"*32 since ids are now validated.
- Both validate `item_id` against `_ITEM_ID` and `download_transcoded` validates bitrate (int, not bool, 32..512) before any request.
- Tests added: max_bytes, content-type reject with existing dest, existing dest kept (both methods), bad ids and bad bitrates make no request. Not strictly red-first this round (tests and fix written together).
- `pytest tests/test_jellyfin.py` -> 48 passed; full suite -> 500 passed, 1 skipped.
