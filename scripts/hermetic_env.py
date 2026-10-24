"""Import this from any test that runs git: it removes repository-local GIT_* variables.

When git runs a hook (pre-push, pre-commit) it exports GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE and similar.
A test that builds a throwaway repository would then silently operate on the real one (this once left a
"seed" commit and a staged fixture in a developer's branch). Importing this module drops them for the
test process and everything it spawns. Settings that only point git at config files are kept.
"""
import os

KEEP = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"}


def scrub_git_env(env=None):
    env = os.environ if env is None else env
    for key in [k for k in env if k.startswith("GIT_") and k not in KEEP]:
        del env[key]
    return env


scrub_git_env()
