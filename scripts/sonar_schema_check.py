#!/usr/bin/env python3
"""Read-only check that the GraphQL names our scripts rely on exist on the configured Sonar.

sonar_comment.py, sonar_bounce.py and gql_client.py send typed variables
(`$id: Int64Bit!`, `$input: CreateTicketCommentMutationInput!`,
`$input: UpdateTicketMutationInput!`). A tenant whose schema names differ would
reject every call. This asks the server's introspection (read-only, no ticket data)
whether those names exist and have the shape the scripts expect.

  python3 scripts/sonar_schema_check.py          # uses SONAR_BASE_URL/profile + SONAR_TOKEN like the other scripts
  SONAR_ID_SCALAR=Int python3 scripts/sonar_schema_check.py   # test an alternative id scalar

Exit 0 = every name found, 1 = something missing or mismatched, 2 = could not ask
(no token, network error, introspection disabled). Prints names only, never the token.
"""
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gql_client  # noqa: E402

TYPE_Q = "query($n: String!) { __type(name: $n) { name kind } }"
FIELDS_Q = (
    "query($n: String!) { __type(name: $n) { fields { name args { name type { name kind "
    "ofType { name kind ofType { name kind ofType { name kind } } } } } } } }"
)


def _unwrap(t: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(name, kind) of the innermost named type, skipping NON_NULL / LIST wrappers."""
    while isinstance(t, dict) and t.get("name") is None and isinstance(t.get("ofType"), dict):
        t = t["ofType"]
    if not isinstance(t, dict):
        return None, None
    return t.get("name"), t.get("kind")


def _type(name: str) -> Optional[Dict[str, Any]]:
    data = gql_client.execute(TYPE_Q, {"n": name})
    t = data.get("__type") if isinstance(data, dict) else None  # null = type does not exist
    return t if isinstance(t, dict) else None


def _fields(type_name: str) -> Dict[str, Dict[str, Any]]:
    data = gql_client.execute(FIELDS_Q, {"n": type_name})
    tp = data.get("__type") if isinstance(data, dict) else None
    fields = tp.get("fields") if isinstance(tp, dict) else None
    return {f["name"]: f for f in fields if isinstance(f, dict) and "name" in f} if isinstance(fields, list) else {}


def check() -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []

    def add(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, ok, detail))

    scalar = gql_client.id_scalar()
    t = _type(scalar)
    add(f"id scalar {scalar}", bool(t and t.get("kind") == "SCALAR"),
        "" if t else "type not found (set SONAR_ID_SCALAR to the id type your tenant uses)")
    for name in ("CreateTicketCommentMutationInput", "UpdateTicketMutationInput"):
        t = _type(name)
        add(f"input type {name}", bool(t and t.get("kind") == "INPUT_OBJECT"), "" if t else "type not found")

    mutation = _fields("Mutation")
    query = _fields("Query")
    for fname in ("createTicketComment", "updateTicket"):
        add(f"Mutation.{fname}", fname in mutation, "" if fname in mutation else "field not found")
    for fname in ("tickets", "ticket_comments"):
        add(f"Query.{fname}", fname in query, "" if fname in query else "field not found")

    upd = mutation.get("updateTicket")
    if upd:
        args = {a["name"]: _unwrap(a.get("type")) for a in upd.get("args", []) if isinstance(a, dict)}
        got = args.get("id", (None, None))[0]
        add("updateTicket(id:) type", got == scalar, f"schema says {got}, scripts send {scalar}")
        got = args.get("input", (None, None))[0]
        add("updateTicket(input:) type", got == "UpdateTicketMutationInput", f"schema says {got}")
    ctc = mutation.get("createTicketComment")
    if ctc:
        args = {a["name"]: _unwrap(a.get("type")) for a in ctc.get("args", []) if isinstance(a, dict)}
        got = args.get("input", (None, None))[0]
        add("createTicketComment(input:) type", got == "CreateTicketCommentMutationInput", f"schema says {got}")
    return results


def main() -> int:
    try:
        results = check()
    except gql_client.GraphQLError as e:
        print(f"sonar_schema_check: could not ask the server ({e.kind}): {e.message[:200]}", file=sys.stderr)
        return 2
    bad = 0
    for label, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f" - {detail}" if detail and not ok else ""))
        bad += 0 if ok else 1
    print("schema check:", "all names match" if not bad else f"{bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
