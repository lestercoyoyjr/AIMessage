"""Native desktop wrapper for the AIMessage dashboard.

Starts a node + its control API as a sidecar (`python -m aimessage serve --control`), waits for the
`DASHBOARD=...` line it prints, and opens that URL in a native OS window (pywebview → WKWebView on
macOS, WebKitGTK on Linux, WebView2 on Windows). Closing the window stops the node.

ponytail: pywebview over the OS's built-in webview instead of a full Tauri project — same UX (native
window + auto-started sidecar node), ONE pip dep, and no Rust/Node toolchain to build or run. Upgrade
path: a signed ~5 MB Tauri .app when you want a double-click installer to hand out — that needs the
Rust toolchain this wrapper deliberately avoids.

Install:  pip install '.[desktop]'
Run:      python desktop/app.py                         # empty store; dashboard shows status/events
          python desktop/app.py -- --centralaizer http://127.0.0.1:3001 --federate-shared
Verify:   python desktop/app.py --selfcheck             # headless: proves the sidecar handshake
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request


def start_node(serve_args: list[str], timeout: float = 40.0):
    """Spawn `aimessage serve --control <serve_args>`; return (proc, dashboard_url) once it's up.

    Raises RuntimeError if the sidecar dies or never prints DASHBOARD= within `timeout`.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "aimessage", "serve", "--control", *serve_args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    # serve prints DASHBOARD=... with flush=True; read line-by-line until we see it.
    import time
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line == "":                                   # EOF → the sidecar exited
            rest = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"node exited before it was ready:\n{rest}")
        if line.startswith("DASHBOARD="):
            return proc, line.split("=", 1)[1].strip()
    proc.terminate()
    raise RuntimeError("timed out waiting for the node's DASHBOARD line")


def _token_from(url: str) -> str:
    return url.split("#", 1)[1] if "#" in url else ""


def _selfcheck() -> int:
    """Headless proof of the non-trivial glue: the sidecar really starts and its token-gated control
    API answers at the advertised URL. No GUI — safe for CI / a terminal session."""
    proc, url = start_node([])
    try:
        base, token = url.split("#", 1)[0].rstrip("/"), _token_from(url)
        assert token, "no token in DASHBOARD url"
        req = urllib.request.Request(f"{base}/status",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as r:   # noqa: S310 (localhost sidecar)
            import json
            body = json.loads(r.read())
        assert r.status == 200 and body.get("node_id"), f"bad /status: {body}"
        # token is actually enforced (no bearer → not 200)
        try:
            with urllib.request.urlopen(f"{base}/status", timeout=5):   # noqa: S310
                unauthorized_ok = False
        except urllib.error.HTTPError as e:
            unauthorized_ok = e.code == 401
        assert unauthorized_ok, "control API served /status without a token"
        print(f"PASS  sidecar up, dashboard at {url}")
        print(f"PASS  token-gated /status returned node_id={body['node_id'][:12]}...")
        print("PASS  missing-token request rejected (401)")
        print("\nALL GREEN — desktop sidecar handshake works.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in argv:
        return _selfcheck()
    serve_args = argv[argv.index("--") + 1:] if "--" in argv else []
    proc, url = start_node(serve_args)
    try:
        import webview                                     # optional dep: pip install '.[desktop]'
    except ImportError:
        proc.terminate()
        sys.exit("pywebview not installed — run: pip install '.[desktop]'\n"
                 f"(the node is up; you can also just open {url} in a browser)")
    try:
        webview.create_window("AIMessage", url, width=1100, height=760)
        webview.start()                                   # blocks until the window closes
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
