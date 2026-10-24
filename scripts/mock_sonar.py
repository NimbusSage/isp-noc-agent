#!/usr/bin/env python3
"""
mock_sonar.py - in-memory Sonar GraphQL mock for NocAgent demo mode.

Serves just enough of Sonar's GraphQL API for the shapes NocAgent actually sends
(scripts/sonar_comment.py, scripts/sonar_bounce.py, scripts/metrics_rollup.py,
scripts/doctor.py and the sonar-api skill), against fictional fixture tickets.
Binds 127.0.0.1 only. Python 3.9+ standard library only. Nothing is persisted.

Run:
    python3 scripts/mock_sonar.py --port 0 [--fixtures DIR] [--token VALUE ...] [--verbose]

On start it prints one JSON line on stdout and then serves until SIGINT/SIGTERM:
    {"event": "listening", "host": "127.0.0.1", "port": 41234,
     "url": "http://127.0.0.1:41234", "token": "demo-..."}
(`token` appears only when no --token was given and one was generated.)

Point the real scripts at it with:
    SONAR_BASE_URL=http://127.0.0.1:<port>  SONAR_TOKEN=<token>

Endpoints
    POST /api/graphql  (also /graphql)   Bearer-authenticated GraphQL
    GET  /health                         unauthenticated liveness
    GET  /__audit                        Bearer; every mutation (JSON) + request counters
    POST /__reset                        Bearer; reload fixtures, clear audit and chat sink
    GET  /__chat                         Bearer; payloads captured by the chat sink
    POST /chat/webhook                   unauthenticated Google-Chat-style incoming webhook sink

GraphQL support (anything else returns a well-formed `errors` array, never a crash):
    Query     tickets, ticket_comments, ticket_groups, ticket_categories, accounts (always empty)
    Mutation  createTicketComment(input: {ticket_id, body}), updateTicket(id, input: {...})
    Search    integer_fields / string_fields / date_fields, general_search, sorter, paginator,
              variables, aliases, fragments, @skip/@include.
    Strings follow GraphQL rules, so a raw newline inside a string literal is a syntax error,
    as on the real API.

Fixtures (--fixtures DIR, default scripts/demo/fixtures): tickets.json and profile.demo.json.
"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import re
import secrets
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HOST = "127.0.0.1"
DEFAULT_FIXTURES = Path(__file__).resolve().parent / "demo" / "fixtures"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PAGE_SIZE = 1000

TICKET_STATUSES = {"OPEN", "CLOSED", "PENDING_INTERNAL", "PENDING_EXTERNAL"}
TICKET_PRIORITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# GraphQL errors, lexer, parser
# ---------------------------------------------------------------------------

class GqlError(Exception):
    def __init__(self, message: str, path: Optional[list] = None, code: str = "GRAPHQL") -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.code = code

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"message": self.message,
                               "extensions": {"category": "graphql", "code": self.code}}
        if self.path:
            out["path"] = self.path
        return out


class EnumVal(str):
    """An unquoted GraphQL enum literal (EQ, DESC, CLOSED...)."""


class Var:
    def __init__(self, name: str) -> None:
        self.name = name


class Field:
    def __init__(self, alias: Optional[str], name: str, args: dict,
                 directives: list, selection: Optional[list]) -> None:
        self.alias = alias
        self.name = name
        self.args = args
        self.directives = directives
        self.selection = selection

    @property
    def key(self) -> str:
        return self.alias or self.name


class FragmentSpread:
    def __init__(self, name: str, directives: list) -> None:
        self.name = name
        self.directives = directives


class InlineFragment:
    def __init__(self, type_condition: Optional[str], directives: list, selection: list) -> None:
        self.type_condition = type_condition
        self.directives = directives
        self.selection = selection


class Operation:
    def __init__(self, kind: str, name: Optional[str], var_defs: dict, selection: list) -> None:
        self.kind = kind
        self.name = name
        self.var_defs = var_defs  # name -> default value AST (or _NO_DEFAULT)
        self.selection = selection


_NO_DEFAULT = object()
_PUNCT = set("!$&():=@[]{|}")
_NAME_RE = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _read_string(src: str, i: int) -> Tuple[str, int]:
    """Read a normal "..." string starting at src[i] == '"'. Returns (value, next index)."""
    i += 1
    out: List[str] = []
    n = len(src)
    while i < n:
        c = src[i]
        if c == '"':
            return "".join(out), i + 1
        if c in "\r\n":
            break
        if c == "\\":
            i += 1
            if i >= n:
                break
            e = src[i]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                i += 1
            elif e == "u":
                hexv = src[i + 1:i + 5]
                if len(hexv) != 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", hexv):
                    raise GqlError("Syntax Error: Invalid Unicode escape sequence.")
                out.append(chr(int(hexv, 16)))
                i += 5
            else:
                raise GqlError(f"Syntax Error: Invalid character escape sequence: \\{e}.")
            continue
        out.append(c)
        i += 1
    raise GqlError("Syntax Error: Unterminated string.")


def _read_block_string(src: str, i: int) -> Tuple[str, int]:
    i += 3
    out: List[str] = []
    n = len(src)
    while i < n:
        if src.startswith('"""', i):
            lines = "".join(out).split("\n")
            indents = [len(x) - len(x.lstrip()) for x in lines[1:] if x.strip()]
            cut = min(indents) if indents else 0
            lines = [lines[0]] + [x[cut:] for x in lines[1:]]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            return "\n".join(lines), i + 3
        if src.startswith('\\"""', i):
            out.append('"""')
            i += 4
            continue
        out.append(src[i])
        i += 1
    raise GqlError("Syntax Error: Unterminated string.")


def tokenize(src: str) -> List[Tuple[str, Any, int]]:
    toks: List[Tuple[str, Any, int]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            while i < n and src[i] not in "\r\n":
                i += 1
        elif src.startswith("...", i):
            toks.append(("spread", "...", i))
            i += 3
        elif c in _PUNCT:
            toks.append(("punct", c, i))
            i += 1
        elif c == '"':
            if src.startswith('"""', i):
                val, j = _read_block_string(src, i)
            else:
                val, j = _read_string(src, i)
            toks.append(("string", val, i))
            i = j
        elif c == "-" or c.isdigit():
            m = _NUM_RE.match(src, i)
            if not m:
                raise GqlError(f"Syntax Error: Invalid number, unexpected character {c!r}.")
            text = m.group(0)
            kind = "float" if (m.group(1) or m.group(2)) else "int"
            toks.append((kind, float(text) if kind == "float" else int(text), i))
            i = m.end()
        else:
            m = _NAME_RE.match(src, i)
            if not m:
                raise GqlError(f"Syntax Error: Unexpected character {c!r}.")
            toks.append(("name", m.group(0), i))
            i = m.end()
    toks.append(("eof", None, n))
    return toks


class Parser:
    def __init__(self, src: str) -> None:
        self.toks = tokenize(src)
        self.p = 0

    def peek(self) -> Tuple[str, Any, int]:
        return self.toks[self.p]

    def next(self) -> Tuple[str, Any, int]:
        t = self.toks[self.p]
        if t[0] != "eof":
            self.p += 1
        return t

    def at_punct(self, ch: str) -> bool:
        t = self.peek()
        return t[0] == "punct" and t[1] == ch

    def expect_punct(self, ch: str) -> None:
        t = self.next()
        if not (t[0] == "punct" and t[1] == ch):
            raise GqlError(f"Syntax Error: Expected {ch!r}, found {self._desc(t)}.")

    def expect_name(self) -> str:
        t = self.next()
        if t[0] != "name":
            raise GqlError(f"Syntax Error: Expected Name, found {self._desc(t)}.")
        return t[1]

    @staticmethod
    def _desc(t: Tuple[str, Any, int]) -> str:
        return "<EOF>" if t[0] == "eof" else repr(t[1])

    def parse_document(self) -> Tuple[List[Operation], Dict[str, list]]:
        ops: List[Operation] = []
        frags: Dict[str, list] = {}
        while self.peek()[0] != "eof":
            if self.at_punct("{"):
                ops.append(Operation("query", None, {}, self.parse_selection_set()))
                continue
            word = self.expect_name()
            if word in ("query", "mutation", "subscription"):
                name = None
                if self.peek()[0] == "name":
                    name = self.expect_name()
                var_defs = self.parse_var_defs() if self.at_punct("(") else {}
                self.parse_directives()
                ops.append(Operation(word, name, var_defs, self.parse_selection_set()))
            elif word == "fragment":
                fname = self.expect_name()
                if self.expect_name() != "on":
                    raise GqlError("Syntax Error: Expected 'on' in fragment definition.")
                self.expect_name()
                self.parse_directives()
                frags[fname] = self.parse_selection_set()
            else:
                raise GqlError(f"Syntax Error: Unexpected Name {word!r}.")
        if not ops:
            raise GqlError("Syntax Error: Unexpected <EOF>.")
        return ops, frags

    def parse_var_defs(self) -> dict:
        defs: Dict[str, Any] = {}
        self.expect_punct("(")
        while not self.at_punct(")"):
            self.expect_punct("$")
            name = self.expect_name()
            self.expect_punct(":")
            self.parse_type()
            default: Any = _NO_DEFAULT
            if self.at_punct("="):
                self.next()
                default = self.parse_value()
            self.parse_directives()
            defs[name] = default
        self.expect_punct(")")
        return defs

    def parse_type(self) -> None:
        if self.at_punct("["):
            self.next()
            self.parse_type()
            self.expect_punct("]")
        else:
            self.expect_name()
        if self.at_punct("!"):
            self.next()

    def parse_selection_set(self) -> list:
        self.expect_punct("{")
        sels: list = []
        while not self.at_punct("}"):
            if self.peek()[0] == "eof":
                raise GqlError("Syntax Error: Expected Name, found <EOF>.")
            sels.append(self.parse_selection())
        self.expect_punct("}")
        if not sels:
            raise GqlError("Syntax Error: Expected Name, found '}'.")
        return sels

    def parse_selection(self) -> Any:
        if self.peek()[0] == "spread":
            self.next()
            t = self.peek()
            if t[0] == "name" and t[1] != "on":
                name = self.expect_name()
                return FragmentSpread(name, self.parse_directives())
            type_cond = None
            if t[0] == "name" and t[1] == "on":
                self.next()
                type_cond = self.expect_name()
            directives = self.parse_directives()
            return InlineFragment(type_cond, directives, self.parse_selection_set())
        first = self.expect_name()
        alias, name = None, first
        if self.at_punct(":"):
            self.next()
            alias, name = first, self.expect_name()
        args = self.parse_arguments() if self.at_punct("(") else {}
        directives = self.parse_directives()
        selection = self.parse_selection_set() if self.at_punct("{") else None
        return Field(alias, name, args, directives, selection)

    def parse_arguments(self) -> dict:
        args: Dict[str, Any] = {}
        self.expect_punct("(")
        while not self.at_punct(")"):
            name = self.expect_name()
            self.expect_punct(":")
            args[name] = self.parse_value()
        self.expect_punct(")")
        return args

    def parse_directives(self) -> list:
        out = []
        while self.at_punct("@"):
            self.next()
            name = self.expect_name()
            out.append((name, self.parse_arguments() if self.at_punct("(") else {}))
        return out

    def parse_value(self) -> Any:
        t = self.next()
        kind, val = t[0], t[1]
        if kind == "punct" and val == "$":
            return Var(self.expect_name())
        if kind in ("int", "float", "string"):
            return val
        if kind == "name":
            if val == "true":
                return True
            if val == "false":
                return False
            if val == "null":
                return None
            return EnumVal(val)
        if kind == "punct" and val == "[":
            items = []
            while not self.at_punct("]"):
                if self.peek()[0] == "eof":
                    raise GqlError("Syntax Error: Expected Value, found <EOF>.")
                items.append(self.parse_value())
            self.expect_punct("]")
            return items
        if kind == "punct" and val == "{":
            obj: Dict[str, Any] = {}
            while not self.at_punct("}"):
                if self.peek()[0] == "eof":
                    raise GqlError("Syntax Error: Expected Name, found <EOF>.")
                key = self.expect_name()
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.expect_punct("}")
            return obj
        raise GqlError(f"Syntax Error: Unexpected {self._desc(t)}.")


def resolve_value(v: Any, variables: dict) -> Any:
    if isinstance(v, Var):
        return variables.get(v.name)
    if isinstance(v, list):
        return [resolve_value(x, variables) for x in v]
    if isinstance(v, dict):
        return {k: resolve_value(x, variables) for k, x in v.items()}
    return v


# ---------------------------------------------------------------------------
# Schema (just the parts NocAgent touches)
# ---------------------------------------------------------------------------

SCALARS = {"Int", "String", "Boolean", "Float", "ID"}
SCHEMA: Dict[str, Dict[str, str]] = {
    "Query": {
        "tickets": "TicketConnection",
        "ticket_comments": "TicketCommentConnection",
        "ticket_groups": "TicketGroupConnection",
        "ticket_categories": "TicketCategoryConnection",
        "accounts": "AccountConnection",
    },
    "Mutation": {
        "createTicketComment": "TicketComment",
        "updateTicket": "Ticket",
    },
    "Ticket": {
        "id": "Int", "subject": "String", "status": "String", "priority": "String",
        "description": "String", "created_at": "String", "updated_at": "String",
        "ticket_group_id": "Int", "ticket_group": "TicketGroup",
        "ticket_categories": "TicketCategoryConnection",
        "ticket_comments": "TicketCommentConnection", "user": "User",
    },
    "TicketComment": {
        "id": "Int", "ticket_id": "Int", "body": "String", "created_at": "String",
        "user": "User",
    },
    "TicketGroup": {"id": "Int", "name": "String"},
    "TicketCategory": {"id": "Int", "name": "String"},
    "User": {"id": "Int", "name": "String"},
    "Account": {"id": "Int", "name": "String", "account_status": "AccountStatus"},
    "AccountStatus": {"id": "Int", "name": "String", "activates_account": "Boolean"},
    "PageInfo": {"total_count": "Int", "page": "Int", "total_pages": "Int", "records_per_page": "Int"},
}
for _conn, _item in (("TicketConnection", "Ticket"), ("TicketCommentConnection", "TicketComment"),
                     ("TicketGroupConnection", "TicketGroup"),
                     ("TicketCategoryConnection", "TicketCategory"),
                     ("AccountConnection", "Account")):
    SCHEMA[_conn] = {"entities": f"[{_item}]", "page_info": "PageInfo"}

MUTATION_INPUT_FIELDS = {
    "updateTicket": {"subject", "status", "priority", "ticket_group_id", "ticket_category_ids", "user_id"},
    "createTicketComment": {"ticket_id", "body", "internal", "user_id"},
}


def _unwrap(typ: str) -> Tuple[str, bool]:
    if typ.startswith("[") and typ.endswith("]"):
        return typ[1:-1], True
    return typ, False


# ---------------------------------------------------------------------------
# Engine: fixtures, state, resolvers
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self.lock = threading.RLock()
        self._load()

    # -- state -----------------------------------------------------------
    def _load(self) -> None:
        with self.lock:
            tickets_doc = self._read_json("tickets.json")
            profile = self._read_json("profile.demo.json")
            sonar = profile.get("sonar") or {}
            self.groups: Dict[int, dict] = {}
            for g in (sonar.get("groups") or {}).values():
                self.groups[int(g["id"])] = {"id": int(g["id"]), "name": g.get("name", f"group-{g['id']}")}
            self.categories: Dict[int, dict] = {}
            for c in (sonar.get("categories") or {}).values():
                self.categories[int(c["id"])] = {"id": int(c["id"]), "name": c.get("name", f"category-{c['id']}")}
            self.tickets: Dict[int, dict] = {}
            self.comments: List[dict] = []
            next_comment = 7001
            for raw in tickets_doc.get("tickets", []):
                tid = int(raw["id"])
                self.tickets[tid] = {
                    "id": tid,
                    "subject": raw.get("subject", ""),
                    "status": raw.get("status", "OPEN"),
                    "priority": raw.get("priority", "MEDIUM"),
                    "description": raw.get("description", ""),
                    "ticket_group_id": int(raw["ticket_group_id"]),
                    "category_ids": [int(x) for x in raw.get("ticket_category_ids", [])],
                    "created_at": raw.get("created_at", now_iso()),
                    "updated_at": raw.get("updated_at", raw.get("created_at", now_iso())),
                    "user": raw.get("user") or {"id": 1, "name": "Demo User"},
                }
                for c in raw.get("comments", []):
                    self.comments.append({
                        "id": next_comment, "ticket_id": tid, "body": c.get("body", ""),
                        "created_at": c.get("created_at", raw.get("created_at", now_iso())),
                        "user": c.get("user") or {"id": 1, "name": "Demo User"},
                    })
                    next_comment += 1
            self.next_comment_id = next_comment
            self.audit: List[dict] = []
            self.chat: List[dict] = []
            self.counters = {"requests": 0, "queries": 0, "mutations": 0,
                             "auth_failures": 0, "graphql_errors": 0}
            self.seq = 0

    def _read_json(self, name: str) -> dict:
        path = self.fixtures_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot load fixture {path}: {exc}") from exc

    def reset(self) -> dict:
        self._load()
        return {"ok": True, "tickets": len(self.tickets), "comments": len(self.comments)}

    def audit_snapshot(self) -> dict:
        with self.lock:
            return {"audit": copy.deepcopy(self.audit), "counts": dict(self.counters)}

    def add_chat(self, payload: Any) -> dict:
        with self.lock:
            rec = {"seq": len(self.chat) + 1, "ts": now_iso(), "payload": payload}
            self.chat.append(rec)
            return {"name": f"spaces/DEMO/messages/{len(self.chat)}"}

    def chat_snapshot(self) -> dict:
        with self.lock:
            return {"chat": copy.deepcopy(self.chat)}

    # -- public entry ----------------------------------------------------
    def execute(self, body: Any) -> dict:
        with self.lock:
            self.counters["requests"] += 1
            try:
                result = self._execute(body)
            except GqlError as exc:
                result = {"errors": [exc.to_dict()]}
            except Exception as exc:  # never crash the server on odd input
                result = {"errors": [{"message": f"Internal mock error: {type(exc).__name__}: {exc}",
                                      "extensions": {"category": "internal"}}]}
            if result.get("errors"):
                self.counters["graphql_errors"] += 1
            return result

    def _execute(self, body: Any) -> dict:
        if isinstance(body, list):
            raise GqlError("Batched queries are not supported by the mock.")
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            raise GqlError("No query string was present.")
        variables = body.get("variables") or {}
        if not isinstance(variables, dict):
            raise GqlError("Variables must be a JSON object.")
        ops, frags = Parser(body["query"]).parse_document()
        op = self._pick_operation(ops, body.get("operationName"))
        if op.kind == "subscription":
            raise GqlError("Subscriptions are not supported by the mock.")
        variables = dict(variables)
        for name, default in op.var_defs.items():
            if name not in variables and default is not _NO_DEFAULT:
                variables[name] = resolve_value(default, {})
        root_type = "Query" if op.kind == "query" else "Mutation"
        ctx = _Ctx(frags, variables)
        fields = self._collect(op.selection, ctx, root_type)
        self._validate_fields(fields, root_type, ctx, [])
        data: Dict[str, Any] = {}
        errors: List[dict] = []
        self.counters["queries" if op.kind == "query" else "mutations"] += 1
        for f in fields:
            if f.name == "__typename":
                data[f.key] = root_type
                continue
            try:
                if op.kind == "query":
                    raw = self._root_query(f, ctx)
                else:
                    raw = self._root_mutation(f, ctx)
                data[f.key] = self._project(raw, f, SCHEMA[root_type][f.name], ctx, [f.key])
            except GqlError as exc:
                data[f.key] = None
                errors.append(GqlError(exc.message, [f.key], exc.code).to_dict())
        out: Dict[str, Any] = {"data": data}
        if errors:
            out["errors"] = errors
        return out

    @staticmethod
    def _pick_operation(ops: List[Operation], name: Optional[str]) -> Operation:
        if name:
            for op in ops:
                if op.name == name:
                    return op
            raise GqlError(f'Unknown operation named "{name}".')
        if len(ops) > 1:
            raise GqlError("Must provide operation name if query contains multiple operations.")
        return ops[0]

    # -- selection handling ---------------------------------------------
    def _collect(self, selection: list, ctx: "_Ctx", typ: str, seen: Optional[set] = None) -> List[Field]:
        seen = seen if seen is not None else set()
        out: List[Field] = []
        for s in selection:
            if not self._directives_ok(getattr(s, "directives", []), ctx):
                continue
            if isinstance(s, Field):
                out.append(s)
            elif isinstance(s, FragmentSpread):
                if s.name in seen:
                    continue
                if s.name not in ctx.fragments:
                    raise GqlError(f'Unknown fragment "{s.name}".')
                seen.add(s.name)
                out.extend(self._collect(ctx.fragments[s.name], ctx, typ, seen))
            elif isinstance(s, InlineFragment):
                if s.type_condition in (None, typ):
                    out.extend(self._collect(s.selection, ctx, typ, seen))
        return out

    @staticmethod
    def _directives_ok(directives: list, ctx: "_Ctx") -> bool:
        for name, args in directives:
            if name in ("skip", "include"):
                cond = resolve_value(args.get("if"), ctx.variables)
                if name == "skip" and cond:
                    return False
                if name == "include" and not cond:
                    return False
        return True

    def _validate_fields(self, fields: List[Field], typ: str, ctx: "_Ctx", path: list) -> None:
        schema = SCHEMA[typ]
        for f in fields:
            if f.name == "__typename":
                continue
            if f.name.startswith("__"):
                raise GqlError("Introspection is not supported by the mock.")
            if f.name not in schema:
                raise GqlError(f'Cannot query field "{f.name}" on type "{typ}".', path + [f.key], "VALIDATION")
            inner, _ = _unwrap(schema[f.name])
            if inner in SCALARS:
                if f.selection is not None:
                    raise GqlError(f'Field "{f.name}" must not have a selection since type "{inner}" has no subfields.',
                                   path + [f.key], "VALIDATION")
            else:
                if f.selection is None:
                    raise GqlError(f'Field "{f.name}" of type "{inner}" must have a selection of subfields.',
                                   path + [f.key], "VALIDATION")
                sub = self._collect(f.selection, ctx, inner)
                self._validate_fields(sub, inner, ctx, path + [f.key])
        if typ == "Mutation":
            for f in fields:
                self._validate_mutation_args(f, ctx)

    def _validate_mutation_args(self, f: Field, ctx: "_Ctx") -> None:
        if f.name == "updateTicket" and ("id" not in f.args or "input" not in f.args):
            raise GqlError('Field "updateTicket" argument "id" and "input" are required.', [f.key], "VALIDATION")
        if f.name == "createTicketComment" and "input" not in f.args:
            raise GqlError('Field "createTicketComment" argument "input" is required.', [f.key], "VALIDATION")

    def _project(self, value: Any, field: Field, typ_spec: str, ctx: "_Ctx", path: list) -> Any:
        inner, is_list = _unwrap(typ_spec)
        if value is None:
            return None
        if is_list:
            return [self._project(v, field, inner, ctx, path + [i]) for i, v in enumerate(value)]
        if inner in SCALARS:
            return value
        out: Dict[str, Any] = {}
        for f in self._collect(field.selection or [], ctx, inner):
            if f.name == "__typename":
                out[f.key] = inner
                continue
            sub_spec = SCHEMA[inner][f.name]
            raw = self._field_value(inner, value, f, ctx)
            out[f.key] = self._project(raw, f, sub_spec, ctx, path + [f.key])
        return out

    # -- field resolvers -------------------------------------------------
    def _field_value(self, typ: str, obj: dict, f: Field, ctx: "_Ctx") -> Any:
        args = resolve_value(f.args, ctx.variables)
        if typ == "Ticket":
            if f.name == "ticket_group":
                return self.groups.get(obj["ticket_group_id"])
            if f.name == "ticket_categories":
                cats = [self.categories[c] for c in obj["category_ids"] if c in self.categories]
                return self._connection(cats, args, {"id"}, lambda it, a: it.get(a))
            if f.name == "ticket_comments":
                rows = [c for c in self.comments if c["ticket_id"] == obj["id"]]
                return self._comment_connection(rows, args)
        return obj.get(f.name)

    def _root_query(self, f: Field, ctx: "_Ctx") -> Any:
        args = resolve_value(f.args, ctx.variables)
        if f.name == "tickets":
            rows = self._filter(list(self.tickets.values()), args.get("search"),
                                int_attrs={"id", "ticket_group_id"},
                                str_attrs={"id", "subject", "description", "status", "priority"},
                                date_attrs={"created_at", "updated_at"}, label="tickets")
            gs = args.get("general_search")
            if gs:
                needle = str(gs).lower()
                rows = [t for t in rows if needle in t["subject"].lower() or needle in t["description"].lower()]
            return self._connection(rows, args, {"id", "created_at", "updated_at"}, lambda it, a: it.get(a))
        if f.name == "ticket_comments":
            rows = self._filter(list(self.comments), args.get("search"),
                                int_attrs={"id", "ticket_id"}, str_attrs={"body"},
                                date_attrs={"created_at"}, label="ticket_comments")
            return self._comment_connection(rows, args)
        if f.name == "ticket_groups":
            return self._connection(list(self.groups.values()), args, {"id", "name"}, lambda it, a: it.get(a))
        if f.name == "ticket_categories":
            return self._connection(list(self.categories.values()), args, {"id", "name"}, lambda it, a: it.get(a))
        if f.name == "accounts":
            return self._connection([], args, {"id", "name"}, lambda it, a: it.get(a))
        raise GqlError(f'Unsupported query field "{f.name}".')

    def _root_mutation(self, f: Field, ctx: "_Ctx") -> Any:
        args = resolve_value(f.args, ctx.variables)
        if f.name == "createTicketComment":
            return self._m_create_comment(args)
        if f.name == "updateTicket":
            return self._m_update_ticket(args)
        raise GqlError(f'Unsupported mutation field "{f.name}".')

    # -- mutations -------------------------------------------------------
    def _record(self, field: str, args: dict, ticket_id: Any, ok: bool, **extra: Any) -> None:
        self.seq += 1
        rec = {"seq": self.seq, "ts": now_iso(), "operation": "mutation", "field": field,
               "ticket_id": ticket_id, "args": args, "ok": ok}
        rec.update(extra)
        self.audit.append(rec)

    @staticmethod
    def _as_int(value: Any, what: str) -> int:
        if isinstance(value, bool):
            raise GqlError(f"{what} must be an integer.")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise GqlError(f"{what} must be an integer.")

    def _m_create_comment(self, args: dict) -> dict:
        inp = args.get("input")
        try:
            if not isinstance(inp, dict):
                raise GqlError("Argument input must be an object.")
            unknown = set(inp) - MUTATION_INPUT_FIELDS["createTicketComment"]
            if unknown:
                raise GqlError(f'Field "{sorted(unknown)[0]}" is not defined by type CreateTicketCommentMutationInput.')
            if "ticket_id" not in inp or "body" not in inp:
                raise GqlError("Fields ticket_id and body are required.")
            tid = self._as_int(inp["ticket_id"], "ticket_id")
            body = inp["body"]
            if not isinstance(body, str) or not body.strip():
                raise GqlError("body must be a non-empty string.")
            if tid not in self.tickets:
                raise GqlError(f"Ticket {tid} does not exist.")
        except GqlError as exc:
            self._record("createTicketComment", self._brief_args(args), inp.get("ticket_id") if isinstance(inp, dict) else None,
                         False, error=exc.message)
            raise
        comment = {"id": self.next_comment_id, "ticket_id": tid, "body": body, "created_at": now_iso(),
                   "user": {"id": 99, "name": "NocAgent (demo)"}}
        self.next_comment_id += 1
        self.comments.append(comment)
        self._record("createTicketComment", {"input": {"ticket_id": tid, "body_chars": len(body)}}, tid, True,
                     comment_id=comment["id"], body=body[:2000])
        return comment

    def _m_update_ticket(self, args: dict) -> dict:
        inp = args.get("input")
        tid_raw = args.get("id")
        try:
            tid = self._as_int(tid_raw, "id")
            if tid not in self.tickets:
                raise GqlError(f"Ticket {tid} does not exist.")
            if not isinstance(inp, dict):
                raise GqlError("Argument input must be an object.")
            unknown = set(inp) - MUTATION_INPUT_FIELDS["updateTicket"]
            if unknown:
                raise GqlError(f'Field "{sorted(unknown)[0]}" is not defined by type UpdateTicketMutationInput.')
            changes: Dict[str, Any] = {}
            if "status" in inp:
                status = str(inp["status"]).upper()
                if status not in TICKET_STATUSES:
                    raise GqlError(f'Value "{inp["status"]}" does not exist in "TicketStatus" enum.')
                changes["status"] = status
            if "priority" in inp:
                prio = str(inp["priority"]).upper()
                if prio not in TICKET_PRIORITIES:
                    raise GqlError(f'Value "{inp["priority"]}" does not exist in "Priority" enum.')
                changes["priority"] = prio
            if "subject" in inp:
                if not isinstance(inp["subject"], str) or not inp["subject"].strip():
                    raise GqlError("subject must be a non-empty string.")
                changes["subject"] = inp["subject"]
            if "ticket_group_id" in inp:
                gid = self._as_int(inp["ticket_group_id"], "ticket_group_id")
                if gid not in self.groups:
                    raise GqlError(f"Ticket group {gid} does not exist.")
                changes["ticket_group_id"] = gid
            if "ticket_category_ids" in inp:
                ids = inp["ticket_category_ids"]
                if not isinstance(ids, list):
                    raise GqlError("ticket_category_ids must be a list.")
                cat_ids = [self._as_int(x, "ticket_category_ids entry") for x in ids]
                for cid in cat_ids:
                    if cid not in self.categories:
                        raise GqlError(f"Ticket category {cid} does not exist.")
                changes["category_ids"] = cat_ids
        except GqlError as exc:
            self._record("updateTicket", self._brief_args(args), tid_raw, False, error=exc.message)
            raise
        t = self.tickets[tid]
        before = self._ticket_brief(t)
        t.update(changes)
        t["updated_at"] = now_iso()
        self._record("updateTicket", {"id": tid, "input": self._brief_input(inp)}, tid, True,
                     before=before, after=self._ticket_brief(t))
        return t

    @staticmethod
    def _ticket_brief(t: dict) -> dict:
        return {"status": t["status"], "priority": t["priority"], "ticket_group_id": t["ticket_group_id"],
                "ticket_category_ids": list(t["category_ids"])}

    @staticmethod
    def _brief_input(inp: dict) -> dict:
        return {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "...") for k, v in inp.items()}

    def _brief_args(self, args: dict) -> dict:
        out = {}
        for k, v in args.items():
            out[k] = self._brief_input(v) if isinstance(v, dict) else v
        return out

    # -- search / sort / paginate ---------------------------------------
    def _comment_connection(self, rows: List[dict], args: dict) -> dict:
        return self._connection(rows, args, {"id", "created_at"}, lambda it, a: it.get(a))

    def _connection(self, rows: List[dict], args: dict, sort_attrs: set,
                    getter: Callable[[dict, str], Any]) -> dict:
        rows = self._sort(rows, args.get("sorter"), sort_attrs, getter)
        pag = args.get("paginator") or {}
        if not isinstance(pag, dict):
            raise GqlError("paginator must be an object.")
        page = max(1, self._as_int(pag.get("page", 1), "paginator.page"))
        per = self._as_int(pag.get("records_per_page", 25), "paginator.records_per_page")
        per = min(max(per, 1), MAX_PAGE_SIZE)
        total = len(rows)
        start = (page - 1) * per
        return {
            "entities": rows[start:start + per],
            "page_info": {"total_count": total, "page": page,
                          "total_pages": max(1, -(-total // per)) if total else 0,
                          "records_per_page": per},
        }

    def _sort(self, rows: List[dict], sorter: Any, allowed: set,
              getter: Callable[[dict, str], Any]) -> List[dict]:
        sorters = sorter if isinstance(sorter, list) else ([sorter] if sorter else [])
        out = list(rows)
        out.sort(key=lambda r: r.get("id", 0) if isinstance(r.get("id", 0), int) else 0)
        for s in reversed(sorters):
            if not isinstance(s, dict) or "attribute" not in s:
                raise GqlError("sorter must be {attribute, direction}.")
            attr = str(s["attribute"])
            if attr not in allowed:
                raise GqlError(f'Cannot sort by "{attr}". Supported: {", ".join(sorted(allowed))}.')
            direction = str(s.get("direction", "ASC")).upper()
            if direction not in ("ASC", "DESC"):
                raise GqlError(f'Value "{s.get("direction")}" does not exist in "SortDirection" enum.')
            out.sort(key=lambda r, a=attr: (str(getter(r, a)), r.get("id", 0)) if a != "id" else (r.get("id", 0), 0),
                     reverse=(direction == "DESC"))
        return out

    def _filter(self, rows: List[dict], search: Any, int_attrs: set, str_attrs: set,
                date_attrs: set, label: str) -> List[dict]:
        if not search:
            return rows
        searches = search if isinstance(search, list) else [search]
        for sd in searches:
            if not isinstance(sd, dict):
                raise GqlError("search must be an object (or a list of objects).")
            unknown = set(sd) - {"integer_fields", "string_fields", "date_fields"}
            if unknown:
                raise GqlError(f'Unsupported search key "{sorted(unknown)[0]}" for {label}.')
            rows = [r for r in rows if self._row_matches(r, sd, int_attrs, str_attrs, date_attrs, label)]
        return rows

    def _row_matches(self, row: dict, sd: dict, int_attrs: set, str_attrs: set,
                     date_attrs: set, label: str) -> bool:
        groups: Dict[Tuple[str, str], List[bool]] = {}
        for ent in self._entries(sd.get("integer_fields"), "integer_fields"):
            attr = self._attr(ent, int_attrs, label)
            groups.setdefault(("int", attr), []).append(self._int_hit(row, attr, ent))
        for ent in self._entries(sd.get("date_fields"), "date_fields"):
            attr = self._attr(ent, date_attrs, label)
            groups.setdefault(("date", attr), []).append(self._date_hit(row, attr, ent))
        neg_ok = True
        str_groups: Dict[str, List[bool]] = {}
        for ent in self._entries(sd.get("string_fields"), "string_fields"):
            attr = self._attr(ent, str_attrs, label)
            hit = self._str_hit(row, attr, ent)
            if ent.get("match", True) is False:
                neg_ok = neg_ok and hit  # _str_hit already negated for match:false
            else:
                str_groups.setdefault(attr, []).append(hit)
        if not neg_ok:
            return False
        ok = all(any(v) for v in groups.values())
        text_groups = {a: any(v) for a, v in str_groups.items()}
        # Tolerance: a search over both subject and description means "either" (sonar-api skill).
        if "subject" in text_groups and "description" in text_groups:
            subj_hit = text_groups.pop("subject")
            desc_hit = text_groups.pop("description")
            ok = ok and (subj_hit or desc_hit)
        return ok and all(text_groups.values())

    @staticmethod
    def _entries(value: Any, key: str) -> List[dict]:
        if value is None:
            return []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise GqlError(f"{key} must be a list of objects.")
        return value

    @staticmethod
    def _attr(ent: dict, allowed: set, label: str) -> str:
        attr = ent.get("attribute")
        if attr not in allowed:
            raise GqlError(f'Cannot search {label} by "{attr}". Supported: {", ".join(sorted(allowed))}.')
        if "search_value" not in ent:
            raise GqlError("search_value is required.")
        return attr

    def _int_hit(self, row: dict, attr: str, ent: dict) -> bool:
        have = self._as_int(row.get(attr), attr)
        want = self._as_int(ent["search_value"], "search_value")
        op = str(ent.get("operator", "EQ")).upper()
        ops = {"EQ": have == want, "NEQ": have != want, "GT": have > want,
               "GTE": have >= want, "LT": have < want, "LTE": have <= want}
        if op not in ops:
            raise GqlError(f'Value "{ent.get("operator")}" does not exist in "SearchOperator" enum.')
        return ops[op]

    @staticmethod
    def _parse_ts(value: Any) -> datetime:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise GqlError(f'Cannot parse date "{value}".')
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _date_hit(self, row: dict, attr: str, ent: dict) -> bool:
        have = self._parse_ts(row.get(attr))
        want = self._parse_ts(ent["search_value"])
        op = str(ent.get("operator", "EQ")).upper()
        ops = {"EQ": have == want, "GT": have > want, "GTE": have >= want,
               "LT": have < want, "LTE": have <= want}
        if op not in ops:
            raise GqlError(f'Value "{ent.get("operator")}" does not exist in "SearchOperator" enum.')
        return ops[op]

    @staticmethod
    def _str_hit(row: dict, attr: str, ent: dict) -> bool:
        have = str(row.get(attr, "")).lower()
        want = str(ent["search_value"]).lower()
        if attr == "id" or ent.get("partial_matching") is False:
            hit = have == want
        else:
            hit = want in have
        return hit if ent.get("match", True) is not False else not hit


class _Ctx:
    def __init__(self, fragments: Dict[str, list], variables: dict) -> None:
        self.fragments = fragments
        self.variables = variables


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def make_handler(srv: "MockSonarServer") -> type:
    engine = srv.engine

    class Handler(BaseHTTPRequestHandler):
        server_version = "MockSonar/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if srv.verbose:
                sys.stderr.write("[mock_sonar] %s\n" % (fmt % args))

        # -- helpers
        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            parts = header.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                supplied = parts[1].strip().encode("utf-8")
                ok = False
                for tok in srv.tokens:
                    ok = hmac.compare_digest(supplied, tok.encode("utf-8")) or ok
                if ok:
                    return True
            with engine.lock:
                engine.counters["auth_failures"] += 1
            self._send(401, {"errors": [{"message": "Unauthenticated.",
                                         "extensions": {"category": "authentication", "code": "UNAUTHENTICATED"}}]})
            return False

        def _read_json(self) -> Tuple[bool, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, {"errors": [{"message": "Request body too large."}]})
                return False, None
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return True, None
            try:
                return True, json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"errors": [{"message": "Request body is not valid JSON."}]})
                return False, None

        # -- verbs
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/__health"):
                return self._send(200, {"status": "ok", "service": "mock-sonar"})
            if path in ("/api/graphql", "/graphql"):
                return self._send(405, {"errors": [{"message": "Use POST for GraphQL requests."}]})
            if path == "/__audit":
                if self._authorized():
                    self._send(200, engine.audit_snapshot())
                return
            if path == "/__chat":
                if self._authorized():
                    self._send(200, engine.chat_snapshot())
                return
            self._send(404, {"errors": [{"message": f"No route for GET {path}."}]})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/api/graphql", "/graphql"):
                if not self._authorized():
                    return
                ok, body = self._read_json()
                if not ok:
                    return
                return self._send(200, engine.execute(body))
            if path == "/__reset":
                if self._authorized():
                    self._send(200, engine.reset())
                return
            if path == "/chat/webhook" or path.startswith("/chat/webhook/"):
                ok, body = self._read_json()
                if ok:
                    self._send(200, engine.add_chat(body))
                return
            self._send(404, {"errors": [{"message": f"No route for POST {path}."}]})

        def do_PUT(self) -> None:
            self._send(405, {"errors": [{"message": "Method not allowed."}]})

        do_DELETE = do_PATCH = do_PUT

    return Handler


class MockSonarServer:
    """Embeddable server: `srv = MockSonarServer(...); srv.start(); ...; srv.stop()`."""

    def __init__(self, fixtures_dir: Optional[Path] = None, tokens: Optional[List[str]] = None,
                 port: int = 0, verbose: bool = False) -> None:
        self.engine = Engine(Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES)
        self.tokens = list(tokens or [])
        if not self.tokens:
            raise ValueError("at least one accepted token is required")
        self.verbose = verbose
        self.httpd = ThreadingHTTPServer((HOST, port), make_handler(self))
        self.httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def start(self) -> "MockSonarServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="mock-sonar", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def generate_token() -> str:
    return "demo-" + secrets.token_urlsafe(24)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="In-memory Sonar GraphQL mock (127.0.0.1 only) for NocAgent demo mode.")
    p.add_argument("--port", type=int, default=0, help="TCP port; 0 picks an ephemeral port (default)")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES),
                   help="directory holding tickets.json and profile.demo.json")
    p.add_argument("--token", action="append", default=[],
                   help="accepted bearer token; repeatable. Default: one generated at start and printed")
    p.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = p.parse_args(argv)

    generated = None
    tokens = list(args.token)
    if not tokens:
        generated = generate_token()
        tokens = [generated]
    try:
        srv = MockSonarServer(Path(args.fixtures), tokens, port=args.port, verbose=args.verbose)
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"event": "error", "message": str(exc)}), flush=True)
        return 2
    info: Dict[str, Any] = {"event": "listening", "host": HOST, "port": srv.port, "url": srv.url}
    if generated:
        info["token"] = generated
    print(json.dumps(info), flush=True)

    def _stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=srv.httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    finally:
        srv.httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
