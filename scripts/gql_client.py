#!/usr/bin/env python3
"""
gql_client.py - minimal Sonar GraphQL client for the ops scripts.

Replaces the per-script `curl` subprocesses:

* urllib (stdlib), timeout=30: the bearer token never appears in argv / `ps`.
* GraphQL VARIABLES only. Ticket ids, comment bodies, evidence and category
  ids travel as JSON variables; query text is a fixed string. Nothing a ticket
  author controls can alter the query (no f-string GraphQL).
* Redirects are NOT followed (urllib would re-send the Authorization header to
  the redirect target), and plain http:// is refused for non-loopback hosts so
  the token cannot go out in clear text.
* Every failure shape (transport, HTTP status, non-JSON, `errors`, and
  `"data": null` / missing keys) becomes ONE exception type, GraphQLError, with
  a short message that never contains the token.

Variable types: Sonar ids are the custom scalar `Int64Bit` on this API (see
.opencode/skills/sonar-api/SKILL.md: "the ID variable's GraphQL type is
`Int64Bit!` on this tenant"). If a tenant differs, set SONAR_ID_SCALAR (a bare
GraphQL type name, e.g. `Int`).

Env: SONAR_TOKEN (env or .env), SONAR_BASE_URL (else site-pack profile
org.sonar_base_url), SONAR_ID_SCALAR, SONAR_ALLOW_INSECURE_HTTP=1 (tests only).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import noc_config  # noqa: E402

_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
_TICKET_ID_RE = re.compile(r"^[0-9]{1,18}$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
USER_AGENT = "NocAgent/1.0"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GraphQLError(Exception):
    """kind: config | transport | http | json | graphql | shape"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def id_scalar() -> str:
    v = os.environ.get("SONAR_ID_SCALAR", "").strip() or "Int64Bit"
    if not _SCALAR_RE.match(v):
        raise GraphQLError("config", f"SONAR_ID_SCALAR={v!r} is not a GraphQL type name")
    return v


def parse_ticket_id(raw: Any) -> int:
    """Strict positive integer ticket id (digits only). ValueError otherwise."""
    s = str(raw).strip()
    if not _TICKET_ID_RE.match(s) or int(s) <= 0:
        raise ValueError(f"ticket id must be a positive integer, got {raw!r}")
    return int(s)


def get_token() -> str:
    return envfile.get("SONAR_TOKEN")


def graphql_url(profile: Optional[dict] = None) -> str:
    return noc_config.sonar_base_url(profile) + "/api/graphql"


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname:
        if parts.hostname in _LOOPBACK or os.environ.get("SONAR_ALLOW_INSECURE_HTTP") == "1":
            return
        raise GraphQLError("config", "refusing to send the Sonar token over plain http:// "
                                     f"(host {parts.hostname}); use https or a loopback test server")
    raise GraphQLError("config", f"unsupported Sonar URL (need https://host): {url[:60]!r}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _opener(url: str) -> urllib.request.OpenerDirector:
    handlers = [_NoRedirect()]
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in _LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))  # never proxy a local test server
    return urllib.request.build_opener(*handlers)


def request(query: str, variables: Optional[Dict[str, Any]] = None, *,
            token: Optional[str] = None, url: Optional[str] = None,
            timeout: float = 30.0) -> Dict[str, Any]:
    """POST one operation; return the parsed JSON response object (any shape of
    `data`/`errors`). Raises GraphQLError for transport/HTTP/JSON problems."""
    tok = token if token is not None else get_token()
    if not tok:
        raise GraphQLError("config", "SONAR_TOKEN not set (environment or .env)")
    target = url or graphql_url()
    _check_url(target)
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(target, data=body, method="POST", headers={
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with _opener(target).open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        if 300 <= e.code < 400:
            raise GraphQLError("http", f"Sonar answered HTTP {e.code} redirect (not followed)")
        raise GraphQLError("http", f"Sonar answered HTTP {e.code}: {snippet[:200]}")
    except urllib.error.URLError as e:
        raise GraphQLError("transport", f"cannot reach Sonar: {getattr(e, 'reason', e)}")
    except (OSError, ValueError) as e:  # timeouts, resets, bad URL
        raise GraphQLError("transport", f"cannot reach Sonar: {e}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GraphQLError("json", "Sonar response too large")
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise GraphQLError("json", f"non-JSON Sonar response: {raw[:200].decode('utf-8', 'replace')!r}")
    if not isinstance(parsed, dict):
        raise GraphQLError("json", "Sonar response is not a JSON object")
    return parsed


def execute(query: str, variables: Optional[Dict[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
    """Run an operation and return its `data` object.

    Raises GraphQLError("graphql") on a non-empty `errors` array and
    GraphQLError("shape") when `data` is null/missing (the case that used to
    surface as `AttributeError: 'NoneType' object has no attribute 'get'`)."""
    resp = request(query, variables, **kw)
    errors = resp.get("errors")
    if errors:
        raise GraphQLError("graphql", json.dumps(errors)[:500])
    data = resp.get("data")
    if not isinstance(data, dict):
        raise GraphQLError("shape", f"Sonar response has no data object: {json.dumps(resp)[:300]}")
    return data


def dig(data: Any, *path: Any) -> Any:
    """data[path[0]][path[1]]... ; GraphQLError("shape") if any hop is missing/null."""
    node = data
    walked = []
    for key in path:
        walked.append(str(key))
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            raise GraphQLError("shape", f"response is missing {'.'.join(walked)}")
        if node is None:
            raise GraphQLError("shape", f"response has null at {'.'.join(walked)}")
    return node


# --- shared operations -------------------------------------------------------

def recent_comments_query() -> str:
    sc = id_scalar()
    return (
        f"query RecentTicketComments($ticketId: {sc}!) {{ "
        "ticket_comments(search: { integer_fields: [{ attribute: \"ticket_id\", operator: EQ, "
        "search_value: $ticketId }] }, sorter: { attribute: \"created_at\", direction: DESC }, "
        "paginator: { page: 1, records_per_page: 5 }) { entities { id body } } }"
    )


def fetch_recent_comments(ticket_id: int, **kw: Any) -> list:
    """Last 5 comments (id, body), newest first. GraphQLError on any bad shape."""
    data = execute(recent_comments_query(), {"ticketId": ticket_id}, **kw)
    entities = dig(data, "ticket_comments", "entities")
    if not isinstance(entities, list):
        raise GraphQLError("shape", "ticket_comments.entities is not a list")
    return entities
