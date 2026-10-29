"""Tests for scripts/pack.py (local bare repositories as the private remote; nothing leaves the temp directory)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermetic_env  # noqa: E402,F401  (drops GIT_* variables a hook may have exported)
import noc_config  # noqa: E402
import pack  # noqa: E402

TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"      # shaped like a GitHub token, built at run time
PROFILE = {"org": {"name": "Example Org"}, "resources": {"uisp": {"url": "https://uisp.corp-net.org"}},
           "sonar": {"groups": {"cs_level_2": {"id": 2}}}}
ROSTER = {"version": 1, "members": {"ada": {"name": "Ada Lead", "role": "lead", "active": True}},
          "pools": {}, "approved_models": []}


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[commit]\n\tgpgsign = false\n"
                             "[init]\n\tdefaultBranch = main\n")
        self.remote = self.root / "remote.git"
        sh("git", "init", "-q", "--bare", str(self.remote), cwd=str(self.root))
        env = {k: v for k, v in os.environ.items() if k not in ("NOC_UNATTENDED", "NOC_OPERATOR", "NOC_PACK_GIT_DIR")}
        env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.root),
                    "NOC_USER_CONFIG": str(self.root / "user.json")})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lead = self.root / "lead" / "site-pack"
        self.mate = self.root / "mate" / "site-pack"

    def at(self, directory):
        return mock.patch.dict(os.environ, {"NOC_SITE_PACK_DIR": str(directory)})

    def seed_lead(self):
        self.lead.mkdir(parents=True)
        (self.lead / "profile.json").write_text(json.dumps(PROFILE))
        (self.lead / "team.json").write_text(json.dumps(ROSTER))
        (self.lead / "devices.md").write_text("| tower | router |\n")
        (self.lead / "topology").mkdir()
        (self.lead / "topology" / "map.md").write_text("fiber map\n")
        (self.lead / "README.md").write_text("public file, not part of the pack\n")
        (self.lead / "profile.json.bak").write_text("{}")
        (self.lead / ".env.local").write_text("X=1\n")

    def run_pack(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO("")):
            code = pack.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def publish(self):
        """A lead initialises the pack and pushes it to the shared remote."""
        self.seed_lead()
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote))[0], 0)
            self.assertEqual(pack.main(["git", "push", "-q", "-u", "origin", "main"]), 0)

    def lead_commits(self, message, **files):
        with self.at(self.lead):
            for rel, text in files.items():
                path = self.lead / rel.replace("__", "/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            self.assertEqual(pack.main(["git", "add", "-A"]), 0)
            self.assertEqual(pack.main(["git", "commit", "-q", "-m", message]), 0)
            self.assertEqual(pack.main(["git", "push", "-q", "origin", "main"]), 0)

    def tracked(self, directory):
        with self.at(directory):
            return set(pack.git("ls-files").stdout.split())


class Init(Base):
    def test_tracks_only_pack_files_and_installs_the_hook(self):
        self.seed_lead()
        with self.at(self.lead):
            code, out, _e = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tracked(self.lead), {"profile.json", "team.json", "devices.md", "topology/map.md"})
        self.assertTrue(os.access(self.lead / ".pack.git" / "hooks" / "pre-commit", os.X_OK))
        self.assertIn("git push -u origin main", out)
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote))[0], 2)

    def test_credentials_stop_init_without_leaving_anything_behind(self):
        self.seed_lead()
        (self.lead / "devices.md").write_text("token = %s\n" % TOKEN)
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 1)
        self.assertIn("devices.md:1 github-token", err)
        self.assertNotIn(TOKEN, out + err)
        self.assertFalse((self.lead / ".pack.git").exists())

    def test_the_first_commit_does_not_wait_for_a_signing_prompt(self):
        gitconfig = Path(os.environ["GIT_CONFIG_GLOBAL"])
        gitconfig.write_text(gitconfig.read_text().replace("gpgsign = false", "gpgsign = true")
                             + "[gpg]\n\tprogram = /bin/false\n[user]\n\tsigningkey = ABCDEF0123456789\n")
        self.seed_lead()
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.lead / ".pack.git" / "HEAD").exists())

    def test_dry_run_lists_files_and_settings_and_writes_nothing(self):
        self.seed_lead()
        with self.at(self.lead):
            code, out, _e = self.run_pack("init", str(self.remote), "--dry-run")
        self.assertEqual(code, 0, out)
        for text in ("4 file(s)", "profile.json", "topology/map.md", "service uisp: https://uisp.corp-net.org", "member ada: lead"):
            self.assertIn(text, out)
        self.assertNotIn("README.md", out)
        self.assertNotIn(".bak", out)
        self.assertFalse((self.lead / ".pack.git").exists())

    def test_dry_run_still_refuses_a_credential(self):
        self.seed_lead()
        (self.lead / "devices.md").write_text("token = %s\n" % TOKEN)
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("devices.md:1 github-token", err)
        self.assertNotIn("Nothing was written", out)

    def test_the_commit_hook_refuses_a_credential(self):
        self.publish()
        with self.at(self.lead):
            (self.lead / "devices.md").write_text("token = %s\n" % TOKEN)
            pack.main(["git", "add", "-A"])
            self.assertNotEqual(pack.main(["git", "commit", "-q", "-m", "oops"]), 0)
            self.assertEqual(pack.git("log", "--oneline").stdout.count("\n"), 1)

    def test_remote_url_must_look_like_a_remote(self):
        self.seed_lead()
        with self.at(self.lead):
            with self.assertRaises(SystemExit):                       # argparse already refuses an option-looking value
                self.run_pack("init", "--upload-pack=x")
            for argv in (("init", "--", "--upload-pack=x"), ("init", "ext::sh -c id"), ("init", "")):
                self.assertEqual(self.run_pack(*argv)[0], 2, argv)
        self.assertFalse((self.lead / ".pack.git").exists())


class Join(Base):
    def test_join_installs_the_pack_and_leaves_other_files_alone(self):
        self.publish()
        self.mate.mkdir(parents=True)
        (self.mate / "README.md").write_text("mine\n")
        with self.at(self.mate):
            code, out, _e = self.run_pack("join", str(self.remote), "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("service uisp added: https://uisp.corp-net.org", out)
        self.assertIn("member ada added: lead", out)
        self.assertEqual(json.loads((self.mate / "profile.json").read_text()), PROFILE)
        self.assertEqual((self.mate / "README.md").read_text(), "mine\n")
        self.assertFalse((self.mate / "profile.json.bak").exists())
        with self.at(self.mate):
            self.assertEqual(self.run_pack("join", str(self.remote), "--yes")[0], 2)
            self.assertEqual(self.run_pack("status")[0], 0)

    def test_nothing_is_written_without_a_yes(self):
        self.publish()
        with self.at(self.mate):
            code, out, err = self.run_pack("join", str(self.remote))
        self.assertEqual(code, 2)
        self.assertIn("service uisp added", out)
        self.assertIn("--yes", err)
        self.assertFalse((self.mate / "profile.json").exists())
        self.assertFalse((self.mate / ".pack.git").exists())

    def test_existing_files_are_never_overwritten(self):
        self.publish()
        self.mate.mkdir(parents=True)
        (self.mate / "profile.json").write_text("mine")
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("profile.json", err)
        self.assertEqual((self.mate / "profile.json").read_text(), "mine")
        self.assertFalse((self.mate / ".pack.git").exists())

    def test_a_pack_may_not_carry_other_paths_or_symlinks(self):
        evil = self.root / "evil"
        sh("git", "init", "-q", str(evil))
        (evil / "README.md").write_text("x")
        sh("git", "add", "-A", cwd=str(evil))
        sh("git", "commit", "-q", "-m", "x", cwd=str(evil))
        sh("git", "push", "-q", str(self.remote), "HEAD:refs/heads/evil", cwd=str(evil))
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--branch", "evil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("README.md is outside what a pack may hold", err)
        (evil / "README.md").unlink()
        (evil / "devices.md").symlink_to("/etc/passwd")
        sh("git", "add", "-A", cwd=str(evil))
        sh("git", "commit", "-q", "-m", "y", cwd=str(evil))
        sh("git", "push", "-q", "-f", str(self.remote), "HEAD:refs/heads/evil", cwd=str(evil))
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--branch", "evil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        self.assertFalse((self.mate / "devices.md").exists())


class Pull(Base):
    def join_mate(self):
        self.publish()
        with self.at(self.mate):
            self.assertEqual(self.run_pack("join", str(self.remote), "--yes")[0], 0)

    def test_pull_names_every_trusted_change_and_waits_for_a_yes(self):
        self.join_mate()
        changed = json.loads(json.dumps(PROFILE))
        changed["resources"]["uisp"]["url"] = "https://uisp.elsewhere.net"
        changed["sonar"]["groups"]["cs_level_2"]["id"] = 9
        roster = json.loads(json.dumps(ROSTER))
        roster["members"]["newbie"] = {"name": "New Person", "role": "viewer", "active": True}
        roster["members"]["ada"]["role"] = "tech"
        self.lead_commits("change", **{"profile.json": json.dumps(changed), "team.json": json.dumps(roster),
                                       "skills__org-x__SKILL.md": "---\nname: org-x\n---\n"})
        with self.at(self.mate):
            code, out, err = self.run_pack("pull")
            self.assertEqual(code, 2)
            for text in ("service uisp changed: https://uisp.corp-net.org -> https://uisp.elsewhere.net",
                         "setting sonar.groups.cs_level_2.id changed: 2 -> 9",
                         "member newbie added: viewer", "member ada changed: lead -> tech",
                         "skill file skills/org-x/SKILL.md"):
                self.assertIn(text, out)
            self.assertIn("--yes", err)
            self.assertEqual(json.loads((self.mate / "profile.json").read_text()), PROFILE)
            self.assertEqual(self.run_pack("pull", "--yes")[0], 0)
            self.assertEqual(json.loads((self.mate / "profile.json").read_text()), changed)
            self.assertTrue((self.mate / "skills" / "org-x" / "SKILL.md").exists())
            self.assertIn("up to date", self.run_pack("pull")[1])

    def test_diverged_copies_are_not_merged(self):
        self.join_mate()
        self.lead_commits("lead change", **{"devices.md": "| a | b |\n"})
        with self.at(self.mate):
            (self.mate / "devices.md").write_text("| mine |\n")
            pack.main(["git", "add", "-A"])
            pack.main(["git", "commit", "-q", "-m", "local"])
            code, _o, err = self.run_pack("pull", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("local commit", err)

    def test_required_signatures_are_enforced(self):
        self.join_mate()
        Path(os.environ["NOC_USER_CONFIG"]).write_text(json.dumps({"pack": {"require_signed": True}}))
        self.lead_commits("unsigned", **{"devices.md": "| a | b |\n"})
        with self.at(self.mate):
            code, _o, err = self.run_pack("pull", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no valid signature", err)
        self.assertEqual((self.mate / "devices.md").read_text(), "| tower | router |\n")


COMMUNITY = "Zx9-community-test-value"  # secret-scan:allow(fictional test value, not a real community string)
RR_PW = "rr-pass-test-4821"


class Localize(Base):
    def seed(self):
        self.seed_lead()
        profile = {"org": {"name": "Example Org", "portability_tokens": ["brandword", COMMUNITY]},
                   "resources": {"uisp": {"url": "https://uisp.corp-net.org"}},
                   "conventions": {"infrastructure-snmp-community": COMMUNITY,
                                   "snmp": {"community": COMMUNITY, "provider": "Example Org", "contact": "noc@example.invalid"}}}
        (self.lead / "profile.json").write_text(json.dumps(profile, indent=2))
        bgp = {"oxidized_base_url": "http://oxidized.corp-net.org:8888", "default_rr": "192.0.2.1",
               "rr_credentials": [{"ip": "192.0.2.1", "password": RR_PW, "fiber_loopback": "192.0.2.33"},
                                  {"ip": "192.0.2.2", "password_env": "BGP_RR2_PASSWORD"},
                                  {"ip": "192.0.2.3", "password": "<rr-password-3>"}]}
        (self.lead / "topology" / "temp-bgp-config.json").write_text(json.dumps(bgp, indent=2))
        self.original_profile, self.original_bgp = profile, bgp

    def localize(self, *extra):
        with self.at(self.lead):
            return self.run_pack("localize", *extra)

    def test_dry_run_lists_paths_changes_nothing_and_never_prints_a_value(self):
        self.seed()
        before = (self.lead / "profile.json").read_text()
        code, out, err = self.localize()
        self.assertEqual(code, 0, err)
        for text in ("conventions.snmp.community", "conventions.infrastructure-snmp-community", "org.portability_tokens[1]",
                     "rr_credentials[0].password", "dry run"):
            self.assertIn(text, out)
        self.assertNotIn("rr_credentials[2]", out)                      # a placeholder is not a secret
        self.assertNotIn("password_env", out)                           # it names a variable, not a value
        for value in (COMMUNITY, RR_PW):
            self.assertNotIn(value, out + err)
        self.assertEqual((self.lead / "profile.json").read_text(), before)
        self.assertFalse((self.lead / "profile.local.json").exists())

    def test_write_moves_secrets_and_the_readers_still_see_the_same_data(self):
        self.seed()
        code, out, err = self.localize("--write")
        self.assertEqual(code, 0, err)
        for value in (COMMUNITY, RR_PW):
            self.assertNotIn(value, out + err)
        for rel in ("profile.json", "topology/temp-bgp-config.json"):
            self.assertNotIn(COMMUNITY, (self.lead / rel).read_text())
            self.assertNotIn(RR_PW, (self.lead / rel).read_text())
            self.assertTrue((self.lead / rel).with_name(Path(rel).stem + ".local.json").exists())
            self.assertTrue(Path(str(self.lead / rel) + ".bak").exists())
        for rel, original in (("profile.json", self.original_profile), ("topology/temp-bgp-config.json", self.original_bgp)):
            self.assertEqual(noc_config.load_json_overlay(self.lead / rel), original)
        self.assertEqual(oct((self.lead / "profile.local.json").stat().st_mode & 0o777), "0o600")
        shared = json.loads((self.lead / "profile.json").read_text())
        self.assertEqual(shared["org"]["portability_tokens"], ["brandword"])
        self.assertEqual(shared["conventions"]["snmp"]["provider"], "Example Org")
        self.assertEqual(json.loads((self.lead / "topology" / "temp-bgp-config.json").read_text())["rr_credentials"][1]["password_env"],
                         "BGP_RR2_PASSWORD")

    def test_second_run_has_nothing_to_do_and_init_then_accepts_the_pack(self):
        self.seed()
        self.assertEqual(self.localize("--write")[0], 0)
        self.assertIn("nothing to move", self.localize()[1])
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("local.json", out)                             # overlays are never tracked

    def test_init_refuses_when_a_pack_file_holds_a_value_that_lives_in_an_overlay(self):
        self.seed()
        self.assertEqual(self.localize("--write")[0], 0)
        (self.lead / "devices.md").write_text("| tower | snmp %s |\n" % COMMUNITY)
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 1)
        self.assertIn("devices.md:1 holds a value that is stored in a .local.json file", err)
        self.assertNotIn(COMMUNITY, out + err)
        self.assertFalse((self.lead / ".pack.git").exists())

    def test_commit_hook_refuses_an_overlay_value_and_overlays_stay_untracked(self):
        self.seed()
        self.assertEqual(self.localize("--write")[0], 0)
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote))[0], 0)
            self.assertFalse(any(".local." in f for f in pack.git("ls-files").stdout.split()))
            (self.lead / "devices.md").write_text("snmp %s\n" % COMMUNITY)
            pack.main(["git", "add", "devices.md"])
            self.assertNotEqual(pack.main(["git", "commit", "-q", "-m", "oops"]), 0)

    def test_a_pulled_pack_may_not_carry_an_overlay_file(self):
        evil = self.root / "evil2"
        sh("git", "init", "-q", str(evil))
        (evil / "profile.json").write_text("{}")
        (evil / "profile.local.json").write_text("{}")
        sh("git", "add", "-A", cwd=str(evil))
        sh("git", "commit", "-q", "-m", "x", cwd=str(evil))
        sh("git", "push", "-q", str(self.remote), "HEAD:refs/heads/evil2", cwd=str(evil))
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--branch", "evil2", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("profile.local.json", err)

    def test_existing_overlay_values_win_and_are_kept(self):
        self.seed()
        (self.lead / "profile.local.json").write_text(json.dumps({"conventions": {"snmp": {"community": "Other-local-value-1"}}}))  # secret-scan:allow(fictional test value)
        self.assertEqual(self.localize("--write")[0], 0)
        merged = noc_config.load_json_overlay(self.lead / "profile.json")
        self.assertEqual(merged["conventions"]["snmp"]["community"], "Other-local-value-1")
        self.assertEqual(merged["conventions"]["infrastructure-snmp-community"], COMMUNITY)

    def seed_leftovers(self):
        self.seed()
        self.assertEqual(self.localize("--write")[0], 0)
        (self.lead / "topology" / "fiber-testing-ips.json").write_text(json.dumps(
            {"net": {"internal": "192.0.2.9", "notes": "user: btest pass: %s" % RR_PW}}, indent=2))
        (self.lead / "topology" / "verified.md").write_text(
            "# probes\n- peer 192.0.2.1:%s ok\n- nothing here\n- again %s and %s\n" % (RR_PW, RR_PW, COMMUNITY))

    def test_scrub_dry_run_lists_file_and_line_but_no_value(self):
        self.seed_leftovers()
        code, out, err = self.localize("--scrub")
        self.assertEqual(code, 0, err)
        self.assertIn("topology/fiber-testing-ips.json: line", out)
        self.assertIn("topology/verified.md: line 2 (1), 4 (2)", out)
        self.assertIn("dry run", out)
        for value in (COMMUNITY, RR_PW):
            self.assertNotIn(value, out + err)
        self.assertIn(RR_PW, (self.lead / "topology" / "verified.md").read_text())

    def test_scrub_write_replaces_values_keeps_a_backup_and_leaves_valid_json(self):
        self.seed_leftovers()
        code, out, err = self.localize("--scrub", "--write")
        self.assertEqual(code, 0, err)
        for value in (COMMUNITY, RR_PW):
            self.assertNotIn(value, out + err)
        md = (self.lead / "topology" / "verified.md").read_text()
        self.assertIn("peer 192.0.2.1:<secret kept locally> ok", md)
        self.assertNotIn(RR_PW, md)
        self.assertNotIn(COMMUNITY, md)
        data = json.loads((self.lead / "topology" / "fiber-testing-ips.json").read_text())
        self.assertEqual(data["net"]["notes"], "user: btest pass: <secret kept locally>")
        self.assertIn(RR_PW, (self.lead / "topology" / "verified.md.pre-scrub.bak").read_text())
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote), "--dry-run")[0], 0)      # the pack is now publishable
        self.assertIn("nothing to scrub", self.localize("--scrub")[1])

    def test_scrub_keeps_the_first_backup_on_a_second_run(self):
        self.seed_leftovers()
        self.assertEqual(self.localize("--scrub", "--write")[0], 0)
        (self.lead / "topology" / "verified.md").write_text("again %s\n" % RR_PW)
        self.assertEqual(self.localize("--scrub", "--write")[0], 0)
        self.assertIn("peer 192.0.2.1:%s" % RR_PW, (self.lead / "topology" / "verified.md.pre-scrub.bak").read_text())

    def test_unattended_scrub_write_is_refused(self):
        self.seed_leftovers()
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            code, _o, _e = self.localize("--scrub", "--write")
        self.assertEqual(code, 4)
        self.assertIn(RR_PW, (self.lead / "topology" / "verified.md").read_text())

    def test_unattended_write_is_refused(self):
        self.seed()
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            code, _o, err = self.localize("--write")
        self.assertEqual(code, 4)
        self.assertIn("refused", err)
        self.assertFalse((self.lead / "profile.local.json").exists())

    def test_broken_overlay_stops_the_move(self):
        self.seed()
        (self.lead / "profile.local.json").write_text("{not json")
        code, _o, err = self.localize("--write")
        self.assertEqual(code, 2)
        self.assertIn("profile.local.json", err)
        self.assertIn(COMMUNITY, (self.lead / "profile.json").read_text())


class DeployKey(Base):
    def setUp(self):
        super().setUp()
        self.key = self.root / "keys" / "pack_deploy_key"
        patcher = mock.patch.dict(os.environ, {"NOC_PACK_SSH_KEY": str(self.key)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_key_means_the_normal_git_login_is_used(self):
        self.assertNotIn("GIT_SSH_COMMAND", pack._env())

    def test_creates_a_private_key_pins_host_keys_and_prints_the_public_half(self):
        with mock.patch.object(pack, "_github_host_keys", return_value=["github.com ssh-ed25519 AAAAtest"]):
            code, out, err = self.run_pack("deploy-key")
        self.assertEqual(code, 0, err)
        self.assertEqual(oct(self.key.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.key.parent.stat().st_mode & 0o777), "0o700")
        public = Path(str(self.key) + ".pub").read_text().strip()
        self.assertIn(public, out)
        self.assertNotIn("PRIVATE KEY", out + err)
        self.assertEqual(pack.known_hosts_path().read_text().strip(), "github.com ssh-ed25519 AAAAtest")
        ssh = pack._env()["GIT_SSH_COMMAND"]
        self.assertIn("-o IdentitiesOnly=yes", ssh)
        self.assertIn("StrictHostKeyChecking=yes", ssh)
        self.assertIn(str(self.key), ssh)

    def test_a_second_run_keeps_the_key(self):
        with mock.patch.object(pack, "_github_host_keys", return_value=[]):
            self.assertEqual(self.run_pack("deploy-key")[0], 0)
            before = self.key.read_text()
            code, out, err = self.run_pack("deploy-key")
        self.assertEqual(code, 0)
        self.assertEqual(self.key.read_text(), before)
        self.assertIn("existing key", out)
        self.assertIn("could not fetch GitHub's host keys", err)
        self.assertIn("StrictHostKeyChecking=accept-new", pack._env()["GIT_SSH_COMMAND"])

    def test_git_commands_run_with_the_deploy_key(self):
        with mock.patch.object(pack, "_github_host_keys", return_value=["github.com ssh-ed25519 AAAAtest"]):
            self.run_pack("deploy-key")
        self.seed_lead()
        bin_dir, log = self.root / "bin", self.root / "ssh.log"
        bin_dir.mkdir()
        fake = bin_dir / "ssh"
        fake.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit 1\n' % log)
        fake.chmod(0o755)
        with self.at(self.lead), mock.patch.dict(os.environ, {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"])}):
            self.assertEqual(self.run_pack("init", "git@github.com:example-org/example-pack.git")[0], 0)
            result = pack.git("ls-remote", "origin")              # git calls ssh, which here is the recording stub
        self.assertNotEqual(result.returncode, 0)
        called = log.read_text()
        self.assertIn("-i %s" % self.key, called)
        self.assertIn("IdentitiesOnly=yes", called)
        self.assertIn("UserKnownHostsFile=%s" % pack.known_hosts_path(), called)

    def test_unattended_runs_create_nothing(self):
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            code, _o, err = self.run_pack("deploy-key")
        self.assertEqual(code, 4)
        self.assertFalse(self.key.exists())


class Misc(Base):
    def test_git_paths_are_relative_to_the_pack(self):
        self.publish()
        (self.lead / "devices.md").write_text("| changed |\n")
        with self.at(self.lead):
            self.assertEqual(pack.main(["git", "add", "devices.md"]), 0)
            self.assertIn("devices.md", pack.git("diff", "--cached", "--name-only").stdout)

    def test_status_without_a_pack(self):
        self.mate.mkdir(parents=True)
        with self.at(self.mate):
            self.assertEqual(self.run_pack("status")[0], 3)
            self.assertEqual(self.run_pack("pull")[0], 3)

    def test_status_reports_counts(self):
        self.publish()
        with self.at(self.lead):
            code, out, _e = self.run_pack("status", "--fetch")
        self.assertEqual(code, 0)
        self.assertIn("ahead:   0", out)
        self.assertIn("behind:  0", out)

    def test_unattended_runs_change_nothing(self):
        self.seed_lead()
        with self.at(self.lead), mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            for argv in (("init", str(self.remote)), ("join", str(self.remote), "--yes"), ("pull", "--yes"), ("git", "status")):
                code, _o, err = self.run_pack(*argv)
                self.assertEqual(code, 4, argv)
                self.assertIn("refused", err)
        self.assertFalse((self.lead / ".pack.git").exists())


if __name__ == "__main__":
    unittest.main()
