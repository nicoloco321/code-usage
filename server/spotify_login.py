#!/usr/bin/env python3
"""Mint a Spotify refresh token for the display's "now playing" mode.

The display can switch between Claude usage and a Spotify now-playing screen
(the /switch Claude Code command, or POST /mode/spotify). For that it needs its
own Spotify authorization, minted once here - the device turns it into
short-lived access tokens on its own, exactly like the Anthropic login.

One-time Spotify app setup (free, any account):

1. Go to https://developer.spotify.com/dashboard and create an app
   (any name/description).
2. In the app's settings, add this EXACT Redirect URI:

       http://127.0.0.1:8898/callback

   and tick "Web API". You do NOT need the client secret - this uses PKCE.
3. Run:

       python3 spotify_login.py
       python3 spotify_login.py --client-id <id>   # or paste it when prompted
       python3 spotify_login.py --config ~/.config/claude-display/config.ini   # Raspberry Pi app

Approve access in the browser; this prints the two #define lines to paste into
firmware/src/config.h - or, with --config, saves them into the Raspberry Pi
app's config.ini. No third-party dependencies.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL  = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
NOW_URL   = "https://api.spotify.com/v1/me/player/currently-playing"
SCOPES    = "user-read-currently-playing user-read-playback-state"
PORT      = 8898  # must match the Redirect URI registered on the Spotify app
REDIRECT  = f"http://127.0.0.1:{PORT}/callback"


def _ssl_context():
    """An SSL context with a usable CA store, even on bundled Pythons.

    Same story as device_login.py: PlatformIO's portable Python ships with a
    broken default CA path, so fall back to certifi or a system bundle when the
    default store comes up empty.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509", 0) > 0:
        return ctx
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        if ctx.cert_store_stats().get("x509", 0) > 0:
            return ctx
    except Exception:
        pass
    for path in (
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/ca-certificates/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        try:
            if os.path.exists(path):
                ctx.load_verify_locations(path)
                if ctx.cert_store_stats().get("x509", 0) > 0:
                    return ctx
        except Exception:
            pass
    return ctx


SSL_CTX = _ssl_context()


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def save_to_config(path, section, values):
    """Set `key = value` under [section] of an INI file (the Raspberry Pi app's
    config.ini), leaving its comments and everything else as they are."""
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    pending, out, current = dict(values), [], None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if current == section:  # leaving our section: add any keys it lacked
                out.extend(f"{k} = {v}" for k, v in pending.items())
                pending.clear()
            current = s[1:-1].strip()
        elif current == section and "=" in s and s[0] not in "#;":
            key = s.split("=", 1)[0].strip()
            if key in pending:
                line = f"{key} = {pending.pop(key)}"
        out.append(line)
    if pending:
        if current != section:
            out += ([""] if out else []) + [f"[{section}]"]
        out.extend(f"{k} = {v}" for k, v in pending.items())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


class _Callback(http.server.BaseHTTPRequestHandler):
    """Catches Spotify's redirect back to 127.0.0.1 and stashes the code."""
    result = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        _Callback.result = {k: v[0] for k, v in
                            urllib.parse.parse_qs(parsed.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Login captured &mdash; you can close this tab "
                         b"and go back to the terminal.</h2>")

    def log_message(self, *args):
        pass


def wait_for_code():
    try:
        srv = http.server.HTTPServer(("127.0.0.1", PORT), _Callback)
    except OSError as e:
        print(f"Could not listen on 127.0.0.1:{PORT} ({e}).", file=sys.stderr)
        print("Close whatever is using that port and rerun.", file=sys.stderr)
        sys.exit(1)
    srv.timeout = 1
    with srv:
        while _Callback.result is None:
            srv.handle_request()
    return _Callback.result


def main():
    ap = argparse.ArgumentParser(description="Mint a Spotify refresh token for the display.")
    ap.add_argument("--client-id", help="Client ID from your Spotify app's dashboard page")
    ap.add_argument("--config", metavar="PATH",
                    help="save both values into this Raspberry Pi config.ini instead of "
                         "printing #defines for config.h")
    args = ap.parse_args()

    client_id = args.client_id or input(
        "Client ID (from https://developer.spotify.com/dashboard -> your app): ").strip()
    if not client_id:
        print("no client id entered", file=sys.stderr)
        sys.exit(1)

    verifier  = b64url(os.urandom(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state     = b64url(os.urandom(16))

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })

    print("\nOpening the Spotify approval page (or open this URL yourself):\n")
    print("   " + url + "\n")
    print(f"Reminder: the app's settings must list  {REDIRECT}  as a Redirect URI.")
    print("Waiting for the browser to come back...")
    webbrowser.open(url)

    result = wait_for_code()
    if "error" in result:
        print("\nSpotify returned an error:", result["error"], file=sys.stderr)
        sys.exit(1)
    if result.get("state") != state:
        print("\nState mismatch - stale or foreign callback; rerun and try again.",
              file=sys.stderr)
        sys.exit(1)

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        tok = json.load(urllib.request.urlopen(req, timeout=30, context=SSL_CTX))
    except urllib.error.HTTPError as e:
        print("\nToken exchange failed:", e.code, e.read().decode()[:400], file=sys.stderr)
        sys.exit(1)

    access  = tok.get("access_token", "")
    refresh = tok.get("refresh_token", "")
    scope   = tok.get("scope", "")

    print("\n--- login succeeded ---")
    print("scopes granted:", scope or "(none reported)")
    if "user-read-currently-playing" not in scope:
        print("WARNING: user-read-currently-playing was NOT granted - "
              "the now-playing fetch will 403.")

    # Prove the token works before flashing. 204 = authorized, nothing playing.
    ureq = urllib.request.Request(NOW_URL, headers={"Authorization": "Bearer " + access})
    try:
        with urllib.request.urlopen(ureq, timeout=15, context=SSL_CTX) as resp:
            if resp.status == 204:
                print("now-playing check: OK (nothing playing right now - that's fine).")
            else:
                data = json.loads(resp.read() or b"{}")
                item = data.get("item") or {}
                print("now-playing check: OK -", item.get("name", "(unknown track)"))
    except urllib.error.HTTPError as e:
        print(f"now-playing check: {e.code} -", e.read().decode()[:200])
        if e.code == 403:
            print("A 403 usually means this Spotify account isn't added to the app: "
                  "Dashboard -> your app -> User Management.", file=sys.stderr)

    if not refresh:
        print("\nNo refresh token returned - cannot continue.", file=sys.stderr)
        sys.exit(1)

    if args.config:
        save_to_config(args.config, "spotify", {"client_id": client_id, "refresh_token": refresh})
        print(f"\nSaved client_id and refresh_token under [spotify] in {args.config}.")
        print("Restart the display to use it: sudo systemctl restart claude-display (or reboot)")
        return

    print("\nPaste these lines into firmware/src/config.h (replacing the empty ones):\n")
    print(f'    #define SPOTIFY_CLIENT_ID     "{client_id}"')
    print(f'    #define SPOTIFY_REFRESH_TOKEN "{refresh}"\n')
    print("Then flash:  cd firmware && pio run -t upload")
    print("Switch the screen:  curl -X POST http://claude-display.local:8080/mode/spotify")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
