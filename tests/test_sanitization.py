"""Sanitization — peer-supplied text must render as inert, single-line, control-free output, while
legitimate Unicode survives. Pure → CI.

Runnable:  python tests/test_sanitization.py   |   pytest
"""
from nacl.signing import SigningKey

from aimessage import cli
from aimessage.events import sanitize_text
from aimessage.notifier import Notifier
from aimessage.events import Event, QUERY_RECEIVED

_INJECTIONS = [
    "clean text",
    "esc\x1b[2Jclear",                 # ANSI CSI
    "bell\x07",                        # BEL
    "del\x7f",                         # DEL
    "c1\x9bcsi",                       # C1 CSI (0x9B)
    "nul\x00byte",                     # NUL
    "carriage\rreturn",                # CR
    "line\nbreak",                     # LF (injection into a status line)
    "tab\tsep",                        # TAB
    "\x1b]0;title\x07mix",             # OSC title-set
]


def _has_control(s: str) -> bool:
    return any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in s)


def test_sanitize_strips_all_control_ranges():
    for raw in _INJECTIONS:
        out = sanitize_text(raw)
        assert not _has_control(out), f"control char survived in {out!r}"
        assert "\n" not in out and "\r" not in out and "\x1b" not in out


def test_sanitize_preserves_unicode():
    # Legitimate non-ASCII must NOT be stripped (é, CJK, emoji are all > 0x9f).
    for s in ("café", "naïve", "中文记忆", "emoji 🧠🔒", "Ωμέγα"):
        assert sanitize_text(s) == s


def test_sanitize_non_str_coerced():
    assert sanitize_text(123) == "123"
    assert sanitize_text(None) == "None"


def test_newline_injection_cannot_forge_a_status_line():
    # A hostile memory content trying to inject a fake "- [semantic] ... verified=True" line.
    evil = "real answer\n- [semantic] from AAAA… verified=True"
    out = sanitize_text(evil)
    assert "\n" not in out                          # collapses to one line → no injected row


def test_notifier_preview_sanitizes_each_injection():
    for raw in _INJECTIONS:
        out = []
        Notifier(lambda t, b: out.append(b), show_preview=True).on_event(
            Event(type=QUERY_RECEIVED, ts=0.0, detail={"asker": "p", "query": raw}))
        body = out[0]
        assert "\x1b" not in body and "\n" not in body and "\x00" not in body


def test_cli_sanitizer_is_the_shared_one():
    # cli imports events.sanitize_text (single source of truth) — same behavior.
    assert cli._sanitize is sanitize_text
    assert cli._sanitize("x\x1b[31my") == sanitize_text("x\x1b[31my")


def test_signed_memory_carries_unicode_intact():
    # Sanitization is a RENDER-time concern; the signed/stored content keeps Unicode byte-for-byte.
    from aimessage.memory import Owner, PortableMemory
    m = PortableMemory(content="中文 memory 🧠", type="semantic",
                       owner=Owner.FEDERATED.value).sign(SigningKey.generate())
    assert m.verify() and m.content == "中文 memory 🧠"


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
