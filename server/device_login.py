#!/usr/bin/env python3
"""Mint a dedicated OAuth refresh token for the usage display.

`claude setup-token` produces an inference-only token that CANNOT read usage -
it lacks the `user:profile` scope the usage endpoint requires, so it 403s. This
runs the same browser login flow Claude Code uses, requesting user:profile, and
prints a refresh token to paste into firmware/src/config.h as
DEVICE_REFRESH_TOKEN.

It's a separate authorization from your everyday Claude Code session, so the
device refreshing this token never disturbs your normal login. The device turns
the refresh token into short-lived access tokens on its own.

    python3 device_login.py

Then open the printed URL, approve, and paste the code it shows you back here.
No third-party dependencies.
"""

import base64
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public OAuth client
AUTH_URL  = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT  = "https://console.anthropic.com/oauth/code/callback"
SCOPES    = "org:create_api_key user:profile user:inference"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The token endpoint sits behind Cloudflare, which 1010-blocks the default
# "Python-urllib" User-Agent. Any real UA gets through.
USER_AGENT = "claude-usage-display/1.0"


def _ssl_context():
    """An SSL context with a usable CA store, even on bundled Pythons.

    PlatformIO's portable Python (often first on PATH in the PlatformIO/VS Code
    terminal) ships with its default CA path pointing at the build machine, so
    every HTTPS request fails with CERTIFICATE_VERIFY_FAILED - and it may not
    have certifi either. When the default store comes up empty, try certifi,
    then well-known CA bundles on disk. A normal system Python is unaffected.
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
        "/opt/homebrew/etc/openssl@3/cert.pem",      # Homebrew (Apple Silicon)
        "/opt/homebrew/etc/ca-certificates/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",         # Homebrew (Intel)
        "/etc/ssl/cert.pem",                         # macOS / BSD system bundle
        "/etc/ssl/certs/ca-certificates.crt",        # Linux
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


def main():
    verifier  = b64url(os.urandom(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state     = b64url(os.urandom(32))

    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    print("\n1. Open this URL in your browser and approve access:\n")
    print("   " + url + "\n")
    print("2. You'll be redirected to a page showing a code (like  <code>#<state>).")
    raw = input("3. Paste the code here: ").strip()
    if not raw:
        print("no code entered", file=sys.stderr)
        sys.exit(1)

    code = raw.split("#", 1)[0].strip()
    ret_state = raw.split("#", 1)[1].strip() if "#" in raw else state

    body = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "state": ret_state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        tok = json.load(urllib.request.urlopen(req, timeout=30, context=SSL_CTX))
    except urllib.error.HTTPError as e:
        print("\nToken exchange failed:", e.code, e.read().decode()[:400], file=sys.stderr)
        print("Double-check you pasted the whole code, then try again.", file=sys.stderr)
        sys.exit(1)

    access  = tok.get("access_token", "")
    refresh = tok.get("refresh_token", "")
    scope   = tok.get("scope", "")

    print("\n--- login succeeded ---")
    print("scopes granted:", scope or "(none reported)")
    if "user:profile" not in scope:
        print("WARNING: user:profile was NOT granted - usage reads will still 403.")

    # Prove the token actually works against the usage endpoint before flashing.
    ureq = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + access,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        urllib.request.urlopen(ureq, timeout=15, context=SSL_CTX).read()
        print("usage endpoint check: 200 OK - this token can read usage.")
    except urllib.error.HTTPError as e:
        print(f"usage endpoint check: {e.code} -", e.read().decode()[:200])

    if not refresh:
        print("\nNo refresh token returned - cannot continue.", file=sys.stderr)
        sys.exit(1)

    print("\nPaste this line into firmware/src/config.h (replacing the existing one):\n")
    print(f'    #define DEVICE_REFRESH_TOKEN "{refresh}"\n')
    print("Then flash:  cd firmware && pio run -t upload")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
