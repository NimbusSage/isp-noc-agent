#!/usr/bin/env python3
"""
sonar_comment.py — Canonical helper to post HTML comments to Sonar tickets.

Eliminates the GraphQL/JSON escaping nightmare that once caused a ticket
to receive literal `BODYPLACEHOLDER` (see 2026-08-30 post-mortem).

Usage:
    python3 scripts/sonar_comment.py <ticket_id> <html_file> [--verify]
                                     [--preserve-newlines]

- <ticket_id> : Sonar ticket ID. Must be a positive integer (digits only);
                anything else exits 1 before any network call.
- <html_file> : Path to file containing the HTML body. Attribute quotes may be
                written as &quot; (the long-standing house convention, still
                accepted unchanged) or as raw double quotes: the body now travels
                as a GraphQL *variable* (JSON), never spliced into the query text,
                so quote/backslash/newline content cannot alter the operation.
- --verify    : After posting, fetch the comment back and verify it contains
                the expected marker and not BODYPLACEHOLDER.
- --preserve-newlines : keep newlines/tabs in the stored body. Default is to
                collapse them to a single line exactly as before, so comments
                render and index (correlated-issues) byte-for-byte as they always
                have. Env SONAR_COMMENT_PRESERVE_NEWLINES=1 does the same.

Why variables?
    The old code built `createTicketComment(input: {..., body: "<html>"})` with an
    f-string. Any `"` or backslash in a ticket-derived body could end the string
    literal and inject GraphQL, which is also why raw `style="` had to be banned and
    newlines flattened. With `$input` as a typed variable none of that is possible.

Exit codes:
    0 — comment posted and verified
    1 — missing args / file not found / ticket id not an integer
    2 — Sonar API error: transport/HTTP failure, GraphQL `errors`, `"data": null`,
        missing comment id, no SONAR_TOKEN, or an input guard tripped
        (BODYPLACEHOLDER residue, body < 50 chars)
    3 — verification failed (stored body contains the placeholder)
    (If the post succeeded but the read-back fetch itself fails, a WARNING is printed
    and the exit stays 0: the comment exists, so a retry would only duplicate it.)

Environment:
    SONAR_TOKEN (environment or .env; read with scripts/envfile.py - no shell),
    SONAR_BASE_URL (else site-pack profile org.sonar_base_url).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gql_client  # noqa: E402

MAX_HTML_BYTES = 1024 * 1024

POST_MUTATION = (
    "mutation PostTicketComment($input: CreateTicketCommentMutationInput!) { "
    "createTicketComment(input: $input) { id } }"
)


def _collapse(html: str) -> str:
    """Historic single-line normalisation (kept by default: it fixes how stored
    comments look/parse on the live tenant)."""
    return html.replace("\r", "").replace("\n", "").replace("\t", " ")


def _fail(code: int, msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def post_comment(ticket_id: int, html: str, preserve_newlines: bool = False) -> str:
    # Input guards - must be before any network call.
    if not preserve_newlines and ("\n" in html or "\r" in html or "\t" in html):
        html = _collapse(html)

    # Reject BODYPLACEHOLDER template residue (the original bug was a failed
    # payload_str.replace('"BODYPLACEHOLDER"', ...)); case-insensitive, anywhere.
    if "bodyplaceholder" in html.lower():
        _fail(2, "ERROR: html contains BODYPLACEHOLDER (any case) — placeholder bug, refusing to post")

    # Reject tiny probe payloads that were left as garbage.
    if len(html.strip()) < 50:
        _fail(2, f"ERROR: html too short ({len(html.strip())} chars) — likely probe/test payload, refusing (min 50)")

    variables = {"input": {"ticket_id": ticket_id, "body": html}}
    try:
        data = gql_client.execute(POST_MUTATION, variables)
        comment_id = gql_client.dig(data, "createTicketComment", "id")
    except gql_client.GraphQLError as e:
        _fail(2, f"ERROR: Sonar API ({e.kind}): {e.message}")
    if not comment_id:
        _fail(2, "ERROR: missing id in response (not posted)")

    print(f"Comment posted: {comment_id}")
    return str(comment_id)


def verify_comment(ticket_id: int, comment_id: str) -> None:
    try:
        entities = gql_client.fetch_recent_comments(ticket_id)
    except gql_client.GraphQLError as e:
        print(f"WARNING: verify fetch failed ({e.kind}): {e.message}", file=sys.stderr)
        return

    # Find our comment
    found = next((e for e in entities if str(e.get("id")) == str(comment_id)), None)
    if not found and entities:
        found = entities[0]  # fallback to most recent
    if not found:
        print("WARNING: verify: no comments found to verify", file=sys.stderr)
        return

    body = found.get("body") or ""
    if "bodyplaceholder" in body.lower():
        print(f"ERROR: verification failed — stored body contains BODYPLACEHOLDER (id {found['id']})", file=sys.stderr)
        sys.exit(3)
    if "\n" in body or "\r" in body:
        print(f"NOTE: stored body contains newlines (id {found['id']}) — expected only with --preserve-newlines",
              file=sys.stderr)
    markers = ("Network Bottleneck Analyzer", "Duplicate Ticket", "Correlated",
               "Direct Path Analysis", "Rain Fade Readiness")
    if not any(m in body for m in markers):
        print(
            f"WARNING: verification: body missing expected marker (Direct Path Analysis / Network Bottleneck Analyzer / Duplicate / Correlated / Rain Fade Readiness). id={found['id']} len={len(body)}",
            file=sys.stderr,
        )
        # not fatal — caller can decide
    else:
        # Also ensure no placeholder residue and body is not tiny probe
        if len(body.strip()) < 50:
            print(f"WARNING: verification: body too short ({len(body.strip())} chars) — possible probe leftover id={found['id']}", file=sys.stderr)
        else:
            print(f"Verified: body OK (id {found['id']}, {len(body)} chars, marker present, no placeholder)")


def main() -> None:
    argv = [a for a in sys.argv[1:]]
    flags = {a for a in argv if a.startswith("--")}
    pos = [a for a in argv if not a.startswith("--")]
    unknown = flags - {"--verify", "--preserve-newlines"}
    if len(pos) != 2 or unknown:
        _fail(1, f"Usage: {sys.argv[0]} <ticket_id> <html_file> [--verify] [--preserve-newlines]")

    try:
        ticket_id = gql_client.parse_ticket_id(pos[0])
    except ValueError as e:
        _fail(1, f"ERROR: {e}")
    html_file = pos[1]
    do_verify = "--verify" in flags
    preserve = "--preserve-newlines" in flags or os.environ.get("SONAR_COMMENT_PRESERVE_NEWLINES") == "1"

    path = Path(html_file)
    if not path.is_file():
        _fail(1, f"ERROR: html file not found: {html_file}")
    if path.stat().st_size > MAX_HTML_BYTES:
        _fail(1, f"ERROR: html file larger than {MAX_HTML_BYTES} bytes — refusing")

    html = path.read_text(encoding="utf-8").strip()
    if not html:
        _fail(1, "ERROR: html file empty — refusing to post empty comment")

    comment_id = post_comment(ticket_id, html, preserve)
    if do_verify:
        verify_comment(ticket_id, comment_id)


if __name__ == "__main__":
    main()
