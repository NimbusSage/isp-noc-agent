#!/usr/bin/env python3
"""hub_tls.py - trusted HTTPS for the central hub, issued over ACME DNS-01.

The hub never has to accept traffic from the internet to get a certificate. The
challenge is answered by writing one `_acme-challenge.<host>` TXT record into the
organization's own DNS server with RFC 2136 dynamic updates (a TSIG key) or with
Technitium's HTTP API, and the CA reads it from there. So the host name can
resolve to the hub's private address and stay unreachable from outside.

    python3 scripts/hub_tls.py --check                     # what is missing, and the cert that exists
    python3 scripts/hub_tls.py --issue --staging           # first run: staging endpoint, so no rate limit
    python3 scripts/hub_tls.py --issue                     # real certificate
    python3 scripts/hub_tls.py --write-caddy               # render the Caddy site block (tls + reverse_proxy)
    sudo bash scripts/install.sh --with-hub-tls            # daily renewal timer

Settings come from the environment, then `.env` (see .env.example, section "Hub"):
`HUB_TLS_HOSTNAME`, `HUB_TLS_EMAIL`, `HUB_TLS_DNS_PROVIDER` (`dnsupdate` or
`technitium`), `HUB_TLS_STAGING`, `HUB_TLS_STATE_DIR`, `HUB_TLS_UPSTREAM`, plus the
backend's own names. Values are only ever handed to `lego` through the environment:
they are never printed, never put on a command line (`ps`, shell history) and never
written anywhere except by `lego` itself.

`lego` (https://go-acme.github.io/lego/) does the ACME work; this script only decides
what to ask for, checks the inputs, and renders the Caddy config. Install it once from
your package manager or a release binary, then renew with `lego renew` on a timer.

Exit codes: 0 ok, 2 usage / refused, 3 missing setting or dependency, 4 lego failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import envfile  # noqa: E402  (sibling module; path inserted above)

PROVIDERS = {
    # RFC 2136 dynamic updates. Preferred: a TSIG key can be scoped to the
    # challenge records, while an API token usually can rewrite the whole zone.
    "dnsupdate": ("DNSUPDATE_NAMESERVER", "DNSUPDATE_TSIG_KEY", "DNSUPDATE_TSIG_SECRET", "DNSUPDATE_TSIG_ALGORITHM"),
    "technitium": ("TECHNITIUM_SERVER_BASE_URL", "TECHNITIUM_API_TOKEN"),
}
PROVIDER_VARS = tuple(sorted({name for names in PROVIDERS.values() for name in names}))
HUB_VARS = ("HUB_TLS_HOSTNAME", "HUB_TLS_EMAIL", "HUB_TLS_DNS_PROVIDER", "HUB_TLS_STAGING",
            "HUB_TLS_STATE_DIR", "HUB_TLS_UPSTREAM")
TSIG_ALGORITHMS = {"hmac-sha512", "hmac-sha256", "hmac-sha1", "hmac-md5"}
STAGING_DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"
DEFAULT_STATE_DIR = "/etc/nocagent/lego"
DEFAULT_CADDY_CONFIG = "/etc/nocagent/caddy/Caddyfile"
DEFAULT_UPSTREAM = "127.0.0.1:8080"
DEFAULT_RELOAD = "systemctl reload caddy"
HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class HubTlsError(Exception):
    """A refusal or invalid input. The message never contains a secret value."""


# ----------------------------------------------------------------------------- settings

def _read(name: str, env: dict | None, default: str = "") -> str:
    """Environment-of-record value for `name`: a passed dict, else envfile (real env, then .env)."""
    if env is not None:
        return str(env.get(name) or "").strip() or default
    return envfile.get(name, default)


def settings(env: dict | None = None, *, lego: str = "lego", caddy_config: str = "",
             reload_cmd: str = "", server: str = "") -> dict:
    provider = (_read("HUB_TLS_DNS_PROVIDER", env, "dnsupdate") or "dnsupdate").lower()
    if provider not in PROVIDERS:
        raise HubTlsError(f"HUB_TLS_DNS_PROVIDER={provider!r} is not one of: {', '.join(sorted(PROVIDERS))}")
    staging = _read("HUB_TLS_STAGING", env, "false").lower() in ("1", "true", "yes", "on")
    variables = {name: _read(name, env) for name in PROVIDER_VARS}   # all, so docs_check sees them read
    host = _read("HUB_TLS_HOSTNAME", env)
    return {
        "hostname": validate_hostname(host) if host else "",
        "email": _read("HUB_TLS_EMAIL", env),
        "provider": provider,
        "staging": staging,
        "server": server or (STAGING_DIRECTORY if staging else ""),
        "state_dir": _read("HUB_TLS_STATE_DIR", env, DEFAULT_STATE_DIR),
        "upstream": _read("HUB_TLS_UPSTREAM", env, DEFAULT_UPSTREAM),
        "lego": lego,
        "caddy_config": caddy_config or DEFAULT_CADDY_CONFIG,
        "reload_cmd": reload_cmd or DEFAULT_RELOAD,
        "vars": variables,
    }


def validate_hostname(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    if not host:
        raise HubTlsError("HUB_TLS_HOSTNAME is empty; set it to the hub's public name (for example hub.example.com)")
    if "*" in host or "_" in host:
        raise HubTlsError(f"HUB_TLS_HOSTNAME={host!r} is not a host name: no wildcard or underscore, one exact name")
    if HOSTNAME_RE.match(host):
        return host
    raise HubTlsError(f"HUB_TLS_HOSTNAME={host!r} is not a host name: use lower-case labels and a real domain, "
                      "not an IP address")


def normalize_tsig_algorithm(value: str) -> str:
    """Technitium writes `HMAC-SHA256`, lego wants `hmac-sha256.`; both are accepted here."""
    alg = (value or "").strip().lower().rstrip(".")
    if not alg:
        return "hmac-sha256."
    if alg not in TSIG_ALGORITHMS:
        raise HubTlsError(f"DNSUPDATE_TSIG_ALGORITHM={value!r} is not supported; "
                          f"use one of: {', '.join(sorted(TSIG_ALGORITHMS))}")
    return alg + "."


# ------------------------------------------------------------------------- provider plumbing

def provider_env(s: dict, base_env: dict | None = None) -> dict:
    """Environment for lego: the caller's environment plus this backend's variables."""
    env = dict(os.environ if base_env is None else base_env)
    for name in PROVIDERS[s["provider"]]:
        value = s["vars"].get(name, "")
        if name.endswith("_ALGORITHM"):
            # Always send one: lego's built-in default is HMAC-MD5.
            env[name] = normalize_tsig_algorithm(value)
        elif value:
            env[name] = value
    return env


def missing_settings(s: dict) -> list:
    """Names still needed, in the order an operator should fill them."""
    problems = []
    if not s["hostname"]:
        problems.append("HUB_TLS_HOSTNAME")
    if not s["email"]:
        problems.append("HUB_TLS_EMAIL")
    # The TSIG algorithm has a documented default (HMAC-SHA256), so it is optional.
    problems += [name for name in PROVIDERS[s["provider"]]
                 if not name.endswith("_ALGORITHM") and not s["vars"].get(name)]
    return problems


def lego_argv(s: dict, action: str) -> list:
    """The command line for lego. No setting value is ever part of it."""
    argv = [s["lego"], "--accept-tos", "--path", s["state_dir"], "--dns", s["provider"]]
    if s["email"]:
        argv += ["--email", s["email"]]
    if s["server"]:
        argv += ["--server", s["server"]]
    argv += ["-d", s["hostname"]]
    if action == "renew":
        return argv + ["renew", "--days", "30", "--renew-hook", s["reload_cmd"]]
    return argv + ["run"]


def lego_path(s: dict) -> str:
    found = shutil.which(s["lego"])
    if found:
        return found
    if Path(s["lego"]).is_file():
        return s["lego"]
    raise HubTlsError(f"lego not found ({s['lego']}). Install it from https://go-acme.github.io/lego/ and "
                      "put it on PATH, or pass --lego /path/to/lego")


def lego_supports(s: dict, provider: str) -> bool:
    """True when this lego build knows the provider (`lego dnshelp` lists them by code)."""
    try:
        out = subprocess.run([lego_path(s), "dnshelp"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return provider.lower() in out.stdout.lower()


# ------------------------------------------------------------------------------- certificate

def cert_paths(s: dict) -> tuple:
    # lego writes <path>/certificates/<domain>.crt and .key; append, never replace,
    # the suffix or hub.example.com becomes hub.example.crt.
    base = str(Path(s["state_dir"]) / "certificates" / s["hostname"])
    return Path(base + ".crt"), Path(base + ".key")


def cert_info(s: dict) -> dict:
    """Subject and end date of the certificate already on disk (openssl), or an empty dict."""
    crt, key = cert_paths(s)
    if not crt.is_file() or not key.is_file():
        return {}
    openssl = shutil.which("openssl")
    if not openssl:
        return {"path": str(crt)}
    try:
        out = subprocess.run([openssl, "x509", "-noout", "-enddate", "-subject", "-in", str(crt)],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"path": str(crt)}
    info = {"path": str(crt)}
    for line in out.stdout.splitlines():
        if line.startswith("notAfter="):
            info["not_after"] = line.split("=", 1)[1].strip()
        elif line.startswith("subject="):
            info["subject"] = line.split("=", 1)[1].strip()
    return info


def caddy_site(s: dict) -> str:
    """The site block for the hub. Caddy loads the files lego renews; it does not do ACME here."""
    crt, key = cert_paths(s)
    return "\n".join([
        "# Managed by scripts/hub_tls.py --write-caddy; edits are overwritten.",
        f"{s['hostname']} {{",
        f"\ttls {crt} {key}",
        "\tencode zstd gzip",
        "",
        "\t# Everything the hub serves sits behind this one host name. Keep it LAN/VPN-reachable:",
        "\t# the certificate needs no open port, so there is no reason to forward 80 or 443.",
        f"\treverse_proxy {s['upstream']}",
        "}",
        "",
    ])


def relax_for_caddy(crt: Path, key: Path) -> str:
    """Let the caddy service read the key without making it world-readable."""
    try:
        import grp
        gid = grp.getgrnam("caddy").gr_gid
    except (KeyError, ImportError):
        return "no 'caddy' group: the key stays owner-only; give the proxy access yourself if it runs as another user"
    if os.geteuid() != 0:
        return "not root: the key stays owner-only (rerun with sudo to hand it to the caddy group)"
    try:
        os.chown(key, -1, gid)
        os.chmod(key, 0o640)
        os.chmod(crt, 0o644)
    except OSError as exc:
        return f"could not adjust file modes: {exc.strerror or exc}"
    return "key 0640 root:caddy, certificate 0644 (readable by the proxy, not by other users)"


def write_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------------- checks

def check(s: dict) -> tuple:
    """(problems, warnings) for a run. Reads nothing secret, contacts nothing."""
    problems, warnings = [], []
    for name in missing_settings(s):
        problems.append(f"{name} is not set")
    if s["provider"] == "technitium":
        warnings.append("the technitium backend uses an HTTP API token, which can usually rewrite the whole zone; "
                        "HUB_TLS_DNS_PROVIDER=dnsupdate with a key scoped to _acme-challenge is the safer choice")
    try:
        binary = lego_path(s)
    except HubTlsError as exc:
        problems.append(str(exc))
    else:
        if not lego_supports(s, s["provider"]):
            problems.append(f"{binary} does not support --dns {s['provider']}; upgrade lego (dnsupdate since v0.3, "
                            "technitium since v4.20)")
    if s["state_dir"]:
        state = Path(s["state_dir"])
        if state.exists() and not os.access(state, os.W_OK) and not os.geteuid() == 0:
            problems.append(f"{state} is not writable by this user; run it with sudo or change HUB_TLS_STATE_DIR")
        elif not state.exists() and not os.access(state.parent if state.parent.exists() else Path("/"), os.W_OK):
            problems.append(f"{state} does not exist and cannot be created; run it with sudo")
    if s["staging"]:
        warnings.append("staging endpoint: the certificate it issues is not trusted by browsers")
    if not s["server"]:
        warnings.append("production endpoint: a failed first attempt spends a real rate-limit budget; "
                        "use --staging once first")
    return problems, warnings


def run_lego(s: dict, action: str, dry_run: bool) -> int:
    argv = lego_argv(s, action)
    env = provider_env(s)
    if dry_run:
        print("would run: " + " ".join(argv))
        return 0
    binary = lego_path(s)
    argv[0] = binary
    try:
        return subprocess.run(argv, env=env).returncode
    except OSError as exc:
        raise HubTlsError(f"could not run {binary}: {exc.strerror or exc}") from exc


# -------------------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub_tls.py", description="Trusted HTTPS for the central hub over ACME DNS-01. "
                                "No inbound port is needed: the challenge is one TXT record in your own DNS server.")
    p.add_argument("--check", action="store_true", help="verify settings, lego and the existing certificate (default)")
    p.add_argument("--issue", action="store_true", help="obtain the certificate (lego run)")
    p.add_argument("--renew", action="store_true", help="renew if due and reload Caddy (lego renew, for the timer)")
    p.add_argument("--write-caddy", action="store_true", help="write the Caddy site block for this host name")
    p.add_argument("--staging", action="store_true", help="use the staging CA for this run")
    p.add_argument("--dry-run", action="store_true", help="print the command and paths, change nothing")
    p.add_argument("--json", action="store_true", help="machine-readable output for --check")
    p.add_argument("--caddy-config", metavar="PATH", help=f"Caddy config to render (default: {DEFAULT_CADDY_CONFIG})")
    p.add_argument("--lego", metavar="PATH", help="lego binary (default: lego on PATH)")
    p.add_argument("--server", metavar="URL", help="ACME directory URL (overrides --staging)")
    p.add_argument("--reload-cmd", metavar="CMD", help=f"command run after a renewal (default: {DEFAULT_RELOAD})")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = settings(lego=args.lego or "lego", caddy_config=args.caddy_config or "",
                     reload_cmd=args.reload_cmd or "", server=args.server or "")
        if args.staging:
            s["staging"], s["server"] = True, STAGING_DIRECTORY
    except HubTlsError as exc:
        print(f"hub_tls: {exc}", file=sys.stderr)
        return 2

    try:
        if args.issue or args.renew:
            problems, _ = check(s)
            if problems:
                for p in problems:
                    print(f"hub_tls: {p}", file=sys.stderr)
                print("hub_tls: nothing issued", file=sys.stderr)
                return 3
            code = run_lego(s, "renew" if args.renew else "run", args.dry_run)
            if code != 0:
                print(f"hub_tls: lego exited {code}; the existing certificate (if any) is untouched", file=sys.stderr)
                return 4
            crt, key = cert_paths(s)
            print("certificate: " + str(crt))
            if not args.dry_run:
                print("permissions: " + relax_for_caddy(crt, key))
            return 0

        if args.write_caddy:
            if not s["hostname"]:
                print("hub_tls: HUB_TLS_HOSTNAME is not set; nothing written", file=sys.stderr)
                return 3
            text = caddy_site(s)
            if args.dry_run:
                print(text, end="")
                return 0
            write_text(Path(s["caddy_config"]), text, 0o644)
            print(f"wrote {s['caddy_config']} (reload Caddy to apply)")
            return 0

        problems, warnings = check(s)
        info = cert_info(s)
        if args.json:
            print(json.dumps({"ok": not problems, "hostname": s["hostname"], "provider": s["provider"],
                              "staging": s["staging"], "certificate": info,
                              "problems": problems, "warnings": warnings}, indent=2))
        else:
            print(f"host:       {s['hostname'] or '(unset)'}")
            print(f"provider:   {s['provider']}"
                  + ("  (staging)" if s["staging"] else "")
                  + f"   state: {s['state_dir']}")
            crt, _ = cert_paths(s)
            if info.get("not_after"):
                print(f"certificate: {crt}  expires {info['not_after']}")
            elif info:
                print(f"certificate: {crt}  (expiry unreadable; is openssl installed?)")
            else:
                print(f"certificate: none at {crt}")
            for w in warnings:
                print(f"note: {w}")
            for p in problems:
                print(f"missing: {p}")
            if not problems:
                print("ok: ready to --issue (add --staging for the first attempt of a new host name)")
        return 3 if problems else 0
    except HubTlsError as exc:
        print(f"hub_tls: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
