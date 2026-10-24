#!/usr/bin/env python3
"""Tests for scripts/notify.py, its use from scripts/chat_alert.py and the doctor `notify` row.

Everything runs against a local http.server on 127.0.0.1 (ephemeral port): no network.
Fake webhook URLs and tokens are built at runtime so no secret-looking literal sits in the file.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import chat_alert  # noqa: E402
import doctor  # noqa: E402
import notify  # noqa: E402

GOLDEN_ALERT_ERROR = (
    '{"cardsV2": [{"cardId": "noc-alert-78f73a80a6", "card": {"header": {"title": "\\u274c Breaker open", '
    '"imageAltText": "Error", "imageType": "CIRCLE"}, "sections": [{"widgets": [{"buttonList": {"buttons": '
    '[{"text": "Open", "onClick": {"openLink": {"url": "https://example.com/run/1"}}}]}}]}, {"widgets": '
    '[{"textParagraph": {"text": "Failure rate 40%"}}]}]}}]}'
)
GOLDEN_ALERT_ROWS = (
    '{"cardsV2": [{"cardId": "noc-alert-2975132481", "card": {"header": {"title": "\\ud83d\\udea8 Weekly", '
    '"imageAltText": "Needs Attention", "imageType": "CIRCLE", "subtitle": "sub"}, "sections": [{"widgets": '
    '[{"textParagraph": {"text": "<b>Key:</b> val"}}]}, {"widgets": [{"textParagraph": {"text": "n"}}]}]}}]}'
)
GOLDEN_TICKET = (
    '{"cardsV2": [{"cardId": "noc-ticket-12", "card": {"header": {"title": "\\ud83d\\udea8 Ticket #12 \\u2014 '
    'Needs Human Review", "imageAltText": "Needs Attention", "imageType": "CIRCLE", "subtitle": "s"}, '
    '"sections": [{"widgets": [{"textParagraph": {"text": "<b>Source:</b> Transferred to NOC group"}}, '
    '{"textParagraph": {"text": "<b>Priority:</b> HIGH"}}, {"textParagraph": {"text": "<b>Issue:</b> i"}}]}, '
    '{"widgets": [{"buttonList": {"buttons": [{"text": "VIEW TICKET", "onClick": {"openLink": {"url": '
    '"https://sonar.example.com/app#/tickets/show/12"}}}]}}]}]}}], "text": "NOC ticket #12 \\u2014 '
    'Needs Human Review \\u2014 s"}'
)


class Hook:
    """A local webhook endpoint that records every request."""

    def __init__(self, status=200, behavior="ok", redirect_to=None):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
                if behavior == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                else:
                    self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.secret = "k" + os.urandom(8).hex()  # stands in for the key/token embedded in a real webhook URL
        self.url = f"http://127.0.0.1:{self.port}/hook/{self.secret}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NotifyCase(unittest.TestCase):
    def setUp(self):
        env = {"PATH": os.environ.get("PATH", ""), "NOC_DOTENV": os.devnull, "CHAT_ICON_VERIFY": "0"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = mock.patch.object(notify, "RETRY_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)
        quiet = mock.patch.object(sys, "stderr", io.StringIO())  # send() logs one line per call
        quiet.start()
        self.addCleanup(quiet.stop)
        self.hooks = []
        self.addCleanup(lambda: [h.close() for h in self.hooks])

    def hook(self, **kw):
        h = Hook(**kw)
        self.hooks.append(h)
        return h

    def use(self, hook, provider=None):
        os.environ["NOTIFY_WEBHOOK_URL"] = hook.url
        if provider:
            os.environ["NOTIFY_PROVIDER"] = provider

    def capture(self, fn, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kw)
        return result, out.getvalue(), err.getvalue()


class ProviderShapes(NotifyCase):
    def test_slack(self):
        h = self.hook()
        self.use(h, "slack")
        self.assertTrue(notify.send("Breaker open", "Rate 40% <!channel> a&b", level="warn", link="https://example.com/x"))
        (r,) = h.requests
        self.assertEqual(r["path"], "/hook/" + h.secret)
        self.assertEqual(r["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(r["body"]), {
            "text": "[WARN] Breaker open\nRate 40% &lt;!channel&gt; a&amp;b\nhttps://example.com/x"})

    def test_discord_truncates_and_disables_mentions(self):
        h = self.hook(status=204)
        self.use(h, "discord")
        self.assertTrue(notify.send("T", "x" * 5000, level="critical"))
        payload = json.loads(h.requests[0]["body"])
        self.assertEqual(set(payload), {"content", "allowed_mentions"})
        self.assertEqual(len(payload["content"]), 2000)
        self.assertTrue(payload["content"].startswith("[CRITICAL] T\nxxx"))
        self.assertTrue(payload["content"].endswith("..."))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_teams(self):
        h = self.hook()
        self.use(h, "teams")
        self.assertTrue(notify.send("Title", "Body"))
        self.assertEqual(json.loads(h.requests[0]["body"]), {"text": "Title\nBody"})

    def test_ntfy_headers_and_raw_body(self):
        h = self.hook()
        self.use(h, "ntfy")
        self.assertTrue(notify.send("Breaker open", "Rate 40%", level="critical", link="https://example.com/x"))
        r = h.requests[0]
        self.assertEqual(r["body"], b"Rate 40%")
        self.assertEqual(r["headers"]["Title"], "Breaker open")
        self.assertEqual(r["headers"]["Priority"], "urgent")
        self.assertEqual(r["headers"]["Click"], "https://example.com/x")
        self.assertNotIn("Authorization", r["headers"])

    def test_ntfy_token_is_a_bearer_header(self):
        h = self.hook()
        self.use(h, "ntfy")
        token = "tk_" + os.urandom(6).hex()
        os.environ["NOTIFY_NTFY_TOKEN"] = token
        self.assertTrue(notify.send("T", "b"))
        self.assertEqual(h.requests[0]["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual(h.requests[0]["headers"]["Priority"], "default")

    def test_ntfy_non_ascii_title_and_byte_cap(self):
        h = self.hook()
        self.use(h, "ntfy")
        self.assertTrue(notify.send("Tour défectueuse", "é" * 5000, level="warn"))
        r = h.requests[0]
        self.assertTrue(r["headers"]["Title"].startswith("=?UTF-8?B?"))
        self.assertLessEqual(len(r["body"]), 4000)
        r["body"].decode("utf-8")  # still valid UTF-8 after the cut
        self.assertEqual(r["headers"]["Priority"], "high")

    def test_ntfy_without_body_sends_the_title(self):
        h = self.hook()
        self.use(h, "ntfy")
        notify.send("Only a title")
        self.assertEqual(h.requests[0]["body"], b"Only a title")

    def test_stdout_prints_and_sends_nothing(self):
        ok, out, err = self.capture(notify.send, "Hello", "World", level="warn", provider="stdout")
        self.assertTrue(ok)
        self.assertEqual(out, "[WARN] Hello\nWorld\n")
        self.assertEqual(err, "")

    def test_control_characters_are_stripped(self):
        h = self.hook()
        self.use(h, "slack")
        notify.send("Ti\x00tle\x1b[31m\r\nX-Evil: 1", "bo\x07dy\x7f\ttab\nline")
        text = json.loads(h.requests[0]["body"])["text"]
        self.assertEqual(text, "Title[31m X-Evil: 1\nbody\ttab\nline")
        h2 = self.hook()
        self.use(h2, "ntfy")
        notify.send("a\r\nX-Evil: 1", "b")
        self.assertEqual(h2.requests[0]["headers"]["Title"], "a X-Evil: 1")
        self.assertNotIn("X-Evil", h2.requests[0]["headers"])

    def test_non_http_link_is_dropped(self):
        h = self.hook()
        self.use(h, "slack")
        notify.send("T", "b", link="javascript:alert(1)")
        self.assertEqual(json.loads(h.requests[0]["body"])["text"], "T\nb")


class GchatCompat(NotifyCase):
    def alert_ns(self, **kw):
        base = dict(title="Breaker open", subtitle=None, severity="error", rows=[],
                    buttons=["Open:https://example.com/run/1"], note="Failure rate 40%")
        base.update(kw)
        return type("A", (), base)()

    def test_gchat_payload_is_byte_identical_to_the_card_builder(self):
        h = self.hook()
        self.use(h)  # no NOTIFY_PROVIDER: the default is gchat
        self.assertTrue(notify.send("Breaker open", "Failure rate 40%", level="critical",
                                    link="https://example.com/run/1"))
        raw = h.requests[0]["body"]
        self.assertEqual(raw, json.dumps(chat_alert.build_alert_card(self.alert_ns())).encode())
        self.assertEqual(raw, GOLDEN_ALERT_ERROR.encode())
        self.assertEqual(h.requests[0]["headers"]["Content-Type"], "application/json")

    def test_gchat_falls_back_to_the_existing_variable(self):
        h = self.hook()
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = h.url
        self.assertTrue(notify.send("Breaker open", "Failure rate 40%", level="critical",
                                    link="https://example.com/run/1"))
        self.assertEqual(h.requests[0]["body"], GOLDEN_ALERT_ERROR.encode())

    def test_other_providers_never_use_the_google_chat_variable(self):
        h = self.hook()
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = h.url
        ok, out, err = self.capture(notify.send, "T", "b", provider="slack")
        self.assertFalse(ok)
        self.assertEqual(h.requests, [])
        self.assertIn("not configured", err)

    def test_chat_alert_default_path_is_unchanged(self):
        h = self.hook()
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = h.url
        os.environ["ORG_SONAR_BASE_URL"] = "https://sonar.example.com"
        cases = [
            (["alert", "--title", "Breaker open", "--severity", "error", "--note", "Failure rate 40%",
              "--button", "Open:https://example.com/run/1"], GOLDEN_ALERT_ERROR),
            (["alert", "--title", "Weekly", "--subtitle", "sub", "--row", "Key:val", "--note", "n"], GOLDEN_ALERT_ROWS),
            (["ticket", "--ticket-id", "12", "--subject", "s", "--priority", "HIGH", "--issue", "i"], GOLDEN_TICKET),
        ]
        for provider in (None, "gchat", "GChat"):
            if provider:
                os.environ["NOTIFY_PROVIDER"] = provider
            for argv, golden in cases:
                h.requests.clear()
                with mock.patch.object(sys, "argv", ["chat_alert.py"] + argv):
                    rc, _, _ = self.capture(chat_alert.main)
                self.assertEqual(rc, 0, (provider, argv))
                self.assertEqual(h.requests[0]["body"], golden.encode(), (provider, argv))

    def test_chat_alert_unconfigured_default_still_exits_4(self):
        with mock.patch.object(sys, "argv", ["chat_alert.py", "alert", "--title", "x"]):
            rc, _, err = self.capture(chat_alert.main)
        self.assertEqual(rc, 4)
        self.assertIn("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL not set", err)


class ChatAlertRouting(NotifyCase):
    def run_cli(self, *argv):
        with mock.patch.object(sys, "argv", ["chat_alert.py"] + list(argv)):
            return self.capture(chat_alert.main)

    def test_alert_goes_to_slack_as_plain_text(self):
        chat = self.hook()
        slack = self.hook()
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = chat.url
        self.use(slack, "slack")
        rc, _, _ = self.run_cli("alert", "--title", "Breaker open", "--severity", "error", "--subtitle", "Auto-bounces blocked",
                                "--row", "Rate:40%", "--note", "see runbook", "--button", "Open:https://example.com/run/1")
        self.assertEqual(rc, 0)
        self.assertEqual(chat.requests, [])
        self.assertEqual(json.loads(slack.requests[0]["body"]), {
            "text": "[CRITICAL] Breaker open\nAuto-bounces blocked\nRate: 40%\nsee runbook\nhttps://example.com/run/1"})

    def test_ticket_goes_to_ntfy_with_the_ticket_link(self):
        ntfy = self.hook()
        self.use(ntfy, "ntfy")
        os.environ["ORG_SONAR_BASE_URL"] = "https://sonar.example.com"
        rc, _, _ = self.run_cli("ticket", "--ticket-id", "12", "--subject", "s", "--issue", "a<br>b &amp; c",
                                "--evidence", "e1", "--confidence", "70")
        self.assertEqual(rc, 0)
        r = ntfy.requests[0]
        self.assertEqual(r["headers"]["Title"], "Ticket #12 - Needs Human Review")
        self.assertEqual(r["headers"]["Click"], "https://sonar.example.com/app#/tickets/show/12")
        self.assertEqual(r["headers"]["Priority"], "high")
        self.assertEqual(r["body"].decode(), "s\nSource: Transferred to NOC group\nPriority: Medium\n"
                                             "Issue: a\nb & c\nConfidence: 70%\nEvidence:\n- e1")

    def test_exit_codes(self):
        os.environ["NOTIFY_PROVIDER"] = "slack"
        rc, _, err = self.run_cli("alert", "--title", "x")
        self.assertEqual(rc, 4)
        self.assertIn("NOTIFY_WEBHOOK_URL is not set", err)
        rc, _, err = self.run_cli("ticket", "--ticket-id", "abc")
        self.assertEqual(rc, 1)
        bad = self.hook(status=500)
        self.use(bad, "slack")
        rc, out, err = self.run_cli("alert", "--title", "x")
        self.assertEqual(rc, 2)
        self.assertIn("provider=slack", err)
        self.assertNotIn(bad.secret, out + err)
        os.environ["NOTIFY_PROVIDER"] = "carrier-pigeon"
        rc, _, err = self.run_cli("alert", "--title", "x")
        self.assertEqual(rc, 4)
        self.assertNotIn("carrier-pigeon", err)

    def test_dry_run_prints_no_url(self):
        h = self.hook()
        self.use(h, "discord")
        rc, out, err = self.run_cli("alert", "--title", "x", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(h.requests, [])
        self.assertNotIn(h.secret, out + err)
        self.assertEqual(json.loads(out)["provider"], "discord")


class Safety(NotifyCase):
    def test_https_only_unless_loopback_or_allowed(self):
        self.assertTrue(notify.url_allowed("https://example.com/h"))
        self.assertTrue(notify.url_allowed("http://127.0.0.1:9/h"))
        self.assertTrue(notify.url_allowed("http://localhost/h"))
        self.assertFalse(notify.url_allowed("http://example.com/h"))
        self.assertFalse(notify.url_allowed("ftp://example.com/h"))
        self.assertFalse(notify.url_allowed("https:///nohost"))
        self.assertFalse(notify.url_allowed(""))
        os.environ["NOTIFY_ALLOW_HTTP"] = "1"
        self.assertTrue(notify.url_allowed("http://ntfy.lan.example.com/topic"))
        self.assertFalse(notify.url_allowed("ftp://example.com/h"))

    def test_plain_http_to_a_remote_host_is_refused_without_a_request(self):
        remote = "http://example.com/hook/" + os.urandom(6).hex()
        with mock.patch.object(notify, "_post") as post:
            ok, detail = notify.deliver("T", "b", provider="slack", webhook=remote)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("refused"))
        post.assert_not_called()
        os.environ["NOTIFY_WEBHOOK_URL"] = remote
        ok, out, err = self.capture(notify.send, "T", "b", provider="slack")
        self.assertFalse(ok)
        self.assertNotIn(remote, out + err)
        self.assertNotIn(remote.rsplit("/", 1)[1], out + err)

    def test_the_url_and_token_never_reach_output(self):
        token = "tk_" + os.urandom(6).hex()
        os.environ["NOTIFY_NTFY_TOKEN"] = token
        scenarios = [
            ("ok", self.hook(status=200)),
            ("http 500", self.hook(status=500)),
            ("dropped", self.hook(behavior="drop")),
        ]
        dead = self.hook()
        dead.close()
        scenarios.append(("refused connection", dead))
        for name, h in scenarios:
            for provider in ("slack", "ntfy", "gchat", "discord", "teams"):
                os.environ["NOTIFY_WEBHOOK_URL"] = h.url
                ok, out, err = self.capture(notify.send, "T", "b", provider=provider)
                self.assertNotIn(h.secret, out + err, (name, provider))
                self.assertNotIn(token, out + err, (name, provider))
                self.assertIn(f"provider={provider}", err)
                self.assertEqual(ok, name == "ok", (name, provider))
        # the detail strings are clean as well
        for name, h in scenarios:
            _, detail = notify.deliver("T", "b", provider="ntfy", webhook=h.url, ntfy_token=token)
            self.assertNotIn(h.secret, detail)
            self.assertNotIn(token, detail)

    def test_http_error_returns_false_without_retry(self):
        h = self.hook(status=500)
        self.use(h, "slack")
        self.assertFalse(notify.send("T", "b"))
        self.assertEqual(len(h.requests), 1)

    def test_network_error_is_retried_once(self):
        h = self.hook(behavior="drop")
        self.use(h, "slack")
        ok, detail = notify.deliver("T", "b")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("network error"))
        self.assertEqual(len(h.requests), 2)

    def test_redirects_are_not_followed(self):
        target = self.hook()
        h = self.hook(redirect_to=target.url)
        self.use(h, "ntfy")
        os.environ["NOTIFY_NTFY_TOKEN"] = "tk_" + os.urandom(4).hex()
        self.assertFalse(notify.send("T", "b"))
        self.assertEqual(target.requests, [])
        self.assertEqual(len(h.requests), 1)

    def test_unconfigured_and_unknown_provider_return_false(self):
        for provider in ("slack", "ntfy", "gchat"):
            ok, detail = notify.deliver("T", "b", provider=provider)
            self.assertFalse(ok)
            self.assertIn("not configured", detail)
        ok, detail = notify.deliver("T", "b", provider="carrier-pigeon")
        self.assertFalse(ok)
        self.assertNotIn("carrier-pigeon", detail)
        self.assertFalse(notify.configured("carrier-pigeon"))
        self.assertTrue(notify.configured("stdout"))

    def test_send_never_raises(self):
        self.use(self.hook(), "slack")
        with mock.patch.object(notify, "_post", side_effect=RuntimeError("boom")):
            ok, out, err = self.capture(notify.send, "T", "b")
        self.assertFalse(ok)
        self.assertIn("internal error (RuntimeError)", err)
        with mock.patch.object(notify, "clean", side_effect=ValueError("boom")):
            self.assertFalse(self.capture(notify.send, "T", "b")[0])

    def test_cli_exit_codes_and_selftest(self):
        h = self.hook()
        self.use(h, "slack")
        self.assertEqual(self.capture(notify.main, ["--title", "T", "--body", "b", "--level", "warn"])[0], 0)
        self.assertEqual(json.loads(h.requests[0]["body"])["text"], "[WARN] T\nb")
        self.assertEqual(self.capture(notify.main, ["--title", "T", "--provider", "stdout"])[0], 0)
        os.environ.pop("NOTIFY_WEBHOOK_URL")
        self.assertEqual(self.capture(notify.main, ["--title", "T"])[0], 1)
        proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "notify.py"), "--selftest"],
                              capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("selftest passed", proc.stdout)


class DoctorNotify(NotifyCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="notify-doctor-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = mock.patch.object(doctor, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["HOME"] = str(self.root)

    def rows(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = doctor.run(["--json", "--only", "notify", *argv])
        text = out.getvalue() + err.getvalue()
        data = json.loads(out.getvalue())
        return code, [r for r in data["results"] if r["name"] == "notify"], text

    def test_unset_and_default_provider_are_optional_and_send_nothing(self):
        h = self.hook()
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = h.url
        for provider in (None, "gchat"):
            if provider:
                os.environ["NOTIFY_PROVIDER"] = provider
            code, rows, text = self.rows("--notify")
            self.assertEqual(code, 0, text)
            self.assertEqual(rows[0]["required"], False)
            self.assertEqual(rows[0]["tier"], 2)
            self.assertIn(rows[0]["status"], ("unconfigured", "skipped"))
            self.assertNotIn(h.secret, text)
        self.assertEqual(h.requests, [])

    def test_configured_provider_is_named_but_not_tested_without_notify(self):
        h = self.hook()
        self.use(h, "slack")
        code, rows, text = self.rows()
        self.assertEqual(code, 0, text)
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertIn("slack", rows[0]["detail"])
        self.assertIn("--notify", text)
        self.assertNotIn(h.secret, text)
        self.assertEqual(h.requests, [])

    def test_notify_flag_sends_one_labeled_message_and_prints_no_secret(self):
        token = "tk_" + os.urandom(6).hex()
        for provider in ("slack", "ntfy"):
            h = self.hook()
            self.use(h, provider)
            os.environ["NOTIFY_NTFY_TOKEN"] = token
            code, rows, text = self.rows("--notify")
            self.assertEqual(code, 0, text)
            self.assertEqual(rows[0]["status"], "pass")
            self.assertIn(f"provider {provider}", rows[0]["detail"])
            self.assertEqual(len(h.requests), 1)
            self.assertIn(b"this is a test message", h.requests[0]["body"])
            self.assertNotIn(h.secret, text)
            self.assertNotIn(token, text)

    def test_failures_and_bad_config_stay_clean(self):
        bad = self.hook(status=500)
        self.use(bad, "discord")
        code, rows, text = self.rows("--notify")
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(rows[0]["required"], False)
        self.assertNotIn(bad.secret, text)
        os.environ["NOTIFY_PROVIDER"] = "carrier-pigeon"
        _, rows, text = self.rows()
        self.assertEqual(rows[0]["status"], "warn")
        self.assertNotIn("carrier-pigeon", text)
        os.environ["NOTIFY_PROVIDER"] = "ntfy"
        os.environ["NOTIFY_WEBHOOK_URL"] = "http://example.com/" + os.urandom(6).hex()
        _, rows, text = self.rows("--notify")
        self.assertEqual(rows[0]["status"], "fail")
        self.assertNotIn(os.environ["NOTIFY_WEBHOOK_URL"].rsplit("/", 1)[1], text)
        os.environ.pop("NOTIFY_WEBHOOK_URL")
        _, rows, _ = self.rows()
        self.assertEqual(rows[0]["status"], "unconfigured")


if __name__ == "__main__":
    unittest.main()
