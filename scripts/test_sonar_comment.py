#!/usr/bin/env python3
"""
Tests for scripts/sonar_comment.py, scripts/gql_client.py, scripts/metrics_rollup.py and
scripts/chat_alert.py against the in-process scripts/mock_sonar.py (loopback, fake tokens).

    python3 -B -m unittest discover -s scripts -p 'test_sonar_comment.py'
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import gql_client  # noqa: E402
import mock_sonar  # noqa: E402

FIXTURES = SCRIPTS / "demo" / "fixtures"
_STRIP = ("BOUNCE_", "CS_", "IHN_", "SLOW_", "NIGHT_", "CORRELATED_", "SONAR_", "NOC_", "BREAKER_",
          "DECISION_", "ORG_", "CHAT_")


def py(script, *argv, env, cwd=None):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *argv], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=90)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "demo-" + secrets.token_hex(16)
        cls.srv = mock_sonar.MockSonarServer(FIXTURES, [cls.token], port=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.srv.engine.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="sonar-scripts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP) and "proxy" not in k.lower()}
        env.update({"NO_PROXY": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1",
                    "SONAR_BASE_URL": self.srv.url, "SONAR_TOKEN": self.token,
                    "NOC_STATE_DIR": str(self.tmp / "state"),
                    "NOC_DOTENV": str(self.tmp / "absent.env"),
                    "NOC_PROFILE_FILE": str(self.tmp / "profile.json")})
        self.env = env

    def audit(self):
        return self.srv.engine.audit_snapshot()["audit"]

    def post(self, html, *flags, ticket="91001"):
        f = self.tmp / "c.html"
        f.write_text(html)
        return py("sonar_comment.py", ticket, str(f), *flags, env=self.env)

    def stored_body(self):
        comments = [c for c in self.srv.engine.comments if c["ticket_id"] == 91001]
        return comments[-1]["body"]


GOOD = "<p><strong>Direct Path Analysis - Alpha</strong></p><p>Status: HEALTHY, every hop is green and fast.</p>"


class TestSonarComment(Base):
    def test_post_and_verify(self):
        r = self.post(GOOD, "--verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Comment posted:", r.stdout)
        self.assertIn("Verified: body OK", r.stdout)

    def test_hostile_body_cannot_alter_the_operation(self):
        hostile = ('<p>Direct Path Analysis - "} } mutation { updateTicket(id: 91002, input: {status: CLOSED}) '
                   '{ id } } #</p><p>back\\slash \\" \\\\ and é✓ padding padding</p>')
        r = self.post(hostile, "--verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        fields = [a["field"] for a in self.audit()]
        self.assertEqual(fields, ["createTicketComment"])        # no injected updateTicket
        self.assertEqual(self.stored_body(), hostile)             # stored byte-for-byte

    def test_raw_style_quotes_no_longer_rejected(self):
        html = '<p><span style="color:red">Direct Path Analysis - bottleneck here</span> and more padding text</p>'
        r = self.post(html)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stored_body(), html)

    def test_quot_entities_still_accepted_unchanged(self):
        html = "<p><span style=&quot;color:red&quot;>Direct Path Analysis - bottleneck</span> padding padding</p>"
        self.assertEqual(self.post(html).returncode, 0)
        self.assertEqual(self.stored_body(), html)

    def test_newlines_collapse_by_default_and_preserve_on_request(self):
        multi = "<p>Direct Path Analysis - Alpha</p>\n<p>line two with enough padding to pass</p>\n\t<p>x</p>"
        self.assertEqual(self.post(multi, "--verify").returncode, 0)
        body = self.stored_body()
        self.assertNotIn("\n", body)
        self.assertNotIn("\t", body)
        r = self.post(multi, "--preserve-newlines", "--verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\n", self.stored_body())                  # variables carry newlines fine
        self.assertEqual(self.stored_body(), multi)

    def test_ticket_id_must_be_integer(self):
        for bad in ("91001; x", "abc", "-1", "0", "1.5", ""):
            r = self.post(GOOD, ticket=bad)
            self.assertEqual(r.returncode, 1, (bad, r.stderr))
        self.assertEqual(self.audit(), [])

    def test_input_guards(self):
        self.assertEqual(self.post("<p>BodyPlaceholder " + "x" * 80 + "</p>").returncode, 2)
        self.assertEqual(self.post("<p>tiny</p>").returncode, 2)
        self.assertEqual(self.audit(), [])

    def test_unknown_ticket_is_api_error(self):
        r = self.post(GOOD, ticket="99999")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Sonar API", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_token(self):
        del self.env["SONAR_TOKEN"]
        r = self.post(GOOD)
        self.assertEqual(r.returncode, 2)
        self.assertIn("SONAR_TOKEN", r.stderr)

    def test_token_from_dotenv_file(self):
        del self.env["SONAR_TOKEN"]
        (self.tmp / "absent.env").write_text(f'export SONAR_TOKEN="{self.token}"\n')
        self.assertEqual(self.post(GOOD).returncode, 0)

    def test_bad_token_never_echoed(self):
        self.env["SONAR_TOKEN"] = "demo-" + secrets.token_hex(8)
        r = self.post(GOOD)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(self.env["SONAR_TOKEN"], r.stderr + r.stdout)

    def test_plain_http_to_remote_host_refused_before_sending(self):
        self.env["SONAR_BASE_URL"] = "http://sonar.example.invalid"
        r = self.post(GOOD)
        self.assertEqual(r.returncode, 2)
        self.assertIn("plain http", r.stderr)

    def test_no_curl_no_shell_in_ops_scripts(self):
        forbidden = [r"\bcurl\b", r"shell\s*=\s*True", r"bash\s*,\s*\"-c\"", r"source \{", r"sshpass[\"', ]+-p\b",
                     r"StrictHostKeyChecking=no"]
        for name in ("sonar_comment.py", "sonar_bounce.py", "metrics_rollup.py", "chat_alert.py",
                     "gql_client.py", "circuit_breaker.py", "decision_log.py", "envfile.py"):
            src = (SCRIPTS / name).read_text()
            code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
            code = re.sub(r'""".*?"""', "", code, flags=re.S)
            for pat in forbidden:
                self.assertIsNone(re.search(pat, code), (name, pat))


class _FixedHandler(BaseHTTPRequestHandler):
    seen = None

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or 0)
        self.rfile.read(n)
        self.server.seen.append(dict(self.headers))
        self.server.respond(self)

    def log_message(self, *a):
        pass


def serve(respond):
    httpd = HTTPServer(("127.0.0.1", 0), _FixedHandler)
    httpd.seen = []
    httpd.respond = respond
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def json_reply(obj, status=200):
    def respond(h):
        body = json.dumps(obj).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return respond


class TestClientEdgeCases(Base):
    def run_against(self, respond):
        httpd = serve(respond)
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        self.env["SONAR_BASE_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        return httpd, self.post(GOOD, "--verify")

    def test_data_null_and_odd_shapes_exit_2_without_traceback(self):
        for obj in ({"data": None}, {}, {"data": {"createTicketComment": None}},
                    {"data": {"createTicketComment": {}}}, {"errors": [{"message": "nope"}], "data": None},
                    [1, 2]):
            _, r = self.run_against(json_reply(obj))
            self.assertEqual(r.returncode, 2, (obj, r.stderr))
            self.assertNotIn("Traceback", r.stderr)

    def test_http_500_and_garbage_exit_2(self):
        _, r = self.run_against(json_reply({"oops": 1}, status=500))
        self.assertEqual(r.returncode, 2)

        def garbage(h):
            h.send_response(200)
            h.send_header("Content-Length", "6")
            h.end_headers()
            h.wfile.write(b"<html>")
        _, r = self.run_against(garbage)
        self.assertEqual(r.returncode, 2)

    def test_verify_fetch_failure_is_warning_only(self):
        calls = []

        def respond(h):
            calls.append(1)
            if len(calls) == 1:
                json_reply({"data": {"createTicketComment": {"id": 5}}})(h)
            else:
                json_reply({"data": None})(h)
        _, r = self.run_against(respond)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: verify fetch failed", r.stderr)

    def test_redirects_are_not_followed_and_token_is_not_forwarded(self):
        target = serve(json_reply({"data": {"createTicketComment": {"id": 1}}}))
        self.addCleanup(lambda: (target.shutdown(), target.server_close()))
        loc = f"http://127.0.0.1:{target.server_address[1]}/api/graphql"

        def redirect(h):
            h.send_response(307)
            h.send_header("Location", loc)
            h.send_header("Content-Length", "0")
            h.end_headers()
        _, r = self.run_against(redirect)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(target.seen, [])                         # never contacted, so no bearer leak

    def test_request_shape_uses_variables_and_bearer_header(self):
        captured = {}

        def respond(h):
            captured["auth"] = h.headers.get("Authorization")
            json_reply({"data": {"createTicketComment": {"id": 5}}})(h)
        httpd = serve(respond)
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        old = dict(os.environ)
        os.environ.update({"SONAR_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}", "SONAR_TOKEN": "t-" + "z" * 8,
                           "NOC_DOTENV": str(self.tmp / "absent.env"), "NOC_PROFILE_FILE": str(self.tmp / "p.json")})
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        data = gql_client.execute("query Q($i: Int64Bit!) { x }", {"i": 7})
        self.assertEqual(data, {"createTicketComment": {"id": 5}})
        self.assertEqual(captured["auth"], "Bearer t-zzzzzzzz")


class TestMetricsRollup(Base):
    def log_result(self, ticket, status):
        r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "decision_log.py"), "append", "--json",
                            json.dumps({"status": status}), "--kind", "batch_result", "--ticket", ticket],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def rollup(self):
        return py("metrics_rollup.py", "--no-chat", env=self.env)

    def test_revert_detected_with_profile_resolved_noc_group(self):
        (self.tmp / "profile.json").write_text(json.dumps({"sonar": {"groups": {"noc": {"id": 41}}}}))
        self.log_result("91001", "auto_bounced_ihn")       # fixture: still in NOC group 41 => reverted
        r = self.rollup()
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout[:r.stdout.rindex("}") + 1])
        self.assertEqual(report["reverted_ticket_ids"], ["91001"])
        self.assertTrue((self.tmp / "state" / "metrics").glob("weekly_*.json"))
        breaker = json.loads((self.tmp / "state" / "circuit_breaker.json").read_text())
        self.assertEqual([x["outcome"] for x in breaker["runs"]], ["bounce_reverted"])

    def test_not_reverted_when_ticket_left_noc(self):
        (self.tmp / "profile.json").write_text(json.dumps({"sonar": {"groups": {"noc": {"id": 99}}}}))
        self.log_result("91001", "auto_bounced_ihn")
        report = json.loads(self.rollup().stdout.split("\nWritten")[0])
        self.assertEqual(report["reverted_ticket_ids"], [])

    def test_unresolved_noc_group_skips_the_check_instead_of_guessing(self):
        self.log_result("91001", "auto_bounced_ihn")
        r = self.rollup()
        self.assertIn("revert check skipped", r.stderr)
        self.assertFalse((self.tmp / "state" / "circuit_breaker.json").exists())

    def test_api_failure_records_nothing(self):
        (self.tmp / "profile.json").write_text(json.dumps({"sonar": {"groups": {"noc": {"id": 41}}}}))
        self.log_result("91001", "auto_bounced_ihn")
        httpd = serve(json_reply({"data": None}))
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        self.env["SONAR_BASE_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        r = self.rollup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.tmp / "state" / "circuit_breaker.json").exists())

    def test_incident_id_must_be_integer(self):
        r = py("metrics_rollup.py", "--no-chat", "--incident", "../../etc/x", env=self.env)
        self.assertEqual(r.returncode, 1)

    def test_chat_report_goes_through_urllib_to_the_sink(self):
        self.env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = self.srv.url + "/chat/webhook"
        r = py("metrics_rollup.py", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        status, body = _get(self.srv.url + "/__chat", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["chat"]), 1)


def _get(url, token):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with opener.open(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


class TestChatAlert(Base):
    def setUp(self):
        super().setUp()
        self.env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = self.srv.url + "/chat/webhook"
        self.env["CHAT_ICON_VERIFY"] = "0"

    def test_ticket_alert_delivered(self):
        r = py("chat_alert.py", "ticket", "--ticket-id", "91001", "--subject", "x", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        _, body = _get(self.srv.url + "/__chat", self.token)
        self.assertEqual(len(body["chat"]), 1)

    def test_ticket_id_must_be_integer(self):
        r = py("chat_alert.py", "ticket", "--ticket-id", "9100/../x", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("integer", r.stderr)

    def test_bad_button_scheme_rejected(self):
        r = py("chat_alert.py", "alert", "--title", "t", "--button", "Go:javascript:alert(1)", env=self.env)
        self.assertEqual(r.returncode, 1)

    def test_webhook_from_dotenv_and_card_id_is_stable(self):
        url = self.env.pop("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL")
        (self.tmp / "absent.env").write_text(f"NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL='{url}'\r\n")
        outs = []
        for _ in range(2):
            r = py("chat_alert.py", "alert", "--title", "Breaker", "--dry-run", env=self.env)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append(json.loads(r.stdout)["cardsV2"][0]["cardId"])
        self.assertEqual(outs[0], outs[1])                       # hash() used to be salted per process

    def test_unset_webhook_exit_4_and_http_webhook_refused(self):
        del self.env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"]
        self.assertEqual(py("chat_alert.py", "alert", "--title", "t", env=self.env).returncode, 4)
        self.env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = "http://chat.example.invalid/hook?key=SECRETKEY"
        r = py("chat_alert.py", "alert", "--title", "t", env=self.env)
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("SECRETKEY", r.stderr)

    def test_unreachable_webhook_error_hides_url(self):
        self.env["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = "http://127.0.0.1:9/hook?key=SECRETKEY"
        r = py("chat_alert.py", "alert", "--title", "t", env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("SECRETKEY", r.stderr + r.stdout)

    def test_selftest(self):
        self.assertEqual(py("chat_alert.py", "--selftest", env=self.env).returncode, 0)


class TestGqlCli(Base):
    def run_cli(self, query, *argv):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "gql_client.py"), *argv], input=query,
                              capture_output=True, text=True, env=self.env, timeout=90)

    def test_reads_a_query_from_stdin_and_prints_data(self):
        q = 'query($t: Int64Bit!) { ticket_comments(search: {integer_fields: [{attribute: "ticket_id", operator: EQ, search_value: $t}]}, paginator: {page: 1, records_per_page: 2}) { entities { id } } }'
        r = self.run_cli(q, "--vars", '{"t": 91001}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ticket_comments", json.loads(r.stdout))

    def test_token_is_not_on_the_command_line_or_in_output(self):
        r = self.run_cli("{ __typename }")
        self.assertNotIn(self.token, r.stdout + r.stderr)

    def test_mutation_refused_unless_allowed(self):
        r = self.run_cli("mutation { x }")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing a mutation", r.stderr)

    def test_bad_input_exits_1(self):
        self.assertEqual(self.run_cli("", ).returncode, 1)
        self.assertEqual(self.run_cli("{ a }", "--vars", "[1]").returncode, 1)

    def test_api_error_exits_2_without_traceback(self):
        self.env["SONAR_TOKEN"] = "wrong-" + "x" * 12
        r = self.run_cli("{ __typename }")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("wrong-", r.stderr)


if __name__ == "__main__":
    unittest.main()
