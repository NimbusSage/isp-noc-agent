#!/usr/bin/env python3
"""Provider-neutral notifications for NocAgent (stdlib only).

Google Chat stays the default. Everything else is opt-in with two variables:

  NOTIFY_PROVIDER    gchat (default) | slack | discord | teams | ntfy | stdout
  NOTIFY_WEBHOOK_URL webhook / topic URL of the chosen provider. For gchat the
                     existing NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL is used when
                     this is unset, so current installs need no change.
  NOTIFY_NTFY_TOKEN  optional ntfy access token (sent as a bearer token)
  NOTIFY_ALLOW_HTTP  set to 1 to allow a plain http:// webhook (e.g. a LAN ntfy)

Both values are read from the environment first, then the repo .env via
scripts/envfile.py (never through a shell).

Providers
---------
  gchat    the cardsV2 card from scripts/chat_alert.py (byte-identical to today)
  slack    incoming webhook: {"text": ...} (&, <, > escaped so a body cannot
           open a <!channel> mention or a link)
  discord  webhook: {"content": ...}, 2000 characters at most, mentions disabled
  teams    {"text": ...}. UNVERIFIED: Microsoft Teams "Workflows" webhooks may
           require an Adaptive Card instead of plain text; the legacy Office 365
           connector accepts this shape. Test it before relying on it.
  ntfy     the body is POSTed to the topic URL with Title / Priority / Click
           headers and an optional Authorization: Bearer header
  stdout   prints the message (local runs, tests)

Rules: https only (plain http is allowed for loopback, or with NOTIFY_ALLOW_HTTP=1);
10 second timeout; one retry on a network error; redirects are never followed
(an Authorization header must not follow a redirect); control characters are
stripped and the text is capped per provider. The webhook URL and the token are
secrets: neither is ever printed or logged, only the provider name and the HTTP
status. send() never raises.

CLI
---
  python3 scripts/notify.py --title T --body B [--level info|warn|critical] \
      [--provider P] [--link URL]
  python3 scripts/notify.py --selftest

Exit codes: 0 delivered, 1 not delivered (unconfigured, refused, network or HTTP
error), 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

PROVIDERS = ("gchat", "slack", "discord", "teams", "ntfy", "stdout")
LEVELS = ("info", "warn", "critical")
TIMEOUT = 10
RETRY_DELAY = 0.5
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Per-provider cap on the message text (ntfy counts bytes: its limit is 4096 bytes).
LIMITS = {"gchat": 4000, "slack": 4000, "discord": 2000, "teams": 4000, "ntfy": 4000, "stdout": 20000}
TITLE_LIMIT = 200
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_NTFY_PRIORITY = {"info": "default", "warn": "high", "critical": "urgent"}
_LEVEL_LABEL = {"info": "", "warn": "[WARN] ", "critical": "[CRITICAL] "}
_CHAT_SEVERITY = {"info": "info", "warn": "needs_attention", "critical": "error"}


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------
def clean(text, *, single_line: bool = False) -> str:
    """Remove control characters (newline and tab survive unless single_line)."""
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    s = _CONTROL_RE.sub("", s)
    if single_line:
        s = re.sub(r"[\n\t]+", " ", s)
    return s.strip()


def truncate(text: str, limit: int, *, by_bytes: bool = False) -> str:
    """Cut to `limit` characters (or UTF-8 bytes), ending in an ellipsis when cut."""
    if by_bytes:
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text
        return raw[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "..."
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _http_link(link) -> str:
    """The link if it is a plain http(s) URL, else '' (it ends up in a button or a header)."""
    s = clean(link, single_line=True)
    parts = urllib.parse.urlsplit(s)
    return s if parts.scheme in ("http", "https") and parts.hostname else ""


def _compose(title: str, body: str, level: str, link: str, limit: int, *, by_bytes: bool = False) -> str:
    text = _LEVEL_LABEL[level] + title
    if body:
        text += "\n" + body
    if link:
        text += "\n" + link
    return truncate(text, limit, by_bytes=by_bytes)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def provider_name(explicit: Optional[str] = None) -> str:
    """The selected provider: explicit argument, else NOTIFY_PROVIDER, else gchat."""
    name = (explicit or envfile.get("NOTIFY_PROVIDER") or "gchat")
    return name.strip().lower()


def webhook_url(provider: str) -> str:
    """The configured URL for `provider` ('' for stdout or when unset)."""
    if provider == "stdout":
        return ""
    url = envfile.get("NOTIFY_WEBHOOK_URL")
    if not url and provider == "gchat":
        url = envfile.get("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL")
    return url


def configured(provider: Optional[str] = None) -> bool:
    p = provider_name(provider)
    return p in PROVIDERS and (p == "stdout" or bool(webhook_url(p)))


def url_allowed(url: str) -> bool:
    """https always; http only for loopback or when NOTIFY_ALLOW_HTTP=1."""
    parts = urllib.parse.urlsplit(url or "")
    if not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return parts.hostname in LOOPBACK or envfile.get("NOTIFY_ALLOW_HTTP") == "1"
    return False


# ---------------------------------------------------------------------------
# request builders: (body bytes, headers)
# ---------------------------------------------------------------------------
def _json(payload: dict) -> Tuple[bytes, Dict[str, str]]:
    return json.dumps(payload).encode(), {"Content-Type": "application/json"}


def _build_gchat(title, body, level, link, token):
    import chat_alert  # lazy: chat_alert imports this module

    ns = type("Alert", (), {
        "title": truncate(title, TITLE_LIMIT),
        "subtitle": None,
        "severity": _CHAT_SEVERITY[level],
        "rows": [],
        "buttons": [f"Open:{link}"] if link else [],
        "note": truncate(body, LIMITS["gchat"]) or None,
    })()
    return _json(chat_alert.build_alert_card(ns))


def _build_slack(title, body, level, link, token):
    text = _compose(title, body, level, link, LIMITS["slack"])
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _json({"text": text})


def _build_discord(title, body, level, link, token):
    return _json({"content": _compose(title, body, level, link, LIMITS["discord"]),
                  "allowed_mentions": {"parse": []}})


def _build_teams(title, body, level, link, token):
    return _json({"text": _compose(title, body, level, link, LIMITS["teams"])})


def _header_text(value: str) -> str:
    """ASCII passes through; anything else becomes an RFC 2047 encoded word (ntfy decodes it)."""
    if all(ord(c) < 128 for c in value):
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _build_ntfy(title, body, level, link, token):
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": _header_text(truncate(title, TITLE_LIMIT)),
        "Priority": _NTFY_PRIORITY[level],
    }
    if link:
        headers["Click"] = link
    token = re.sub(r"[^\x21-\x7e]", "", token or "")
    if token:
        headers["Authorization"] = "Bearer " + token
    message = truncate(body or title, LIMITS["ntfy"], by_bytes=True)
    return message.encode("utf-8"), headers


_BUILDERS = {"gchat": _build_gchat, "slack": _build_slack, "discord": _build_discord,
             "teams": _build_teams, "ntfy": _build_ntfy}


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a 3xx surfaces as an HTTPError
        return None


def _post(url: str, data: bytes, headers: Dict[str, str]) -> Tuple[int, str]:
    """(HTTP status, "") on any HTTP answer, (0, exception class name) on a network error."""
    handlers = [_NoRedirect()]
    if (urllib.parse.urlsplit(url).hostname or "") in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))  # never proxy a local server
    opener = urllib.request.build_opener(*handlers)
    headers = dict(headers)
    headers.setdefault("User-Agent", "NocAgent/1.0")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, ""
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return 0, type(e).__name__  # class name only: messages can carry the host


def deliver(title, body="", *, level: str = "info", provider: Optional[str] = None,
            link: Optional[str] = None, webhook: Optional[str] = None,
            ntfy_token: Optional[str] = None) -> Tuple[bool, str]:
    """Send one message. Returns (ok, short detail); the detail never holds the URL or token.

    `webhook` / `ntfy_token` override the configured values (used by doctor.py,
    which has its own env view). Never raises.
    """
    name = "?"
    try:
        name = provider_name(provider)
        if name not in PROVIDERS:
            return False, "unknown provider (use: " + ", ".join(PROVIDERS) + ")"
        level = level if level in LEVELS else "info"
        title = truncate(clean(title, single_line=True), TITLE_LIMIT) or "NocAgent"
        body = truncate(clean(body), LIMITS[name], by_bytes=False)
        link = _http_link(link)
        if name == "stdout":
            print(_compose(title, body, level, link, LIMITS["stdout"]))
            return True, "printed"
        url = (webhook or webhook_url(name) or "").strip()
        if not url:
            need = "NOTIFY_WEBHOOK_URL" + (" or NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL" if name == "gchat" else "")
            return False, f"not configured (set {need})"
        if not url_allowed(url):
            return False, "refused: the webhook must be an https:// URL (http only for loopback or NOTIFY_ALLOW_HTTP=1)"
        token = ntfy_token if ntfy_token is not None else envfile.get("NOTIFY_NTFY_TOKEN")
        data, headers = _BUILDERS[name](title, body, level, link, token)
        status, err = _post(url, data, headers)
        if status == 0:
            time.sleep(RETRY_DELAY)
            status, err = _post(url, data, headers)
        if status == 0:
            return False, f"network error ({err})"
        return (200 <= status < 300), f"HTTP {status}"
    except Exception as e:  # a notification failure must never break the caller
        return False, f"internal error ({type(e).__name__})"


def send(title, body="", *, level: str = "info", provider: Optional[str] = None,
         link: Optional[str] = None) -> bool:
    """Send one notification; True when the provider accepted it. Logs provider + status only."""
    ok, detail = deliver(title, body, level=level, provider=provider, link=link)
    try:
        name = provider_name(provider)
    except Exception:
        name = "?"
    if not (ok and detail == "printed"):
        print(f"notify: provider={name} {'ok' if ok else 'FAILED'} {detail}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# selftest (local http.server, no network)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, dict(self.headers), self.rfile.read(n)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    saved = {k: os.environ.get(k) for k in ("NOTIFY_PROVIDER", "NOTIFY_WEBHOOK_URL", "NOTIFY_NTFY_TOKEN",
                                            "NOTIFY_ALLOW_HTTP", "NOC_DOTENV", "CHAT_ICON_VERIFY")}
    try:
        os.environ["NOC_DOTENV"] = os.devnull
        os.environ["CHAT_ICON_VERIFY"] = "0"
        for k in ("NOTIFY_PROVIDER", "NOTIFY_NTFY_TOKEN", "NOTIFY_ALLOW_HTTP"):
            os.environ.pop(k, None)
        os.environ["NOTIFY_WEBHOOK_URL"] = base + "/hook"
        assert deliver("t", "b", provider="slack") == (True, "HTTP 200")
        assert json.loads(seen[-1][2]) == {"text": "t\nb"} and seen[-1][0] == "/hook"
        assert deliver("t", "x" * 5000, provider="discord")[0]
        assert len(json.loads(seen[-1][2])["content"]) == LIMITS["discord"]
        os.environ["NOTIFY_NTFY_TOKEN"] = "selftest-token"
        assert deliver("t", "b", level="critical", provider="ntfy")[0]
        _, hdrs, raw = seen[-1]
        assert raw == b"b" and hdrs["Priority"] == "urgent" and hdrs["Authorization"] == "Bearer selftest-token"
        assert deliver("t", "b", provider="gchat")[0] and "cardsV2" in json.loads(seen[-1][2])
        ok, detail = deliver("t", "b", provider="slack", webhook="http://example.com/hook")
        assert not ok and detail.startswith("refused")
        assert deliver("t", "b", provider="nope")[0] is False
    finally:
        srv.shutdown()
        srv.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("selftest passed")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Send one NocAgent notification through the configured provider")
    p.add_argument("--title")
    p.add_argument("--body", default="")
    p.add_argument("--level", choices=LEVELS, default="info")
    p.add_argument("--provider", default=None, help="override NOTIFY_PROVIDER: " + ", ".join(PROVIDERS))
    p.add_argument("--link", default=None, help="optional http(s) link shown with the message")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.title:
        p.error("--title is required")
    return 0 if send(args.title, args.body, level=args.level, provider=args.provider, link=args.link) else 1


if __name__ == "__main__":
    sys.exit(main())
