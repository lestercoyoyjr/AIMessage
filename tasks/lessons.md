# AIMessage — Lessons

Patterns captured after corrections / reviews, so the same mistake isn't repeated. Review this at the
start of a session on this repo. Newest first.

## 2026-08-19 — Harden every boundary that parses peer-supplied bytes
**From:** staff-level security review (see `aimessage-security-review` memory).
**Pattern:** the crypto primitives were right, but every place that decodes/parses bytes from a peer
(`open_answer`, `broadcast_ask`, `fetch`, `from_json`, tar extraction, frame reads) lacked a defensive
boundary — so a malicious peer can crash any asker, OOM it with a 4 GiB frame, or path-traverse an
install with a validly-SIGNED manifest.
**Rule for myself:** a signature proves *who* sent bytes, never that the bytes are *safe*. Any function
that ingests network/peer/artifact input must: (a) cap size before allocating, (b) wrap decode→decrypt→
parse in try/except and skip on failure, (c) sanitize any field used to build a filesystem path, and
(d) have an adversarial test that fails without the guard. Verify-then-parse, never parse-then-verify.

## 2026-08-19 — Socket reads must treat any peer error as EOF, not raise
**From:** the P0.4 oversized-frame integration test caught a `ConnectionResetError` — when the server
refused a frame and reset, the client's `_recvn` raised instead of returning what it had.
**Rule:** every `sock.recv` loop wraps recv in `except OSError: break` (covers timeout, reset, broken
pipe). A hostile or broken peer resetting mid-stream must degrade to a short read (→ `b""`), never an
exception. This is the transport-layer echo of the "harden every boundary that parses peer bytes" rule.
Corollary: write the real-socket integration test, not just the unit test — it found this; the unit test wouldn't have.

## 2026-08-19 — Test-integrity before construction
**Pattern:** the one test guarding the "E2E encryption" claim couldn't fail (`assert False` caught by a
bare `except Exception`), and the entire network layer skipped-as-passed in CI (libp2p absent). The
suite reported green while defending nothing.
**Rule:** a test that can't fail is worse than no test — it's false assurance. Use `pytest.raises(...)`
for negative cases, `importorskip` so skips are visible, and ensure CI actually exercises the code being
hardened (a `WITH_LIBP2P=1` leg). Fix test-integrity FIRST so every later fix is verifiable.

## 2026-08-19 — Features ride behind hardening; storage/Drive touches the founding promise
**Pattern:** notifications, Drive overflow, and the desktop app all wanted to come first; each would have
shipped on a node any peer can crash, and Drive would have quietly broken the zero-egress/de-identified
promise.
**Rule:** sequence hardening → observability → features. Anything that moves data off the machine
(Drive) is isolated to its own release with an explicit boundary test ("personal data never spills") —
never bundled into a security or feature release. "Buy more space" = detect + link-only prompt; the app
never performs a purchase.

## 2026-08-19 — Smallest change that closes the specific failure mode
**From:** carried over from the Southland scope-discipline feedback.
**Rule:** before writing a fix, write the smallest version that would prevent the reported failure. Treat
anything larger (new deps, abstractions, "while we're here" reworks) as a separate proposal needing
sign-off. Watch for over-engineering tells: new external calls, new allowlists, new abstractions.
