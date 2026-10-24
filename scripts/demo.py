#!/usr/bin/env python3
"""
demo.py - run the NocAgent ticket pipeline end to end with zero real systems.

    python3 scripts/demo.py                # run, print what was decided and the mock's audit trail
    python3 scripts/demo.py --check        # same run as a quiet CI smoke test (exit 0 only if every check passes)

What it does:
  1. Starts scripts/mock_sonar.py in-process on an ephemeral 127.0.0.1 port (fictional tickets).
  2. Starts the real server/index.js in a temp dir with a fully synthetic environment: a fresh webhook
     token, AGENT_HARNESS=command driving scripts/demo_agent_stub.py, Sonar and the chat webhook
     pointing at the mock, and logs/state redirected into the temp dir. The server is fenced in only
     through its own settings: WEBHOOK_PORT=0 (ephemeral), WEBHOOK_BIND=127.0.0.1, NOC_SKIP_DOTENV=1
     (a real .env is never loaded), NOC_FETCH_LOOPBACK_ONLY=1, NOC_LOG_DIR / NOC_STATE_DIR in the temp
     dir and an explicit agent environment allowlist. server/index.js is not modified.
  3. POSTs the fixture tickets to /webhook/ticket exactly as N8N would, waits for the batches to
     settle, then compares decisions, ticket state, mutation order, comments, chat cards and the
     decision log with scripts/demo/fixtures/expected.json.
  4. Stops every child process and removes the temp dir (--keep to inspect it).

Scenarios (--scenario):
  default        the bounce gate runs for real against the mock
  dry-run        the gate runs with --dry-run; nothing is bounced
  kill-switch    BOUNCE_AUTONOMOUS_ENABLED=false; the gate refuses (exit 4)
  breaker-open   the circuit breaker is opened via POST /governance/breaker; the gate refuses (exit 3)

Needs: python3 (3.9+), node (18+), curl, and `npm install` in server/. Nothing leaves 127.0.0.1.
Exit codes: 0 all checks passed, 1 a check failed, 2 missing prerequisite, 3 the pipeline did not run.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
FIXTURES = SCRIPTS / "demo" / "fixtures"
STUB = SCRIPTS / "demo_agent_stub.py"
SERVER_JS = ROOT / "server" / "index.js"
AGENT_COMMENT_USER = "NocAgent (demo)"
HUMAN_REVIEW_STATUSES = {"needs_human", "queued_for_peak", "pbs_failure"}
SCENARIOS = ("default", "dry-run", "kill-switch", "breaker-open")

sys.path.insert(0, str(SCRIPTS))
import mock_sonar  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DemoError(Exception):
    """The pipeline could not be run (as opposed to a check failing)."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def http_json(method: str, url: str, payload: Any = None, headers: Optional[Dict[str, str]] = None,
              timeout: float = 10) -> Tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_jsonl(paths: List[Path]) -> List[dict]:
    out = []
    for p in paths:
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    return out


def tail(path: Path, n: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(unreadable)"


def snapshot_tree(root: Path) -> Dict[str, Tuple[int, int]]:
    snap: Dict[str, Tuple[int, int]] = {}
    if root.is_dir():
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return snap


def preflight() -> None:
    problems = []
    if sys.version_info < (3, 9):
        problems.append("Python 3.9+ is required")
    node = shutil.which("node")
    if not node:
        problems.append("node is not on PATH (Node 18+ required)")
    else:
        try:
            ver = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
            if int(ver.lstrip("v").split(".")[0]) < 18:
                problems.append(f"Node 18+ required (found {ver})")
        except (OSError, ValueError, subprocess.SubprocessError):
            problems.append("could not run `node --version`")
    if not shutil.which("curl"):
        problems.append("curl is not on PATH (the Sonar helper scripts shell out to it)")
    if not (ROOT / "server" / "node_modules" / "express" / "package.json").exists():
        problems.append("server dependencies are missing: run `npm install` inside server/")
    if not SERVER_JS.exists():
        problems.append(f"{SERVER_JS} not found")
    if problems:
        for p in problems:
            print(f"demo: {p}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# sandbox: mock Sonar + real webhook server in a temp dir
# ---------------------------------------------------------------------------

class Sandbox:
    def __init__(self, scenario: str, keep: bool) -> None:
        self.scenario = scenario
        self.keep = keep
        self.tmp = Path(tempfile.mkdtemp(prefix="nocagent-demo-"))
        for d in ("logs", "state", "workspace", "home", "tmp"):
            (self.tmp / d).mkdir()
        self.logs = self.tmp / "logs"      # NOC_LOG_DIR: webhook.log, batch/result files
        self.state = self.tmp / "state"    # NOC_STATE_DIR: decisions/, breaker, pending queues
        self.web_token = secrets.token_urlsafe(32)
        self.sonar_token = mock_sonar.generate_token()
        self.mock: Optional[mock_sonar.MockSonarServer] = None
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.server_out = self.tmp / "server.out"
        self.profile = json.loads((FIXTURES / "profile.demo.json").read_text())["sonar"]
        self._stopped = False

    # -- lifecycle
    def start_mock(self) -> None:
        self.mock = mock_sonar.MockSonarServer(FIXTURES, [self.sonar_token], port=0).start()

    def server_env(self) -> Dict[str, str]:
        assert self.mock is not None
        base = self.mock.url
        # AGENT_COMMAND_TEMPLATE is split into an argv array and run without a shell; the prompt arrives as
        # the path of a mode-0600 temp file ({prompt_file}).
        template = f"{shlex.quote(sys.executable)} -B {shlex.quote(str(STUB))} {{prompt_file}}"
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "TMPDIR": str(self.tmp / "tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            # Fence: ephemeral loopback port, no .env, loopback-only outbound fetch, everything under the temp dir.
            "WEBHOOK_PORT": "0",
            "WEBHOOK_BIND": "127.0.0.1",
            "NOC_SKIP_DOTENV": "1",
            "NOC_FETCH_LOOPBACK_ONLY": "1",
            "NOC_LOG_DIR": str(self.logs),
            "NOC_STATE_DIR": str(self.state),
            "NOC_DOTENV": str(self.tmp / "absent.env"),   # the Python helpers' .env reader
            "NOC_PROFILE_FILE": str(FIXTURES / "profile.demo.json"),
            "TICKET_WEBHOOK_AUTH_TOKEN": self.web_token,
            "WEBHOOK_GOVERNANCE_RATE_LIMIT": "600",
            "AGENT_WORKSPACE": str(self.tmp / "workspace"),
            "AGENT_HARNESS": "command",
            "AGENT_COMMAND_TEMPLATE": template,
            "AGENT_ENV_PASSTHROUGH": "DEMO_*",
            "PEAK_SCHEDULER_ENABLED": "false",
            "WINDOW_DURATION_MS": "1500",
            "MAX_BATCH_SIZE": "3",
            "BATCH_TIMEOUT_MS": "120000",
            "SONAR_BASE_URL": base,
            "ORG_SONAR_BASE_URL": base,
            "SONAR_TOKEN": self.sonar_token,
            "NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL": base + "/chat/webhook",
            "CHAT_ICON_VERIFY": "0",
            "DEMO_CHAT_URL": base + "/chat/webhook",
            "DEMO_FIXTURES_DIR": str(FIXTURES),
            "DEMO_BOUNCE_MODE": "dry" if self.scenario == "dry-run" else "live",
        }
        # The bounce kill switch defaults to OFF, so only the scenarios that bounce switch it on; the
        # kill-switch scenario leaves it unset (the fail-closed default).
        if self.scenario != "kill-switch":
            env["BOUNCE_AUTONOMOUS_ENABLED"] = "true"
        return env

    def start_server(self, timeout: float = 25) -> None:
        node = shutil.which("node") or "node"
        env = self.server_env()
        cfg = subprocess.run([node, str(SERVER_JS), "--check-config"], env=env, cwd=env["AGENT_WORKSPACE"],
                             capture_output=True, text=True, timeout=30)
        if cfg.returncode != 0:
            raise DemoError("server config check failed:\n" + "\n".join(
                l for l in (cfg.stdout + cfg.stderr).splitlines() if l.startswith("FAIL"))[:1500])
        with open(self.server_out, "wb") as out:
            self.proc = subprocess.Popen(
                [node, str(SERVER_JS)], env=env, cwd=env["AGENT_WORKSPACE"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline and self.proc.poll() is None:
            port = self._announced_port()
            if port:
                try:
                    status, body = http_json("GET", f"http://127.0.0.1:{port}/health", timeout=2)
                    if status == 200 and isinstance(body, dict) and body.get("service") == "noc-agent-webhook":
                        self.port = port
                        return
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.15)
        last = tail(self.server_out)
        self._kill_server()
        raise DemoError("the webhook server did not come up:\n" + last)

    def _announced_port(self) -> int:
        """The real port, from the server's startup line {"event":"listening","host":...,"port":...}."""
        try:
            for line in self.server_out.read_text(errors="replace").splitlines():
                if line.startswith("{") and '"listening"' in line:
                    rec = json.loads(line)
                    if rec.get("event") == "listening" and rec.get("host") in ("127.0.0.1", "::1"):
                        return int(rec["port"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return 0

    def _kill_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=8)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5)

    def stop_processes(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._kill_server()
        if self.mock is not None:
            self.mock.stop()

    def cleanup(self) -> None:
        self.stop_processes()
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- webhook client
    @property
    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def basic_auth(self, token: Optional[str] = None) -> Dict[str, str]:
        raw = ("OCAGENT:" + (token if token is not None else self.web_token)).encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def sonar(self, query: str) -> dict:
        status, body = http_json("POST", self.mock.url + "/api/graphql", {"query": query},
                                 {"Authorization": "Bearer " + self.sonar_token})
        if status != 200 or (body or {}).get("errors"):
            raise DemoError(f"mock Sonar query failed: {status} {body}")
        return body["data"]

    def mock_get(self, path: str) -> dict:
        status, body = http_json("GET", self.mock.url + path, headers={"Authorization": "Bearer " + self.sonar_token})
        if status != 200:
            raise DemoError(f"mock GET {path} -> {status}")
        return body


# ---------------------------------------------------------------------------
# run the pipeline and collect facts
# ---------------------------------------------------------------------------

def webhook_payload(t: dict, groups: Dict[int, str]) -> dict:
    return {
        "ticket_id": str(t["id"]), "subject": t["subject"], "ticket_group_id": t["ticket_group_id"],
        "ticket_group_name": groups.get(t["ticket_group_id"], "Unknown"), "status": t["status"],
        "priority": t["priority"].title(), "description": t["description"], "ticket_created": False,
    }


def run_pipeline(sb: Sandbox, timeout: float) -> Dict[str, Any]:
    tickets = json.loads((FIXTURES / "tickets.json").read_text())["tickets"]
    groups = {int(g["id"]): g["name"] for g in sb.profile["groups"].values()}
    facts: Dict[str, Any] = {"contract": {}, "posts": {}}

    # Webhook contract (nothing here queues a ticket).
    url = sb.webhook_url
    facts["contract"]["health"] = http_json("GET", url + "/health")[0]
    facts["contract"]["no_auth"] = http_json("POST", url + "/webhook/ticket", {"ticket_id": "1"})[0]
    facts["contract"]["bad_token"] = http_json("POST", url + "/webhook/ticket", {"ticket_id": "1"},
                                               sb.basic_auth(secrets.token_urlsafe(32)))[0]
    facts["contract"]["missing_ticket_id"] = http_json("POST", url + "/webhook/ticket", {}, sb.basic_auth())[0]

    if sb.scenario == "breaker-open":
        status, body = http_json("POST", url + "/governance/breaker",
                                 {"action": "open", "reason": "demo scenario"}, sb.basic_auth())
        facts["contract"]["breaker_open_call"] = status

    for t in tickets:
        status, body = http_json("POST", url + "/webhook/ticket", webhook_payload(t, groups), sb.basic_auth())
        facts["posts"][str(t["id"])] = {"status": status, "body": body}
    posted_at = time.time()

    # Wait for every ticket to have a decision-log record and the breaker feed to catch up.
    want = {str(t["id"]) for t in tickets}
    done: set = set()
    deadline = posted_at + timeout
    while time.time() < deadline:
        decisions = read_jsonl(sorted((sb.state / "decisions").glob("*.jsonl")))
        done = {str(r.get("ticket_id")) for r in decisions if r.get("kind") == "batch_result"}
        if want <= done:
            status, gov = http_json("GET", url + "/governance", headers=sb.basic_auth())
            if status == 200 and gov["breaker"]["window_total"] >= len(want):
                break
        if sb.proc is not None and sb.proc.poll() is not None:
            raise DemoError("the webhook server exited early:\n" + tail(sb.server_out))
        time.sleep(0.3)
    else:
        raise DemoError(
            f"timed out after {timeout:.0f}s waiting for decisions on {sorted(want - done)}\n"
            f"--- server log tail ---\n{tail(sb.logs / 'webhook.log', 12)}")
    time.sleep(0.4)  # let trailing async log writes land

    decisions = read_jsonl(sorted((sb.state / "decisions").glob("*.jsonl")))
    facts["batch"] = {}
    facts["batch_counts"] = {}
    for r in decisions:
        if r.get("kind") == "batch_result":
            tid = str(r["ticket_id"])
            facts["batch"][tid] = r
            facts["batch_counts"][tid] = facts["batch_counts"].get(tid, 0) + 1
    facts["bounce_records"] = [r for r in decisions if r.get("kind") == "bounce"]
    facts["governance"] = http_json("GET", url + "/governance", headers=sb.basic_auth())[1]["breaker"]
    facts["server_log_problems"] = [
        line for line in (sb.logs / "webhook.log").read_text(errors="replace").splitlines()
        if any(m in line for m in ("Batch agent processing failed", "Failed to parse batch results",
                                   "Decision log append failed", "Breaker record failed",
                                   "Fallback notification", "OpenCode failed"))
    ]
    pending = sb.state / "pending_tickets.json"
    try:
        facts["pending_after"] = json.loads(pending.read_text())
    except (OSError, ValueError):
        facts["pending_after"] = None

    # Final Sonar state straight from the mock, over its GraphQL API.
    data = sb.sonar(
        '{ tickets(search: { integer_fields: [ { attribute: "id", operator: GTE, search_value: 0 } ] }, '
        'paginator: { page: 1, records_per_page: 100 }) { entities { id subject status priority '
        'ticket_group { id name } ticket_categories { entities { id name } } '
        'ticket_comments(sorter: { attribute: "id", direction: ASC }, paginator: { page: 1, records_per_page: 50 }) '
        '{ entities { id body user { name } } } } } }')
    facts["tickets"] = {str(t["id"]): t for t in data["tickets"]["entities"]}
    audit = sb.mock_get("/__audit")
    facts["audit"], facts["mock_counts"] = audit["audit"], audit["counts"]
    facts["chat"] = sb.mock_get("/__chat")["chat"]
    facts["fixture_tickets"] = tickets
    facts["groups"] = groups
    facts["server_tail"] = tail(sb.logs / "webhook.log", 12)
    return facts


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def expected_for(tid: str, scenario: str, doc: dict) -> dict:
    base = dict(doc["tickets"][tid])
    override = (base.pop("scenarios", None) or {}).get(scenario, {})
    base.update(override)
    return base


def chat_ticket_ids(chat: List[dict]) -> List[str]:
    ids = []
    for c in chat:
        try:
            ids.append(c["payload"]["cardsV2"][0]["cardId"].rsplit("-", 1)[-1])
        except (KeyError, IndexError, TypeError, AttributeError):
            ids.append("?")
    return ids


def evaluate(facts: Dict[str, Any], scenario: str, cleanup: Dict[str, Any]) -> List[Tuple[str, bool, str]]:
    doc = json.loads((FIXTURES / "expected.json").read_text())
    checks: List[Tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    c = facts["contract"]
    chk("webhook: GET /health is 200", c["health"] == 200, str(c["health"]))
    chk("webhook: no credentials -> 401", c["no_auth"] == 401, str(c["no_auth"]))
    chk("webhook: wrong token -> 401", c["bad_token"] == 401, str(c["bad_token"]))
    chk("webhook: missing ticket_id -> 400", c["missing_ticket_id"] == 400, str(c["missing_ticket_id"]))
    if scenario == "breaker-open":
        chk("governance: POST /governance/breaker open -> 200", c.get("breaker_open_call") == 200,
            str(c.get("breaker_open_call")))
    bad = [tid for tid, p in facts["posts"].items()
           if p["status"] != 202 or not isinstance(p["body"], dict) or p["body"].get("status") != "accepted"
           or not p["body"].get("job_id")]
    jobs = [p["body"].get("job_id") for p in facts["posts"].values() if isinstance(p["body"], dict)]
    chk("webhook: every ticket POST -> 202 accepted with a distinct job_id",
        not bad and len(set(jobs)) == len(jobs), f"bad={bad}")

    chat_ids = chat_ticket_ids(facts["chat"])
    for t in facts["fixture_tickets"]:
        tid = str(t["id"])
        exp = expected_for(tid, scenario, doc)
        problems: List[str] = []
        rec = facts["batch"].get(tid)
        live = facts["tickets"].get(tid)
        if not rec or not live:
            chk(f"ticket {tid}: decision recorded and ticket present", False, "missing")
            continue
        if facts["batch_counts"].get(tid) != 1:
            problems.append(f"decision logged {facts['batch_counts'].get(tid)} times")
        if rec.get("status") != exp["status"]:
            problems.append(f"status {rec.get('status')!r} != {exp['status']!r}")
        if rec.get("escalation_reason") != exp.get("escalation_reason"):
            problems.append(f"escalation_reason {rec.get('escalation_reason')!r} != {exp.get('escalation_reason')!r}")
        if rec.get("requires_human_review") != (rec.get("status") in HUMAN_REVIEW_STATUSES):
            problems.append("requires_human_review inconsistent with status")
        cls = rec.get("classification") or {}
        if not (cls.get("category") and isinstance(rec.get("confidence"), int) and rec.get("evidence")):
            problems.append("decision record is missing classification/confidence/evidence")
        if live["status"] != exp["ticket_status"]:
            problems.append(f"Sonar status {live['status']} != {exp['ticket_status']}")
        if int(live["ticket_group"]["id"]) != exp["group_id"]:
            problems.append(f"group {live['ticket_group']['id']} != {exp['group_id']}")
        cats = [int(x["id"]) for x in live["ticket_categories"]["entities"]]
        if sorted(cats) != sorted(exp["category_ids"]):
            problems.append(f"categories {cats} != {exp['category_ids']}")
        muts = [a["field"] for a in facts["audit"] if str(a.get("ticket_id")) == tid and a.get("ok")]
        if muts != exp["mutations"]:
            problems.append(f"mutations {muts} != {exp['mutations']}")
        added = "\n".join(cm["body"] for cm in live["ticket_comments"]["entities"]
                          if (cm.get("user") or {}).get("name") == AGENT_COMMENT_USER).lower()
        for marker in exp["comment_markers"]:
            if marker.lower() not in added:
                problems.append(f"comment lacks {marker!r}")
        if not exp["comment_markers"] and added:
            problems.append("unexpected comment")
        n_chat = chat_ids.count(tid)
        if n_chat != exp["chat"]:
            problems.append(f"{n_chat} chat card(s) != {exp['chat']}")
        if bool(rec.get("notification_sent")) != bool(exp["chat"]):
            problems.append("notification_sent disagrees with chat sink")
        bounced = any(b.get("ticket_id") == tid and b.get("action") == "BOUNCED" for b in facts["bounce_records"])
        if bounced != exp.get("bounce_logged", False):
            problems.append(f"bounce record present={bounced}")
        chk(f"ticket {tid} ({exp['status']}): decision, Sonar state, mutations, comments, chat, bounce log",
            not problems, "; ".join(problems))

    gov = facts["governance"]
    chk("governance: breaker state as expected for the scenario",
        gov["breaker_open"] == (scenario == "breaker-open"), f"breaker_open={gov['breaker_open']}")
    chk("governance: every decision fed the breaker with no failures",
        gov["window_total"] == len(facts["fixture_tickets"]) and gov["window_failures"] == 0,
        f"total={gov['window_total']} failures={gov['window_failures']}")
    chk("server: webhook.log has no failure/fallback entries", not facts["server_log_problems"],
        "; ".join(facts["server_log_problems"][:2]))
    chk("server: pending-ticket tracking drained", facts["pending_after"] == [], str(facts["pending_after"]))
    chk("mock Sonar: every script call authenticated and well-formed",
        facts["mock_counts"]["auth_failures"] == 0 and facts["mock_counts"]["graphql_errors"] == 0,
        str(facts["mock_counts"]))
    chk("cleanup: webhook server stopped and its port closed", cleanup["server_stopped"] and not cleanup["port_open"],
        str(cleanup))
    chk("isolation: the repository's logs/ was not touched", cleanup["repo_logs_unchanged"],
        "logs/ changed during the run (is another instance writing there?)")
    return checks


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def clip(text: Any, n: int) -> str:
    s = "-" if text is None or text == "" else str(text)
    return s if len(s) <= n else s[: n - 1] + "~"


def render_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def describe_mutation(a: dict, groups: Dict[int, str]) -> str:
    if not a.get("ok"):
        return "REJECTED: " + clip(a.get("error"), 70)
    if a["field"] == "createTicketComment":
        m = re.search(r"<strong>(.*?)</strong>", a.get("body", ""))
        return f"comment: {clip(m.group(1) if m else a.get('body'), 60)}"
    b, af = a["before"], a["after"]
    parts = []
    if b["status"] != af["status"]:
        parts.append(f"status {b['status']} -> {af['status']}")
    if b["ticket_group_id"] != af["ticket_group_id"]:
        parts.append(f"group {groups.get(b['ticket_group_id'])} -> {groups.get(af['ticket_group_id'])}")
    if b["ticket_category_ids"] != af["ticket_category_ids"]:
        parts.append(f"categories {b['ticket_category_ids']} -> {af['ticket_category_ids']}")
    return "; ".join(parts) or "no change"


def render_report(facts: Dict[str, Any], scenario: str, checks: List[Tuple[str, bool, str]], elapsed: float) -> str:
    groups = facts["groups"]
    out: List[str] = [f"NocAgent demo - scenario: {scenario} (synthetic data, 127.0.0.1 only)", ""]
    rows = []
    chat_ids = chat_ticket_ids(facts["chat"])
    for t in facts["fixture_tickets"]:
        tid = str(t["id"])
        rec, live = facts["batch"].get(tid, {}), facts["tickets"].get(tid, {})
        cats = [x["id"] for x in (live.get("ticket_categories") or {}).get("entities", [])]
        rows.append([tid, clip(t["subject"], 40), clip(rec.get("status"), 24), clip(rec.get("confidence"), 4),
                     clip(rec.get("escalation_reason"), 22),
                     f"{live.get('status', '?')}/{(live.get('ticket_group') or {}).get('name', '?')} {cats}",
                     "chat" if tid in chat_ids else "-"])
    out.append("Pipeline decisions (from the server's decision log) and resulting Sonar state")
    out.append(render_table(["Ticket", "Subject", "Decision", "Conf", "Escalation", "Sonar state (cats)", "Chat"], rows))
    out += ["", "Mock Sonar audit trail (every mutation, in order)"]
    arows = [[str(a["seq"]), a["field"], str(a.get("ticket_id")), describe_mutation(a, groups)]
             for a in facts["audit"]]
    out.append(render_table(["#", "Mutation", "Ticket", "Effect"], arows) if arows else "(none)")
    out += ["", "Chat cards captured by the mock (nothing was sent anywhere)"]
    for c in facts["chat"]:
        out.append(f"  {c['payload'].get('text', '')}")
    if not facts["chat"]:
        out.append("  (none)")
    out += ["", "Bounce gate records"]
    for b in facts["bounce_records"] or []:
        out.append(f"  {b.get('action')} ticket {b.get('ticket_id')} reason={b.get('reason')} "
                   f"confidence={b.get('confidence')} target_group={b.get('target_group')}")
    if not facts["bounce_records"]:
        out.append("  (none)")
    out += ["", "Self-check"]
    for name, ok, detail in checks:
        out.append(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failed = [c for c in checks if not c[1]]
    out += ["", f"RESULT: {'PASS' if not failed else 'FAIL'} - {len(checks) - len(failed)}/{len(checks)} checks, "
                f"{elapsed:.1f}s"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run the NocAgent ticket pipeline against a mock Sonar (no real systems).")
    p.add_argument("--check", action="store_true", help="quiet CI smoke test: one result line, details only on failure")
    p.add_argument("--scenario", choices=SCENARIOS, default="default")
    p.add_argument("--timeout", type=float, default=90, help="seconds to wait for all tickets to settle (default 90)")
    p.add_argument("--keep", action="store_true", help="keep the temp dir (logs, server.out) and print its path")
    p.add_argument("--json", action="store_true", help="print the checks and decisions as JSON")
    args = p.parse_args(argv)

    preflight()
    started = time.time()
    repo_logs_before = snapshot_tree(ROOT / "logs")
    sb = Sandbox(args.scenario, args.keep)
    atexit.register(sb.cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: sys.exit(130))

    try:
        sb.start_mock()
        sb.start_server()
        facts = run_pipeline(sb, args.timeout)
    except DemoError as exc:
        print(f"demo: {exc}", file=sys.stderr)
        if args.keep:
            print(f"demo: temp dir kept at {sb.tmp}", file=sys.stderr)
        sb.cleanup()
        return 3
    except BaseException:
        sb.cleanup()
        raise

    sb.stop_processes()
    cleanup = {
        "server_stopped": sb.proc is not None and sb.proc.poll() is not None,
        "port_open": port_open(sb.port),
        "repo_logs_unchanged": snapshot_tree(ROOT / "logs") == repo_logs_before,
    }
    checks = evaluate(facts, args.scenario, cleanup)
    elapsed = time.time() - started
    failed = [c for c in checks if not c[1]]

    if args.json:
        print(json.dumps({
            "scenario": args.scenario, "result": "pass" if not failed else "fail",
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
            "decisions": {k: {x: v.get(x) for x in ("status", "confidence", "escalation_reason")}
                          for k, v in facts["batch"].items()},
        }, indent=2))
    elif args.check and not failed:
        print(f"demo check: PASS ({len(checks)} checks, {elapsed:.1f}s, scenario {args.scenario})")
    else:
        print(render_report(facts, args.scenario, checks, elapsed))
        if failed:
            print("\n--- server log tail ---\n" + facts["server_tail"])
    if args.keep:
        print(f"\ntemp dir kept at {sb.tmp}")
    sb.cleanup()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
