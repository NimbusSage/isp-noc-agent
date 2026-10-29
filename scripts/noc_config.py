#!/usr/bin/env python3
"""
noc_config.py - shared config resolution for the ops scripts.

* site-pack/profile.json loader (org-specific Sonar group/category ids, base URL), with the machine-local
  profile.local.json overlay merged over it (secret values; never shared through the site pack)
* state directory resolution (breaker state, decision log, metrics, eval output)
* strict integer-id resolution: env (real environment, then .env) beats the
  profile; there are NO built-in fallback ids - a missing id is an error the
  caller must surface, never a silent default that hands one org's ids to another.

Env:
    NOC_PROFILE_FILE    override the profile path (default <repo>/site-pack/profile.json)
    NOC_STATE_DIR       state root (default: NOC_AGENT_LOG_DIR, else <repo>/logs)
    NOC_AGENT_LOG_DIR   legacy name for the same thing; the webhook server sets it
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """A required setting is missing or malformed. Callers map this to their config exit code."""


def overlay_path(path: Path) -> Path:
    """The machine-local overlay beside a config file: profile.json -> profile.local.json."""
    return path.with_name(path.stem + ".local" + path.suffix)


def merge_overlay(base: Any, overlay: Any) -> Any:
    """Overlay on top of base: objects merge key by key, anything else (lists included) is replaced whole."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = merge_overlay(base[key], value) if key in base else value
        return out
    return overlay


def load_json_overlay(path: Path) -> Any:
    """JSON from `path` with its `.local` overlay merged over it. The base file must exist (FileNotFoundError, like
    open()); a missing overlay is normal; invalid JSON in either raises ValueError naming the file.

    Secret values (SNMP communities, router passwords) live in the overlay, which the shared site pack never
    tracks (scripts/pack.py localize moves them there). Only readers that need those values merge it in."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError("%s: %s" % (path, e)) from e
    local = overlay_path(path)
    try:
        text = local.read_text(encoding="utf-8")
    except FileNotFoundError:
        return data
    try:
        return merge_overlay(data, json.loads(text))
    except ValueError as e:
        raise ValueError("%s: %s" % (local, e)) from e


def profile_path() -> Path:
    override = os.environ.get("NOC_PROFILE_FILE", "").strip()
    return Path(override) if override else ROOT / "site-pack" / "profile.json"


def load_profile() -> dict:
    """site-pack/profile.json as a dict; {} when missing, unreadable or not an object."""
    try:
        data = load_json_overlay(profile_path())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def profile_get(profile: Optional[dict], keys: Iterable[str], default: Any = None) -> Any:
    node: Any = profile if profile is not None else load_profile()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def state_dir() -> Path:
    """Directory for mutable runtime state. Created lazily by the writers."""
    for name in ("NOC_STATE_DIR", "NOC_AGENT_LOG_DIR"):
        v = os.environ.get(name, "").strip()
        if v:
            return Path(v)
    return ROOT / "logs"


def sonar_base_url(profile: Optional[dict] = None) -> str:
    """env SONAR_BASE_URL (env or .env), else profile org.sonar_base_url, else a neutral
    example host (auth fails loudly rather than ever reaching some other org)."""
    env_url = envfile.get("SONAR_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    base = profile_get(profile, ("org", "sonar_base_url"), "")
    if isinstance(base, str) and base.strip():
        return base.strip().rstrip("/")
    return "https://your-org.sonar.software"


def _as_id(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        v = int(raw.strip())
        return v if v > 0 else None
    return None


def resolve_id(env_names: Iterable[str], profile_keys: Iterable[str],
               profile: Optional[dict] = None) -> Optional[int]:
    """Resolve a Sonar group/category id.

    Order: each env name in turn (real environment, then .env), then the profile
    at `profile_keys` (<...>.id is appended when the node is an object). Returns
    None when nothing is configured. Raises ConfigError when something IS
    configured but is not a positive integer - a typo must never fall through
    to a different source.
    """
    for name in env_names:
        raw = envfile.get(name)
        if raw:
            v = _as_id(raw)
            if v is None:
                raise ConfigError(f"{name}={raw!r} is not a positive integer id")
            return v
    node = profile_get(profile, profile_keys, None)
    if isinstance(node, dict):
        node = node.get("id")
    if node is None:
        return None
    v = _as_id(node)
    if v is None:
        raise ConfigError(f"site-pack profile {'.'.join(profile_keys)} = {node!r} is not a positive integer id")
    return v
