#!/usr/bin/env python3
"""
Tests for scripts/mock_sonar.py and the demo fixtures.

Run from the repo root:
    python3 -B -m unittest scripts.test_mock_sonar
    python3 -B -m unittest discover -s scripts -p 'test_*.py'

The mock runs on an ephemeral 127.0.0.1 port inside this process. Integration tests that drive the
real scripts/sonar_comment.py and scripts/sonar_bounce.py run as subprocesses (urllib, no curl).
Fake tokens are generated at runtime; nothing here touches a real system.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
FIXTURES = SCRIPTS / "demo" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import mock_sonar  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ALLOWED_STATUSES = {
    "handled_duplicate", "categorized", "needs_human", "passthrough", "auto_closed", "auto_closed_ooo",
    "auto_closed_maintenance", "pbs_failure", "auto_bounced_ihn", "auto_bounced_ihn_peak",
    "correlated_bounced", "queued_for_peak",
}
ALLOWED_CATEGORIES = {
    "slow_speeds", "rain_fade", "outage", "duplicate", "passthrough_ooo", "passthrough_pbs",
    "passthrough_maintenance", "sys_admin", "other",
}


class MockTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "demo-" + secrets.token_hex(16)
        cls.srv = mock_sonar.MockSonarServer(FIXTURES, [cls.token], port=0).start()
        cls.url = cls.srv.url

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        status, body = self.http("POST", "/__reset", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    # -- helpers
    def http(self, method, path, payload=None, token="default", raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        tok = self.token if token == "default" else token
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _OPENER.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def gql(self, query, variables=None, token="default"):
        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        status, body = self.http("POST", "/api/graphql", payload, token=token)
        self.assertEqual(status, 200, body)
        return body

    def audit(self):
        status, body = self.http("GET", "/__audit")
        self.assertEqual(status, 200)
        return body


class TestQueries(MockTestBase):
    def test_ticket_by_integer_id_with_nested_fields(self):
        body = self.gql('{ tickets(search: { integer_fields: [ { attribute: "id", operator: EQ, search_value: 91001 } ] }) '
                        '{ entities { id subject status priority ticket_group { id name } '
                        'ticket_categories { entities { id name } } created_at } page_info { total_count } } }')
        self.assertNotIn("errors", body)
        ents = body["data"]["tickets"]["entities"]
        self.assertEqual(len(ents), 1)
        t = ents[0]
        self.assertEqual(t["id"], 91001)
        self.assertEqual(t["status"], "OPEN")
        self.assertEqual(t["ticket_group"], {"id": 41, "name": "NOC"})
        self.assertEqual([c["id"] for c in t["ticket_categories"]["entities"]], [201])
        self.assertEqual(body["data"]["tickets"]["page_info"]["total_count"], 1)

    def test_string_id_search_is_exact_not_substring(self):
        body = self.gql('{ tickets(search: {string_fields: [{attribute: "id", search_value: "9100", match: true}]}) '
                        '{ entities { id } } }')
        self.assertEqual(body["data"]["tickets"]["entities"], [])
        body = self.gql('{ tickets(search: {string_fields: [{attribute: "id", search_value: "91003", match: true}]}) '
                        '{ entities { id subject } } }')
        self.assertEqual([e["id"] for e in body["data"]["tickets"]["entities"]], [91003])

    def test_open_noc_tickets_status_or_and_group_and(self):
        body = self.gql('query { tickets(search: { integer_fields: [ { attribute: "ticket_group_id", operator: EQ, search_value: 41 } ] '
                        'string_fields: [ { attribute: "status", search_value: "OPEN", match: true }, '
                        '{ attribute: "status", search_value: "PENDING_INTERNAL", match: true } ] }) '
                        '{ entities { id } page_info { total_count page total_pages records_per_page } } }')
        ids = [e["id"] for e in body["data"]["tickets"]["entities"]]
        self.assertEqual(ids, [91001, 91002, 91003, 91004, 91006, 91007, 91008])
        self.assertEqual(body["data"]["tickets"]["page_info"]["total_count"], 7)

    def test_site_search_subject_or_description_with_sorter_and_paginator(self):
        body = self.gql('{ tickets(search: { string_fields: [ { attribute: "subject", search_value: "delta", match: true }, '
                        '{ attribute: "description", search_value: "delta", match: true } ] }, '
                        'sorter: { attribute: "created_at", direction: DESC }, paginator: { page: 1, records_per_page: 1 }) '
                        '{ entities { id } page_info { total_count total_pages records_per_page } } }')
        self.assertEqual([e["id"] for e in body["data"]["tickets"]["entities"]], [91007])
        self.assertEqual(body["data"]["tickets"]["page_info"],
                         {"total_count": 2, "total_pages": 2, "records_per_page": 1})

    def test_comments_sorted_and_paginated(self):
        q = ('{ ticket_comments(search: { integer_fields: [ { attribute: "ticket_id", operator: EQ, search_value: 91008 } ] }, '
             'sorter: { attribute: "created_at", direction: %s }, paginator: { page: 1, records_per_page: %d }) '
             '{ entities { id body user { name } created_at } page_info { total_count } } }')
        desc = self.gql(q % ("DESC", 5))["data"]["ticket_comments"]
        asc = self.gql(q % ("ASC", 1))["data"]["ticket_comments"]
        self.assertEqual(desc["page_info"]["total_count"], 2)
        self.assertIn("rain fade", desc["entities"][0]["body"])
        self.assertEqual(asc["entities"][0]["body"], "<p>Tower: Echo</p>")
        self.assertEqual(len(asc["entities"]), 1)

    def test_variables_alias_fragment_skip_and_typename(self):
        query = ('query Q($id: Int64Bit!, $hide: Boolean = false) { t: tickets(search: { integer_fields: '
                 '[ { attribute: "id", operator: EQ, search_value: $id } ] }) { entities { ...F skipped: subject @skip(if: $hide) } } } '
                 'fragment F on Ticket { id __typename status }')
        body = self.gql(query, {"id": 91002})
        ent = body["data"]["t"]["entities"][0]
        self.assertEqual((ent["id"], ent["__typename"], ent["status"]), (91002, "Ticket", "OPEN"))
        self.assertIn("skipped", ent)
        body = self.gql(query, {"id": 91002, "hide": True})
        self.assertNotIn("skipped", body["data"]["t"]["entities"][0])

    def test_groups_categories_and_empty_accounts(self):
        body = self.gql('{ ticket_groups { entities { id name } } ticket_categories { entities { id name } } '
                        'accounts(search: [{string_fields: [{attribute: "name", search_value: "zzz", match: true}]}]) { entities { id } } }')
        self.assertNotIn("errors", body)
        self.assertIn({"id": 42, "name": "CS Level 2"}, body["data"]["ticket_groups"]["entities"])
        self.assertEqual(len(body["data"]["ticket_categories"]["entities"]), 5)
        self.assertEqual(body["data"]["accounts"]["entities"], [])


class TestMutations(MockTestBase):
    def test_create_comment_roundtrip_and_audit(self):
        html = "<p><strong>Direct Path Analysis - Alpha</strong></p><p>Status: HEALTHY, all hops green.</p>"
        body = self.gql('mutation { createTicketComment(input: { ticket_id: 91001, body: "%s" }) { id } }' % html)
        self.assertNotIn("errors", body)
        cid = body["data"]["createTicketComment"]["id"]
        got = self.gql('{ ticket_comments(search: { integer_fields: [ { attribute: "ticket_id", operator: EQ, search_value: 91001 } ] }, '
                       'sorter: { attribute: "created_at", direction: DESC }, paginator: { page: 1, records_per_page: 5 }) '
                       '{ entities { id body } } }')
        newest = got["data"]["ticket_comments"]["entities"][0]
        self.assertEqual((newest["id"], newest["body"]), (cid, html))
        rec = self.audit()["audit"][-1]
        self.assertEqual((rec["field"], rec["ticket_id"], rec["ok"], rec["comment_id"]),
                         ("createTicketComment", 91001, True, cid))

    def test_create_comment_unknown_ticket_is_graphql_error_and_audited(self):
        body = self.gql('mutation { createTicketComment(input: { ticket_id: 1, body: "x" }) { id } }')
        self.assertIsNone(body["data"]["createTicketComment"])
        self.assertIn("does not exist", body["errors"][0]["message"])
        self.assertEqual(body["errors"][0]["path"], ["createTicketComment"])
        rec = self.audit()["audit"][-1]
        self.assertFalse(rec["ok"])

    def test_update_ticket_group_and_categories_replace(self):
        body = self.gql('mutation { updateTicket(id: 91001, input: { ticket_group_id: 42, ticket_category_ids: [203] }) '
                        '{ id ticket_group { id name } ticket_categories { entities { id name } } } }')
        self.assertNotIn("errors", body)
        t = body["data"]["updateTicket"]
        self.assertEqual(t["ticket_group"], {"id": 42, "name": "CS Level 2"})
        self.assertEqual([c["id"] for c in t["ticket_categories"]["entities"]], [203])
        rec = self.audit()["audit"][-1]
        self.assertEqual(rec["before"]["ticket_group_id"], 41)
        self.assertEqual(rec["after"]["ticket_group_id"], 42)
        self.assertEqual(rec["after"]["ticket_category_ids"], [203])

    def test_update_ticket_status_by_variable_and_enum_validation(self):
        q = "mutation { updateTicket(id: 91003, input: { status: CLOSED }) { id status } }"
        self.assertEqual(self.gql(q)["data"]["updateTicket"]["status"], "CLOSED")
        bad = self.gql('mutation { updateTicket(id: 91003, input: { status: SOMETHING }) { id } }')
        self.assertIsNone(bad["data"]["updateTicket"])
        self.assertIn("does not exist", bad["errors"][0]["message"])

    def test_update_ticket_rejects_unknown_group_category_and_input_field(self):
        for inp, needle in (("ticket_group_id: 9999", "group"), ("ticket_category_ids: [9999]", "category"),
                            ("assignee: 5", "not defined")):
            body = self.gql('mutation { updateTicket(id: 91001, input: { %s }) { id } }' % inp)
            self.assertIsNone(body["data"]["updateTicket"], inp)
            self.assertIn(needle, body["errors"][0]["message"])
        state = self.gql('{ tickets(search: {integer_fields: [{attribute: "id", operator: EQ, search_value: 91001}]}) '
                         '{ entities { ticket_group_id } } }')
        self.assertEqual(state["data"]["tickets"]["entities"][0]["ticket_group_id"], 41)
        self.assertEqual(sum(1 for a in self.audit()["audit"] if not a["ok"]), 3)

    def test_invalid_mutation_does_not_execute_any_side_effect(self):
        body = self.gql('mutation { updateTicket(id: 91001, input: { ticket_group_id: 42 }) { id nope } }')
        self.assertNotIn("data", body)
        self.assertEqual(self.audit()["audit"], [])

    def test_reset_restores_fixtures_and_clears_audit_and_chat(self):
        self.gql('mutation { updateTicket(id: 91001, input: { status: CLOSED }) { id } }')
        self.http("POST", "/chat/webhook", {"text": "hello"}, token=None)
        self.assertEqual(len(self.audit()["audit"]), 1)
        status, body = self.http("POST", "/__reset")
        self.assertEqual((status, body["tickets"]), (200, 8))
        self.assertEqual(self.audit()["audit"], [])
        self.assertEqual(self.http("GET", "/__chat")[1]["chat"], [])
        t = self.gql('{ tickets(search: {integer_fields: [{attribute: "id", operator: EQ, search_value: 91001}]}) '
                     '{ entities { status } } }')
        self.assertEqual(t["data"]["tickets"]["entities"][0]["status"], "OPEN")


class TestAuthAndErrors(MockTestBase):
    def test_missing_wrong_and_malformed_auth(self):
        q = {"query": "{ ticket_groups { entities { id } } }"}
        for token in (None, "wrong-" + secrets.token_hex(8)):
            status, body = self.http("POST", "/api/graphql", q, token=token)
            self.assertEqual(status, 401)
            self.assertEqual(body["errors"][0]["extensions"]["code"], "UNAUTHENTICATED")
        status, _ = self.http("POST", "/api/graphql", q, token=None, headers={"Authorization": "Basic Zm9vOmJhcg=="})
        self.assertEqual(status, 401)
        self.assertEqual(self.http("GET", "/__audit", token=None)[0], 401)
        self.assertEqual(self.http("POST", "/__reset", token=None)[0], 401)
        self.assertEqual(self.audit()["counts"]["auth_failures"], 5)

    def test_alternate_graphql_path_and_health(self):
        status, body = self.http("POST", "/graphql", {"query": "{ ticket_groups { entities { id } } }"})
        self.assertEqual(status, 200)
        self.assertIn("data", body)
        self.assertEqual(self.http("GET", "/health", token=None)[1]["status"], "ok")

    def test_unsupported_things_return_wellformed_errors(self):
        cases = [
            "{ nope { id } }",                                            # unknown root field
            "{ tickets { entities { assignee } } }",                      # field Sonar's tickets do not have
            "{ tickets { entities } }",                                   # missing sub-selection
            "{ tickets { entities { id { x } } } }",                      # selection on scalar
            "mutation { deleteTicket(id: 1) { id } }",                    # unsupported mutation
            "{ __schema { types { name } } }",                            # introspection
            "subscription { tickets { entities { id } } }",               # subscriptions
            "{ tickets(search: { integer_fields: [ { attribute: \"ticket_category_id\", operator: EQ, search_value: 1 } ] }) { entities { id } } }",
            "{ tickets(search: { boolean_fields: [] }) { entities { id } } }",
            "{ tickets(sorter: { attribute: \"subject\", direction: ASC }) { entities { id } } }",
            "query {",                                                    # syntax: EOF
            "{ tickets(search: ) { entities { id } } }",                  # syntax: missing value
            "{ tickets @@ }",                                             # syntax: junk
            "",                                                           # empty document
        ]
        for q in cases:
            status, body = self.http("POST", "/api/graphql", {"query": q})
            self.assertEqual(status, 200, q)
            self.assertIsInstance(body.get("errors"), list, q)
            self.assertTrue(body["errors"][0]["message"], q)

    def test_raw_newline_in_string_literal_is_a_syntax_error_like_real_sonar(self):
        q = 'mutation { createTicketComment(input: { ticket_id: 91001, body: "line1\nline2" }) { id } }'
        body = self.gql(q)
        self.assertIn("Unterminated string", body["errors"][0]["message"])
        self.assertEqual(self.audit()["audit"], [])

    def test_bad_json_batch_and_missing_query(self):
        self.assertEqual(self.http("POST", "/api/graphql", raw=b"{not json")[0], 400)
        status, body = self.http("POST", "/api/graphql", [{"query": "{ ticket_groups { entities { id } } }"}])
        self.assertEqual(status, 200)
        self.assertIn("Batched", body["errors"][0]["message"])
        status, body = self.http("POST", "/api/graphql", {"variables": {}})
        self.assertIn("No query", body["errors"][0]["message"])
        self.assertEqual(self.http("GET", "/nope", token=None)[0], 404)
        self.assertEqual(self.http("GET", "/api/graphql")[0], 405)

    def test_string_escapes_and_unicode_survive(self):
        body = self.gql('mutation { createTicketComment(input: { ticket_id: 91001, body: "quote \\" backslash \\\\ dash — \\u00e9 &quot;x&quot;" }) { id } }')
        self.assertNotIn("errors", body)
        stored = self.audit()["audit"][-1]["body"]
        self.assertEqual(stored, 'quote " backslash \\ dash — é &quot;x&quot;')

    def test_chat_sink_records_payloads(self):
        status, ack = self.http("POST", "/chat/webhook", {"text": "hi", "cardsV2": []}, token=None)
        self.assertEqual(status, 200)
        self.assertIn("name", ack)
        chat = self.http("GET", "/__chat")[1]["chat"]
        self.assertEqual(chat[0]["payload"]["text"], "hi")


class TestFixtures(unittest.TestCase):
    """Guards that keep the public fixtures fictional and consistent with the batch output contract."""

    def _all_fixture_files(self):
        return [p for p in FIXTURES.rglob("*.json")]

    def test_only_documentation_ranges_and_example_domains(self):
        ip_re = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
        for path in self._all_fixture_files():
            text = path.read_text()
            for m in ip_re.finditer(text):
                a, b, c, _d = (int(x) for x in m.groups())
                ok = (a == 10 and b == 0) or (a, b, c) in ((198, 51, 100), (203, 0, 113)) or (a, b, c) == (127, 0, 0)
                self.assertTrue(ok, f"{path.name}: {m.group(0)} is outside the allowed documentation ranges")
            self.assertNotRegex(text, r"sonar\.software", path.name)
            for host in re.findall(r"@([A-Za-z0-9.-]+)", text):
                self.assertTrue(host.endswith("example.com"), f"{path.name}: unexpected mail domain {host}")

    def test_canned_outputs_follow_batch_contract(self):
        tickets = json.loads((FIXTURES / "tickets.json").read_text())["tickets"]
        ids = {str(t["id"]) for t in tickets}
        files = {p.stem for p in (FIXTURES / "agent_outputs").glob("*.json")}
        self.assertEqual(files, ids)
        for tid in ids:
            out = json.loads((FIXTURES / "agent_outputs" / f"{tid}.json").read_text())
            self.assertEqual(out["ticket_id"], tid)
            self.assertIn(out["status"], ALLOWED_STATUSES)
            for key in ("tower_site", "analyzer_site", "bottleneck_analysis", "notification_sent",
                        "classification", "confidence", "requires_human_review", "escalation_reason", "evidence"):
                self.assertIn(key, out, f"{tid} missing {key}")
            self.assertIn(out["bottleneck_analysis"], {"completed", "failed", "skipped", "n/a", "deferred_to_peak"})
            self.assertIn(out["classification"]["category"], ALLOWED_CATEGORIES)
            self.assertIn(out["classification"]["complexity"], {"low", "medium", "high"})
            self.assertIn(out["classification"]["priority"], {"P1", "P2", "P3", "P4"})
            self.assertTrue(isinstance(out["confidence"], int) and 0 <= out["confidence"] <= 100)
            self.assertIsInstance(out["evidence"], list)
            self.assertEqual(out["requires_human_review"], out["status"] in ("needs_human", "queued_for_peak", "pbs_failure"))
            self.assertEqual(out["escalation_reason"] is None, not out["requires_human_review"])

    def test_plans_and_expectations_cover_every_ticket(self):
        tickets = json.loads((FIXTURES / "tickets.json").read_text())["tickets"]
        ids = {str(t["id"]) for t in tickets}
        plans = json.loads((FIXTURES / "plans.json").read_text())
        expected = json.loads((FIXTURES / "expected.json").read_text())["tickets"]
        self.assertEqual({k for k in plans if not k.startswith("_")}, ids)
        self.assertEqual(set(expected), ids)
        for tid in ids:
            out = json.loads((FIXTURES / "agent_outputs" / f"{tid}.json").read_text())
            self.assertEqual(expected[tid]["status"], out["status"])

    def test_scenarios_requested_for_demo_are_present(self):
        statuses = {json.loads(p.read_text())["status"] for p in (FIXTURES / "agent_outputs").glob("*.json")}
        for needed in ("auto_bounced_ihn", "categorized", "auto_closed", "auto_closed_maintenance",
                       "auto_closed_ooo", "handled_duplicate", "needs_human"):
            self.assertIn(needed, statuses)


class TestRealScriptsAgainstMock(MockTestBase):
    """Run the shipped helpers (sonar_comment.py, sonar_bounce.py) against the mock."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="mock-sonar-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update({
            "SONAR_BASE_URL": self.url, "SONAR_TOKEN": self.token, "NO_PROXY": "127.0.0.1",
            "NOC_AGENT_LOG_DIR": os.path.join(self.tmp, "logs"),
            "NOC_DOTENV": os.path.join(self.tmp, "absent.env"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CS_LEVEL2_GROUP_ID": "42", "IHN_BOUNCE_CATEGORY_ID": "203",
            "SLOW_SPEEDS_CATEGORY_ID": "201", "NIGHT_PEAK_CATEGORY_ID": "202",
        })
        env["BOUNCE_AUTONOMOUS_ENABLED"] = "true"  # the gate is default-OFF since the 2c hardening
        self.env = env

    def run_script(self, *argv):
        return subprocess.run([sys.executable, "-B", *argv], capture_output=True, text=True,
                              env=self.env, cwd=self.tmp, timeout=60)

    def post_comment(self, ticket_id, html):
        f = Path(self.tmp) / "comment.html"
        f.write_text(html)
        return self.run_script(str(SCRIPTS / "sonar_comment.py"), str(ticket_id), str(f), "--verify")

    def test_sonar_comment_posts_and_verifies(self):
        html = "<p><strong>Direct Path Analysis - Alpha</strong></p><p>Status: HEALTHY, every hop is green.</p>"
        r = self.post_comment(91001, html)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Comment posted:", r.stdout)
        self.assertIn("Verified: body OK", r.stdout)
        self.assertEqual([a["field"] for a in self.audit()["audit"]], ["createTicketComment"])

    def test_sonar_comment_bad_token_fails_cleanly(self):
        self.env["SONAR_TOKEN"] = "demo-" + secrets.token_hex(8)
        r = self.post_comment(91001, "<p>Direct Path Analysis - Alpha</p><p>this should never be stored anywhere</p>")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.audit()["audit"], [])

    def test_bounce_dry_run_then_live_then_anti_loop_gate(self):
        bounce = ["bounce", "91001", "--reason", "IHN", "--add-categories", "203",
                  "--remove-categories", "201,202", "--confidence", "92", "--evidence", "tower:note,hops:5"]
        dry = self.run_script(str(SCRIPTS / "sonar_bounce.py"), *bounce, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["final_categories"], [203])
        self.assertEqual(self.audit()["audit"], [])

        live = self.run_script(str(SCRIPTS / "sonar_bounce.py"), *bounce)
        self.assertEqual(live.returncode, 0, live.stderr)
        mutation = self.audit()["audit"][-1]
        self.assertEqual((mutation["field"], mutation["after"]["ticket_group_id"],
                          mutation["after"]["ticket_category_ids"]), ("updateTicket", 42, [203]))
        log_dir = Path(self.env["NOC_AGENT_LOG_DIR"]) / "decisions"
        lines = [json.loads(x) for p in log_dir.glob("*.jsonl") for x in p.read_text().splitlines()]
        self.assertTrue(any(rec.get("action") == "BOUNCED" and rec["ticket_id"] == "91001" for rec in lines))

        # The marker comment posted after a bounce makes a second bounce hit the anti-loop gate (exit 5).
        marker = "<p><strong>Direct Path Analysis - Alpha</strong></p><p>Automated NOC Review - No Network Issue</p>"
        self.assertEqual(self.post_comment(91001, marker).returncode, 0)
        again = self.run_script(str(SCRIPTS / "sonar_bounce.py"), *bounce)
        self.assertEqual(again.returncode, 5, again.stderr)

    def test_bounce_kill_switch_exit_4(self):
        self.env["BOUNCE_AUTONOMOUS_ENABLED"] = "false"
        r = self.run_script(str(SCRIPTS / "sonar_bounce.py"), "bounce", "91001", "--add-categories", "203")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.audit()["audit"], [])


if __name__ == "__main__":
    unittest.main()
