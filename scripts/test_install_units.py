"""scripts/install.sh --dry-run renders the units and the optional read-only drop-in (nothing is installed)."""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
NEEDS = unittest.skipUnless(os.geteuid() == 0 and shutil.which("bash") and shutil.which("node"),
                            "install.sh --dry-run needs root (it re-runs under sudo otherwise), bash and node")


def dry_run(*flags):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOC_", "AGENT_"))}
    return subprocess.run(["bash", str(REPO / "scripts" / "install.sh"), "--dry-run", *flags],
                          capture_output=True, text=True, env=env, timeout=120)


@NEEDS
class RenderedUnits(unittest.TestCase):
    def test_every_unit_renders_with_no_placeholder_left(self):
        r = dry_run()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        for name in ("noc-agent.service", "noc-treeguard.service", "noc-treeguard.timer", "noc-metrics.timer"):
            self.assertIn("----- %s -----" % name, r.stdout)
        self.assertNotIn("{{", r.stdout)
        self.assertIn("tree_guard.py check --alert --quiet", r.stdout)
        self.assertIn("SuccessExitStatus=1", r.stdout)
        self.assertNotIn("10-protect-tree.conf", r.stdout)

    def test_protect_tree_renders_the_read_only_drop_in_for_this_checkout(self):
        r = dry_run("--protect-tree")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        drop = r.stdout.split("noc-agent.service.d/10-protect-tree.conf -----", 1)[1]
        self.assertIn("ReadOnlyPaths=%s\n" % REPO, drop)
        self.assertIn("ReadWritePaths=%s/logs\n" % REPO, drop)
        for kept in ("credentials.enc", "server.json"):
            self.assertIn("ReadOnlyPaths=-%s/mcp-server/config/%s" % (REPO, kept), drop)
        self.assertNotIn("ReadWritePaths=%s/scripts" % REPO, drop)
        self.assertNotIn("{{", drop)

    def test_protect_and_unprotect_together_are_refused(self):
        r = dry_run("--protect-tree", "--unprotect-tree")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot be combined", r.stderr + r.stdout)

    def test_the_drop_in_template_only_opens_what_the_system_writes(self):
        text = (REPO / "deploy" / "noc-agent.protect-tree.conf.template").read_text()
        writable = [ln.split("=", 1)[1].lstrip("-").replace("{{INSTALL_DIR}}", "") for ln in text.splitlines()
                    if ln.startswith("ReadWritePaths=")]
        self.assertEqual(writable, ["/logs", "/mcp-server/logs", "/mcp-server/state", "/mcp-server/config"])


if __name__ == "__main__":
    unittest.main()
