#!/usr/bin/env python3
"""Canonical Google Chat alert sender for NocAgent.

Posts rich cardsV2 messages to the NOC ticket webhook. Replaces the
deprecated cards v1 format that spaces increasingly render as plain text.

Modes
-----
  ticket   --ticket-id ID [--status needs_human] [standard fields]
  alert    --title "..." [--subtitle "..."] [--row "Key:value"]... \
           [--button "Label:url"]... [--severity needs_attention]

Usage from the agent (preferred over hand-rolling cards):

  python3 scripts/chat_alert.py ticket --ticket-id 12345 \
      --subject "(town) ap pppoe - slow tower" --priority HIGH \
      --source "Transferred to" \
      --issue "Unable to determine tower site from subject" \
      --reason "Subject does not match expected format" \
      --analysis "N/A (tower site unknown)" \
      --evidence "Site-name heuristic failed" --evidence "3 open tickets at town"

Only resource URLs that were verified with HTTP 200 are embedded; if a
header icon URL cannot be re-verified at send time it is dropped so the
card never ships with a blank image.

Env
---
  NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL  webhook URL (environment first, then the
                                       .env file via scripts/envfile.py; never
                                       passed on a command line)
  ORG_SONAR_BASE_URL                   overrides site-pack org.sonar_base_url
  CHAT_ICON_VERIFY                     set to '0' to skip icon URL re-verify
  NOTIFY_PROVIDER                      unset or gchat: the cardsV2 path below, unchanged.
                                       slack | discord | teams | ntfy | stdout: the same
                                       alert is sent as plain text through scripts/notify.py
                                       (URL from NOTIFY_WEBHOOK_URL; see that file)

--ticket-id must be an integer (it is embedded in a link). Button URLs must be
http(s). The webhook must be https:// (loopback http is allowed for tests).

Exit codes: 0 sent, 1 usage/build error, 2 HTTP/transport error, 4 webhook unconfigured
(also when NOTIFY_PROVIDER names another provider whose URL is not set).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import noc_config  # noqa: E402
import notify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Status metadata -> header icon + emoji accent
# Verified-200 icon sources (fonts.gstatic.com material icons, SVG):
#   warning  https://fonts.gstatic.com/s/i/materialicons/warning/v1/24px.svg
#   error    https://fonts.gstatic.com/s/i/materialicons/error/v1/24px.svg
#   check    https://fonts.gstatic.com/s/i/materialicons/check_circle/v1/24px.svg
#   copy     https://fonts.gstatic.com/s/i/materialicons/content_copy/v1/24px.svg
# (The old www.gstatic.com .../system/2x/*.png set is mostly 404 now.)
# ---------------------------------------------------------------------------
STATUS_GFX = {
    "needs_human": {
        "emoji": "\U0001f6a8",  # rotating siren
        "icon": "https://fonts.gstatic.com/s/i/materialicons/warning/v1/24px.svg",
        "alt": "Needs Attention",
    },
    "duplicate": {
        "emoji": "\U0001f5d2",  # clipboard
        "icon": "https://fonts.gstatic.com/s/i/materialicons/content_copy/v1/24px.svg",
        "alt": "Possible Duplicate",
    },
    "error": {
        "emoji": "\u274c",
        "icon": "https://fonts.gstatic.com/s/i/materialicons/error/v1/24px.svg",
        "alt": "Error",
    },
    "resolved": {
        "emoji": "\u2705",
        "icon": "https://fonts.gstatic.com/s/i/materialicons/check_circle/v1/24px.svg",
        "alt": "Resolved",
    },
}

_SEVERITY_TO_KEY = {
    "needs_attention": "needs_human",
    "needs_human": "needs_human",
    "warning": "needs_human",
    "info": "needs_human",
    "duplicate": "duplicate",
    "error": "error",
    "success": "resolved",
    "resolved": "resolved",
}

STATUS_TITLES = {
    "handled_duplicate": "Possible Duplicate",
    "categorized": "Categorized - needs attention",
    "needs_human": "Needs Human Review",
    "passthrough": "Passthrough",
    "auto_closed": "Auto-Closed",
    "auto_closed_ooo": "Auto-Closed (Out of Office)",
    "auto_closed_maintenance": "Auto-Closed (Maintenance)",
    "pbs_failure": "PBS Failure",
    "auto_bounced_ihn": "Auto-Bounced to CS (IHN)",
    "auto_bounced_ihn_peak": "Auto-Bounced to CS (IHN, peak)",
    "correlated_bounced": "Auto-Bounced to CS (Correlated)",
    "queued_for_peak": "Queued for Peak Retest",
}

_icon_cache: dict = {}


def load_webhook_url() -> str:
    return envfile.get("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL")


def sonar_base_url() -> str:
    override = os.environ.get("ORG_SONAR_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    base = noc_config.profile_get(None, ("org", "sonar_base_url"), "")
    return base.strip().rstrip("/") if isinstance(base, str) else ""


_TICKET_ID_RE = re.compile(r"^[0-9]{1,18}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def http_url_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url or "")
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def webhook_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url or "")
    if not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)


def verify_icon(url: str) -> str:
    """Return url if it actually serves (HTTP 200), else '' - avoids blank
    header squares when a hosted icon goes away (the gstatic 2x PNG set did).
    With CHAT_ICON_VERIFY=0 (offline/dev) we cannot verify liveness, so we
    return '' too: unverified URLs must never be embedded."""
    if os.environ.get("CHAT_ICON_VERIFY", "1").strip() == "0":
        return ""
    if url in _icon_cache:
        return _icon_cache[url]
    ok = url
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "NocAgent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 200:
                ok = ""
    except Exception:
        ok = ""
    _icon_cache[url] = ok
    return ok


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_flag_list(items) -> list:
    """Parse repeated 'Key:value' / 'Label:url' flags into tuples.
    URLs keep their '://' prefix; a value that starts with https:// is not
    split (partition on first ':' only when key has no http[s] inside)."""
    out = []
    for item in items or []:
        if ":" not in item:
            raise ValueError(f"Expected 'Key:value', got: {item}")
        key, sep, value = item.partition(":")
        out.append((key.strip(), value.strip() if "://" in sep + value else value.strip()))
    return out


def row(label: str, value: str) -> dict:
    """Bold-label text row. Plain textParagraph — no knownIcon games, those
    render as blank squares on several spaces."""
    return {"textParagraph": {"text": f"<b>{html_escape(label)}:</b> {value}"}}


def header_for(severity: str) -> dict:
    key = _SEVERITY_TO_KEY.get(severity.lower(), "needs_human")
    gfx = STATUS_GFX[key]
    h = {"title": f"{gfx['emoji']} ", "imageAltText": gfx["alt"], "imageType": "CIRCLE"}
    icon_url = verify_icon(gfx["icon"])
    if icon_url:
        h["imageUrl"] = icon_url
    return h


def build_ticket_card(**kw) -> dict:
    ticket_id = str(kw.get("ticket_id", "")).strip()
    if not ticket_id:
        raise ValueError("--ticket-id is required for ticket mode")
    if not _TICKET_ID_RE.match(ticket_id):
        raise ValueError(f"--ticket-id must be an integer, got {ticket_id!r}")

    status = (kw.get("status") or "").strip()
    status_label = STATUS_TITLES.get(status) or status
    severity = kw.get("severity") or ("error" if status == "error" else "needs_attention")
    h = header_for(severity)
    h["title"] += f"Ticket #{ticket_id} — {status_label}"
    sub_bits = []
    if kw.get("confidence") is not None:
        sub_bits.append(f"Confidence {kw['confidence']}%")
    subject = kw.get("subject") or "No subject"
    h["subtitle"] = (sub_bits[0] + " — " if sub_bits else "") + subject
    parity = "Created in" if kw.get("ticket_created") in ("true", "True", True, 1) else "Transferred to"

    widgets = [
        row("Source", f"{parity} {html_escape(kw.get('group') or 'NOC')} group"),
        row("Priority", html_escape(kw.get("priority") or "Medium")),
    ]
    if kw.get("issue"):
        widgets.append(row("Issue", kw["issue"]))
    if kw.get("reason"):
        widgets.append(row("Reason", kw["reason"]))
    if kw.get("analysis"):
        widgets.append(row("Analysis", kw["analysis"]))
    evidence = kw.get("evidence") or []
    if evidence:
        ev_lines = "<br>".join("• " + html_escape(e) for e in evidence)
        widgets.append(row("Evidence", ev_lines))

    sections = [{"widgets": widgets}]

    buttons = []
    base = sonar_base_url()
    if base:
        buttons.append({"text": "VIEW TICKET", "onClick": {"openLink": {"url": f"{base}/app#/tickets/show/{ticket_id}"}}})
    link = (kw.get("link") or "").strip()
    if link and not buttons:
        buttons.append({"text": "Open Ticket", "onClick": {"openLink": {"url": link}}})
    for label, url in parse_flag_list(kw.get("buttons")):
        if not http_url_ok(url):
            raise ValueError(f"button URL must be http(s), got {url!r}")
        buttons.append({"text": label, "onClick": {"openLink": {"url": url}}})
    if buttons:
        sections.append({"widgets": [{"buttonList": {"buttons": buttons}}]})

    desc_bits = [f"NOC ticket #{ticket_id}", status_label, subject]
    return {
        "cardsV2": [{"cardId": f"noc-ticket-{ticket_id}", "card": {"header": h, "sections": sections}}],
        "text": " — ".join(desc_bits),
    }


def build_alert_card(args) -> dict:
    if not args.title:
        raise ValueError("--title is required for alert mode")
    h = header_for(args.severity)
    h["title"] += args.title
    if args.subtitle:
        h["subtitle"] = args.subtitle

    sections = []
    widgets = [row(k, v) for k, v in parse_flag_list(args.rows)]
    if widgets:
        sections.append({"widgets": widgets})
    btns = []
    for label, url in parse_flag_list(args.buttons):
        if not http_url_ok(url):
            raise ValueError(f"button URL must be http(s), got {url!r}")
        btns.append({"text": label, "onClick": {"openLink": {"url": url}}})
    if btns:
        sections.append({"widgets": [{"buttonList": {"buttons": btns}}]})
    if args.note:
        sections.append({"widgets": [{"textParagraph": {"text": html_escape(args.note)}}]})

    card = {"header": h}
    if sections:
        card["sections"] = sections
    # stable across runs (str hash() is salted per process)
    card_id = "noc-alert-" + hashlib.sha256(args.title.encode("utf-8")).hexdigest()[:10]
    return {"cardsV2": [{"cardId": card_id, "card": card}]}


def send(payload: dict, webhook: str) -> None:
    """POST the card. Errors never include the webhook URL (it embeds key + token)."""
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f"non-200 from webhook: {r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"webhook HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"webhook unreachable: {getattr(e, 'reason', 'network error')}") from None
    except OSError as e:
        raise RuntimeError(f"webhook transport error: {type(e).__name__}") from None


# ---------------------------------------------------------------------------
# Other providers (NOTIFY_PROVIDER != gchat): the same alert as plain text
# ---------------------------------------------------------------------------
def _plain(text) -> str:
    """Chat card markup (<b>, <br>, entities) -> plain text for providers without cards."""
    s = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.I)
    return html.unescape(re.sub(r"</?[a-z][^>]*>", "", s, flags=re.I)).strip()


def _level_for(severity: str) -> str:
    sev = (severity or "").lower()
    if sev == "error":
        return "critical"
    if sev in ("info", "success", "resolved"):
        return "info"
    return "warn"


def plain_message(args) -> tuple:
    """(title, body, level, link) for a parsed ticket/alert command; ValueError on bad input."""
    if args.mode == "ticket":
        ticket_id = str(args.ticket_id or "").strip()
        if not _TICKET_ID_RE.match(ticket_id):
            raise ValueError(f"--ticket-id must be an integer, got {ticket_id!r}")
        status = (args.status or "").strip()
        label = STATUS_TITLES.get(status) or status
        severity = args.severity or ("error" if status == "error" else "needs_attention")
        parity = "Created in" if args.ticket_created in ("true", "True", True, 1) else "Transferred to"
        lines = [args.subject or "No subject",
                 f"Source: {parity} {args.group or 'NOC'} group",
                 f"Priority: {args.priority or 'Medium'}"]
        for key, value in (("Issue", args.issue), ("Reason", args.reason), ("Analysis", args.analysis)):
            if value:
                lines.append(f"{key}: {_plain(value)}")
        if args.confidence is not None:
            lines.append(f"Confidence: {args.confidence}%")
        if args.evidence:
            lines.append("Evidence:")
            lines.extend("- " + _plain(e) for e in args.evidence)
        base = sonar_base_url()
        link = f"{base}/app#/tickets/show/{ticket_id}" if base else (args.link or "")
        return f"Ticket #{ticket_id} - {label}", "\n".join(lines), _level_for(severity), link
    if args.mode == "alert":
        if not args.title:
            raise ValueError("--title is required for alert mode")
        lines = [args.subtitle] if args.subtitle else []
        lines.extend(f"{k}: {_plain(v)}" for k, v in parse_flag_list(args.rows))
        if args.note:
            lines.append(args.note)
        link = ""
        for _label, url in parse_flag_list(args.buttons):
            if not http_url_ok(url):
                raise ValueError(f"button URL must be http(s), got {url!r}")
            link = link or url
        return args.title, "\n".join(lines), _level_for(args.severity), link
    raise ValueError("mode required: ticket | alert (or --selftest)")


def _main_via_notify(args, provider: str, parser) -> int:
    """NOTIFY_PROVIDER names a provider other than gchat. Same CLI, same exit codes."""
    try:
        title, body, level, link = plain_message(args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        if args.mode not in ("ticket", "alert"):
            parser.print_help()
        return 1
    if args.dry_run:
        print(json.dumps({"provider": provider, "title": title, "body": body, "level": level, "link": link}, indent=2))
        return 0
    if not notify.configured(provider):
        if provider in notify.PROVIDERS:
            print(f"NOTIFY_PROVIDER={provider} but NOTIFY_WEBHOOK_URL is not set (env or .env)", file=sys.stderr)
        else:
            print("NOTIFY_PROVIDER is not a known provider (use: " + ", ".join(notify.PROVIDERS) + ")", file=sys.stderr)
        return 4
    ok, detail = notify.deliver(title, body, level=level, provider=provider, link=link)
    if not ok:
        print(f"send failed: provider={provider} {detail}", file=sys.stderr)
        return 2
    return 0


def _selftest() -> int:
    # Deterministic icon handling: with verification disabled, header must not ship imageUrl
    os.environ["CHAT_ICON_VERIFY"] = "0"
    c = build_ticket_card(ticket_id="1", status="needs_human", subject="x")
    hdr = c["cardsV2"][0]["card"]["header"]
    assert isinstance(c, dict) and "Needs Human Review" in hdr["title"]
    assert "sections" in c["cardsV2"][0]["card"]
    assert "imageUrl" not in hdr, "unverified-icon env (selftest) must drop imageUrl"
    sub = c["cardsV2"][0]["card"]["header"]["subtitle"]
    assert sub == "x"
    full = build_ticket_card(
        ticket_id="123",
        status="needs_human",
        confidence=72,
        subject="s",
        group="NOC",
        priority="HIGH",
        issue="i",
        reason="r",
        analysis="a",
        evidence=["e1", "e2"],
        ticket_created="false",
        severity="needs_attention",
    )
    widgets = full["cardsV2"][0]["card"]["sections"][0]["widgets"]
    assert widgets[2]["textParagraph"]["text"].startswith("<b>Issue:</b>")
    assert "<b>Evidence:</b>" in widgets[5]["textParagraph"]["text"]
    a = build_alert_card(type("NS", (), {
        "title": "t", "subtitle": None, "severity": "info", "note": "n",
        "rows": ["Key1:val1", "Key2:val2"], "buttons": ["B:https://example.com/x"],
    })())
    assert a["cardsV2"][0]["card"]["header"]["title"].endswith("t")
    assert len(a["cardsV2"][0]["card"]["sections"]) == 3  # rows + buttons + note
    # button URL must keep its scheme (https:// is 3 chars after colon)
    btns = a["cardsV2"][0]["card"]["sections"][1]["widgets"][0]["buttonList"]["buttons"]
    assert btns[0]["onClick"]["openLink"]["url"].startswith("https://")
    # a dead icon source must be dropped, not embedded as a blank square
    _icon_cache.clear()
    os.environ["CHAT_ICON_VERIFY"] = "1"
    assert verify_icon("https://example.invalid/dead.png") == ""
    os.environ["CHAT_ICON_VERIFY"] = "0"
    print("selftest passed")
    return 0


NOT_SET = object()


def main() -> int:
    p = argparse.ArgumentParser(description="NocAgent Google Chat alerts (cardsV2)")
    p.add_argument("--selftest", action="store_true")
    sub = p.add_subparsers(dest="mode")

    t = sub.add_parser("ticket", help="rich ticket alert")
    t.add_argument("--ticket-id", required=True)
    t.add_argument("--status", default="needs_human")
    t.add_argument("--subject", default=None)
    t.add_argument("--group", default=None)
    t.add_argument("--priority", default=None)
    t.add_argument("--ticket-created", default=None, help="true|false (uses provided group details)")
    t.add_argument("--issue", default=None)
    t.add_argument("--reason", default=None)
    t.add_argument("--analysis", default=None)
    t.add_argument("--evidence", action="append", default=[], help="evidence line; repeatable")
    t.add_argument("--confidence", type=int, default=None)
    t.add_argument("--severity", default="needs_attention")
    t.add_argument("--button", action="append", default=[], help="Label:url; repeatable")
    t.add_argument("--link", default=None, help="url for default button if no site-pack org URL known")
    t.add_argument("--dry-run", action="store_true", help="print payload, don't send")

    a = sub.add_parser("alert", help="generic operational alert (breaker, fallback, weekly notes)")
    a.add_argument("--title", required=True)
    a.add_argument("--subtitle", default=None)
    a.add_argument("--severity", default="needs_attention")
    a.add_argument("--row", dest="rows", action="append", default=[], help="Key:value; repeatable")
    a.add_argument("--button", dest="buttons", action="append", default=[], help="Label:url; repeatable")
    a.add_argument("--note", default=None)
    a.add_argument("--dry-run", action="store_true", help="print payload, don't send")

    args = p.parse_args()
    if args.selftest:
        return _selftest()

    provider = notify.provider_name()
    if provider != "gchat":
        return _main_via_notify(args, provider, p)

    webhook = load_webhook_url()
    if not webhook:
        print("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL not set (env or .env)", file=sys.stderr)
        return 4
    if not webhook_ok(webhook):
        print("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL must be an https:// URL", file=sys.stderr)
        return 4

    try:
        if args.mode == "ticket":
            payload = build_ticket_card(
                ticket_id=args.ticket_id,
                status=args.status,
                subject=args.subject,
                group=args.group,
                priority=args.priority,
                issue=args.issue,
                reason=args.reason,
                analysis=args.analysis,
                evidence=args.evidence,
                confidence=args.confidence,
                ticket_created=args.ticket_created,
                severity=args.severity,
                buttons=args.button,
                link=args.link,
            )
        elif args.mode == "alert":
            payload = build_alert_card(args)
        else:
            print("mode required: ticket | alert (or --selftest)", file=sys.stderr)
            p.print_help()
            return 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    try:
        send(payload, webhook)
    except RuntimeError as e:
        print(f"send failed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
