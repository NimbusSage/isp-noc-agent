#!/usr/bin/env python3
"""
demo_agent_stub.py - deterministic stand-in for the NOC agent, for demo mode.

Use it as the agent harness of server/index.js:
    AGENT_HARNESS=command
    AGENT_COMMAND_TEMPLATE='python3 -B scripts/demo_agent_stub.py {prompt}'

It reads the batch prompt (argv[1] as text, or argv[1] as a path to a file holding the prompt, or
stdin when argv[1] is '-' or absent), finds the tickets of the batch (batch_file named in the prompt,
else '#<id>' lines, else 'ticket <id>'), and for each ticket answers with the canned output in
scripts/demo/fixtures/agent_outputs/<id>.json (the batch output contract of
server/batch_prompt_template.md). The side effects that answer implies are then carried out against
the mock Sonar, using the real helper scripts where the real agent would:

    comments           scripts/sonar_comment.py --verify
    bounces            scripts/sonar_bounce.py bounce ... (circuit breaker, kill switch, anti-loop)
    notifications      scripts/chat_alert.py ticket ... (to the mock's chat sink)
    close / category   updateTicket over the same GraphQL endpoint

Bounce mode (DEMO_BOUNCE_MODE or --bounce-mode):
    dry   (default) run the real gate with --dry-run: every gate is evaluated and the mutation is
          printed, but nothing is written. The ticket is reported as needs_human with reason
          dry_run_would_bounce, because nothing was bounced.
    live  run the gate for real. Only honoured against the mock (see the sandbox guard below).
A gate that refuses (exit 3 breaker, 4 kill switch, 5 anti-loop) turns the ticket into needs_human with
the matching escalation_reason, exactly as the batch template tells the agent to.

Sandbox guard: the stub exits with code 2 before doing anything unless SONAR_BASE_URL is a loopback
URL, SONAR_TOKEN starts with 'demo-', and any chat webhook URL is loopback too. It can only talk to
the mock. (The server's agent environment is an allowlist that drops any *WEBHOOK* name, so the demo
hands the chat URL over as DEMO_CHAT_URL through AGENT_ENV_PASSTHROUGH=DEMO_*.)

Manual use against a running mock (scripts/mock_sonar.py):
    SONAR_BASE_URL=http://127.0.0.1:PORT SONAR_TOKEN=demo-... \\
      python3 scripts/demo_agent_stub.py --tickets 91001,91003 --bounce-mode live

Output: the JSON array of results is written atomically to the result_file named in the prompt (when
that path is inside NOC_LOG_DIR / NOC_AGENT_LOG_DIR) and printed as the final stdout line. Progress goes to stderr.
Python 3.9+ standard library only.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_FIXTURES = SCRIPTS / "demo" / "fixtures"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
HEALTH_COLORS = {"ok": "#2e7d32", "warn": "#ef6c00", "critical": "#c62828"}

GATE_EXIT_REASONS = {
    3: "circuit_breaker_open",
    4: "kill_switch_off",
    5: "anti_loop_marker",
}

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str) -> None:
    print(f"[demo-agent] {msg}", file=sys.stderr, flush=True)


class SandboxError(Exception):
    pass


class StubError(Exception):
    pass


def _is_loopback_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def check_sandbox(env: Dict[str, str]) -> None:
    base = env.get("SONAR_BASE_URL", "").strip()
    if not base or not _is_loopback_url(base):
        raise SandboxError("SONAR_BASE_URL must be a loopback URL (the demo mock); refusing to run")
    if not env.get("SONAR_TOKEN", "").startswith("demo-"):
        raise SandboxError("SONAR_TOKEN must be a demo token (prefix 'demo-'); refusing to run")
    chat = (env.get("DEMO_CHAT_URL") or env.get("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL") or "").strip()
    if chat and not _is_loopback_url(chat):
        raise SandboxError("the chat webhook URL (DEMO_CHAT_URL) must be a loopback URL; refusing to run")


class Sonar:
    """Minimal GraphQL client for the mock (same wire shapes the real scripts send)."""

    def __init__(self, base: str, token: str) -> None:
        self.url = base.rstrip("/") + "/api/graphql"
        self.token = token

    def gql(self, query: str) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps({"query": query}).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with _OPENER.open(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StubError(f"Sonar HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise StubError(f"Sonar request failed: {exc}") from exc
        if body.get("errors"):
            raise StubError("Sonar GraphQL error: " + str(body["errors"][0].get("message")))
        return body.get("data") or {}

    def get_ticket(self, tid: int) -> Optional[dict]:
        data = self.gql(
            '{ tickets(search: { integer_fields: [ { attribute: "id", operator: EQ, search_value: %d } ] }) '
            '{ entities { id subject status priority description ticket_group { id name } '
            'ticket_categories { entities { id name } } } } }' % tid)
        ents = (data.get("tickets") or {}).get("entities") or []
        return ents[0] if ents else None

    def update_ticket(self, tid: int, group_id: Optional[int] = None, status: Optional[str] = None,
                      category_ids: Optional[List[int]] = None) -> dict:
        parts = []
        if group_id is not None:
            parts.append(f"ticket_group_id: {int(group_id)}")
        if status is not None:
            if not re.fullmatch(r"[A-Z_]+", status):
                raise StubError(f"bad status {status!r}")
            parts.append(f"status: {status}")
        if category_ids is not None:
            parts.append("ticket_category_ids: [%s]" % ", ".join(str(int(c)) for c in category_ids))
        data = self.gql(
            "mutation { updateTicket(id: %d, input: { %s }) { id status ticket_group { id name } "
            "ticket_categories { entities { id name } } } }" % (int(tid), ", ".join(parts)))
        updated = data.get("updateTicket")
        if not updated or not updated.get("id"):
            raise StubError("updateTicket returned no ticket")
        return updated


class Context:
    def __init__(self, fixtures: Path, bounce_mode: str, env: Dict[str, str]) -> None:
        self.fixtures = fixtures
        self.bounce_mode = bounce_mode
        self.env = env
        self.sonar = Sonar(env["SONAR_BASE_URL"], env["SONAR_TOKEN"])
        profile = json.loads((fixtures / "profile.demo.json").read_text())["sonar"]
        self.groups = {k: int(v["id"]) for k, v in profile["groups"].items()}
        self.categories = {k: int(v["id"]) for k, v in profile["categories"].items()}
        plans_path = fixtures / "plans.json"
        self.plans = json.loads(plans_path.read_text()) if plans_path.exists() else {}
        self.tmp = tempfile.TemporaryDirectory(prefix="demo-agent-")
        self.child_env = self._child_env()

    def _child_env(self) -> Dict[str, str]:
        env = {k: v for k, v in self.env.items() if "proxy" not in k.lower()}
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["CHAT_ICON_VERIFY"] = "0"
        env["ORG_SONAR_BASE_URL"] = env["SONAR_BASE_URL"]
        # Group/category ids come from the (synthetic) profile; the ops scripts have no built-in fallbacks.
        env.setdefault("NOC_PROFILE_FILE", str(self.fixtures / "profile.demo.json"))
        chat = env.get("DEMO_CHAT_URL")
        if chat:
            env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = chat
        env.setdefault("NOC_DOTENV", os.path.join(self.tmp.name, "absent.env"))
        if not (env.get("NOC_STATE_DIR") or env.get("NOC_LOG_DIR") or env.get("NOC_AGENT_LOG_DIR")):
            # Standalone use: keep decision-log/breaker writes out of the repository's logs/.
            env["NOC_STATE_DIR"] = os.path.join(self.tmp.name, "state")
            log(f"NOC_STATE_DIR not set; using {env['NOC_STATE_DIR']}")
        return env

    def run(self, argv: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B"] + argv, capture_output=True, text=True,
                              env=self.child_env, cwd=self.tmp.name, timeout=timeout)

    def close(self) -> None:
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# HTML builders (sonar_comment.py rules: no raw double quotes, single line, >= 50 chars)
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def path_comment(tower: str, path: dict, extra: str = "") -> str:
    rows = []
    for hop in path["hops"]:
        color = HEALTH_COLORS.get(hop.get("level", "ok"), HEALTH_COLORS["ok"])
        rows.append(f"<li><span style=&quot;color:{color}&quot;>{_esc(hop['name'])} {_esc(hop['ip'])}</span>"
                    f" - {_esc(hop['note'])}</li>")
    return (f"<p><strong>Direct Path Analysis - {_esc(tower)}</strong></p><!-- path-analyzer -->"
            f"<p>Status: <strong>{_esc(path['verdict'])}</strong> across {len(path['hops'])} hops.</p>"
            f"<ul>{''.join(rows)}</ul>{extra}"
            "<p><em>Synthetic demo data. No real network was probed.</em></p>")


def ihn_block(confidence: int) -> str:
    return ("<p><strong>Automated NOC Review - No Network Issue</strong></p>"
            "<p>Every hop on the path is healthy, so the slowdown is on the customer or tower-host side. "
            f"Returned to CS Level 2. Tower confidence: {int(confidence)}%.</p>")


def weather_block(keywords: List[str]) -> str:
    return ("<p>The path is healthy, but weather language was found on the ticket "
            f"({_esc(', '.join(keywords))}). Not auto-bouncing per the weather policy; left for a NOC tech.</p>")


def duplicate_comment(base: str, original: int, confidence: int) -> str:
    link = f"{_esc(base)}/app#/tickets/show/{int(original)}"
    return ("<p><strong>Duplicate Ticket</strong></p>"
            f"<p>This ticket duplicates <a href=&quot;{link}&quot;>#{int(original)}</a> "
            f"(same site and issue, confidence {int(confidence)}%). Reassigned to CS Level 2; "
            f"please follow #{int(original)}.</p>")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def post_comment(ctx: Context, tid: str, body: str) -> Tuple[bool, str]:
    f = Path(ctx.tmp.name) / f"comment_{tid}.html"
    f.write_text(body, encoding="utf-8")
    r = ctx.run([str(SCRIPTS / "sonar_comment.py"), tid, str(f), "--verify"])
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:200]
    return True, r.stdout.strip().splitlines()[0] if r.stdout.strip() else "posted"


def send_chat(ctx: Context, result: dict, ticket: dict, group_name: str, plan: dict) -> bool:
    if not ctx.child_env.get("DEMO_CHAT_URL"):
        return False
    chat = plan.get("chat") or {}
    argv = [str(SCRIPTS / "chat_alert.py"), "ticket", "--ticket-id", str(result["ticket_id"]),
            "--status", result["status"], "--subject", ticket.get("subject") or "",
            "--group", group_name, "--priority", (ticket.get("priority") or "MEDIUM").title(),
            "--confidence", str(int(result.get("confidence") or 0))]
    if chat.get("issue"):
        argv += ["--issue", chat["issue"]]
    reason = chat.get("reason") or result.get("escalation_reason")
    if reason:
        argv += ["--reason", str(reason)]
    if chat.get("analysis"):
        argv += ["--analysis", chat["analysis"]]
    for ev in result.get("evidence", [])[:6]:
        argv += ["--evidence", str(ev)]
    r = ctx.run(argv)
    if r.returncode != 0:
        log(f"chat_alert failed for {result['ticket_id']}: {(r.stderr or r.stdout).strip()[:160]}")
    return r.returncode == 0


def run_gate(ctx: Context, tid: str, result: dict, dry: bool) -> Tuple[int, str]:
    plan = ctx.plans.get(tid, {}).get("bounce") or {}
    add = ",".join(str(ctx.categories[k]) for k in plan.get("add", []))
    remove = ",".join(str(ctx.categories[k]) for k in plan.get("remove", []))
    argv = [str(SCRIPTS / "sonar_bounce.py"), "bounce", tid, "--reason", plan.get("reason", "IHN"),
            "--confidence", str(int(result.get("confidence") or 0)),
            "--evidence", ",".join(str(e).replace(",", ";") for e in result.get("evidence", []))]
    if add:
        argv += ["--add-categories", add]
    if remove:
        argv += ["--remove-categories", remove]
    if dry:
        argv.append("--dry-run")
    r = ctx.run(argv)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def escalate(result: dict, reason: str, detail: str = "") -> None:
    result["status"] = "needs_human"
    result["requires_human_review"] = True
    result["escalation_reason"] = reason
    if detail:
        result["evidence"].append(detail[:160])


# ---------------------------------------------------------------------------
# Per-status handlers
# ---------------------------------------------------------------------------

def handle(ctx: Context, tid: str, canned: dict, ticket: dict) -> dict:
    result = copy.deepcopy(canned)
    result["ticket_id"] = str(tid)
    result.setdefault("evidence", [])
    plan = ctx.plans.get(tid, {})
    status = canned["status"]
    group_name = (ticket.get("ticket_group") or {}).get("name", "NOC")
    base = ctx.env["SONAR_BASE_URL"]
    notify = False

    if status in ("auto_closed", "auto_closed_ooo", "auto_closed_maintenance"):
        updated = ctx.sonar.update_ticket(int(tid), status="CLOSED")
        if updated.get("status") != "CLOSED":
            escalate(result, "close_not_verified")
            notify = True

    elif status == "handled_duplicate":
        original = int(plan["duplicate_of"])
        ok, detail = post_comment(ctx, tid, duplicate_comment(base, original, int(canned.get("confidence") or 0)))
        if not ok:
            escalate(result, "comment_post_failed", detail)
            notify = True
        else:
            ctx.sonar.update_ticket(int(tid), group_id=ctx.groups["cs_level_2"])

    elif status == "categorized":
        if plan.get("path"):
            ok, detail = post_comment(ctx, tid, path_comment(result["tower_site"], plan["path"]))
            if not ok:
                escalate(result, "comment_post_failed", detail)
        if plan.get("add_categories"):
            current = [int(c["id"]) for c in ticket["ticket_categories"]["entities"]]
            wanted = [ctx.categories[k] for k in plan["add_categories"]]
            ctx.sonar.update_ticket(int(tid), category_ids=current + [c for c in wanted if c not in current])
        notify = True

    elif status == "needs_human":
        if plan.get("path"):
            keywords = [e.split(":", 1)[1] for e in canned.get("evidence", []) if e.startswith("weather:")]
            extra = weather_block(keywords) if keywords else ""
            ok, detail = post_comment(ctx, tid, path_comment(result["tower_site"], plan["path"], extra))
            if not ok:
                result["evidence"].append(detail[:160])
        notify = True

    elif status in ("auto_bounced_ihn", "auto_bounced_ihn_peak", "correlated_bounced"):
        dry = ctx.bounce_mode != "live"
        rc, out = run_gate(ctx, tid, result, dry)
        if rc == 0 and not dry:
            ok, detail = post_comment(ctx, tid, path_comment(
                result["tower_site"], plan["path"], ihn_block(int(canned.get("confidence") or 0))))
            if not ok:
                result["evidence"].append("comment_failed_after_bounce")
                result["evidence"].append(detail[:160])
        else:
            if rc == 0:
                escalate(result, "dry_run_would_bounce", "dry_run:gate_passed")
            else:
                escalate(result, GATE_EXIT_REASONS.get(rc, "bounce_gate_error"), f"gate_exit:{rc}")
                log(f"{tid}: bounce gate exit {rc}: {out[:160]}")
            ok, detail = post_comment(ctx, tid, path_comment(result["tower_site"], plan["path"]))
            if not ok:
                result["evidence"].append(detail[:160])
            notify = True

    else:
        escalate(result, "unsupported_canned_status", status)
        notify = True

    if notify:
        result["notification_sent"] = send_chat(ctx, result, ticket, group_name, plan)
    else:
        result["notification_sent"] = False
    return result


def fallback_result(tid: str, reason: str) -> dict:
    return {
        "ticket_id": str(tid), "status": "needs_human", "tower_site": None, "analyzer_site": None,
        "bottleneck_analysis": "failed", "notification_sent": False,
        "classification": {"category": "other", "complexity": "high", "priority": "P3"},
        "confidence": 0, "requires_human_review": True, "escalation_reason": reason,
        "evidence": [f"demo_agent:{reason}"],
    }


def process_ticket(ctx: Context, tid: str) -> dict:
    canned_path = ctx.fixtures / "agent_outputs" / f"{tid}.json"
    try:
        canned = json.loads(canned_path.read_text())
    except (OSError, ValueError):
        log(f"{tid}: no canned output at {canned_path}")
        return fallback_result(tid, "no_canned_output")
    try:
        ticket = ctx.sonar.get_ticket(int(tid))
        if not ticket:
            return fallback_result(tid, "ticket_not_found")
        log(f"{tid}: {ticket['subject']!r} -> {canned['status']}")
        return handle(ctx, tid, canned, ticket)
    except (StubError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        log(f"{tid}: action failed: {type(exc).__name__}: {exc}")
        return fallback_result(tid, "action_failed")


# ---------------------------------------------------------------------------
# Prompt handling and entry point
# ---------------------------------------------------------------------------

def read_prompt(arg: Optional[str]) -> str:
    if arg is None or arg == "-":
        return sys.stdin.read()
    try:
        if len(arg) < 4096 and "\n" not in arg and os.path.isfile(arg):
            return Path(arg).read_text(encoding="utf-8")
    except (OSError, ValueError):
        pass
    return arg


def _inside(path: Path, root: Optional[str]) -> bool:
    if not root:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def extract_ticket_ids(prompt: str, log_root: Optional[str]) -> Tuple[List[str], Optional[Path]]:
    result_file = None
    m = re.search(r"`result_file`:\s*`([^`]+)`", prompt)
    if m and _inside(Path(m.group(1)), log_root):
        result_file = Path(m.group(1))
    m = re.search(r"`batch_file`:\s*`([^`]+)`", prompt)
    if m and _inside(Path(m.group(1)), log_root):
        try:
            payload = json.loads(Path(m.group(1)).read_text())
            ids = [str(t["ticket_id"]) for t in payload if isinstance(t, dict) and "ticket_id" in t]
            if ids:
                return ids, result_file
        except (OSError, ValueError, TypeError):
            pass
    ids = re.findall(r"^\s*\d+\.\s+#(\d+)\b", prompt, flags=re.M)
    if not ids:
        ids = re.findall(r"\bticket\s+#?(\d{3,})\b", prompt, flags=re.I)
    return list(dict.fromkeys(ids)), result_file


def emit(results: List[dict], result_file: Optional[Path]) -> None:
    text = json.dumps(results)
    if result_file is not None:
        tmp = Path(str(result_file) + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, result_file)
    print(text, flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    explicit_tickets: Optional[List[str]] = None
    bounce_mode = env.get("DEMO_BOUNCE_MODE", "dry").lower()
    fixtures = Path(env.get("DEMO_FIXTURES_DIR") or DEFAULT_FIXTURES)

    flags = ("--tickets", "--bounce-mode", "--fixtures", "-h", "--help")
    if argv and argv[0].split("=", 1)[0] in flags:
        p = argparse.ArgumentParser(description="Deterministic demo agent (mock Sonar only).")
        p.add_argument("--tickets", help="comma-separated ticket ids to process")
        p.add_argument("--bounce-mode", choices=["dry", "live"], default=bounce_mode)
        p.add_argument("--fixtures", default=str(fixtures))
        args = p.parse_args(argv)
        explicit_tickets = [t.strip() for t in (args.tickets or "").split(",") if t.strip()]
        bounce_mode, fixtures, prompt = args.bounce_mode, Path(args.fixtures), ""
    else:
        prompt = read_prompt(argv[0] if argv else None)
    if bounce_mode not in ("dry", "live"):
        print(f"DEMO_BOUNCE_MODE must be dry or live (got {bounce_mode!r})", file=sys.stderr)
        return 2

    try:
        check_sandbox(env)
    except SandboxError as exc:
        print(f"demo_agent_stub: {exc}", file=sys.stderr)
        return 2

    if explicit_tickets is not None:
        ids, result_file = explicit_tickets, None
    else:
        ids, result_file = extract_ticket_ids(prompt, env.get("NOC_LOG_DIR") or env.get("NOC_AGENT_LOG_DIR"))
    if not ids:
        print("demo_agent_stub: no ticket ids found in the prompt", file=sys.stderr)
        return 1

    try:
        ctx = Context(fixtures, bounce_mode, env)
    except (OSError, ValueError, KeyError) as exc:
        print(f"demo_agent_stub: cannot load fixtures from {fixtures}: {exc}", file=sys.stderr)
        return 2
    try:
        log(f"batch of {len(ids)}: {', '.join(ids)} (bounce mode: {bounce_mode})")
        results = [process_ticket(ctx, tid) for tid in ids]
        emit(results, result_file)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
