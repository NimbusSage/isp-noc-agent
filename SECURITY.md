This tool can hold router and ticket-system credentials, read customer ticket text, and send commands to live network devices. A flaw disclosed publicly before it is patched could reach production gear.

If you found a vulnerability, please do not open a public issue, pull request, or discussion. Report it privately through GitHub's security advisory feature:

[Report a vulnerability](https://github.com/NimbusSage/isp-noc-agent/security/advisories/new)

## Supported

There are no tagged releases. The only supported version is the latest commit on the master branch. If you are testing a vulnerability, test against master.

## Report

Use the private advisory link above. A useful report covers:

- What you found and why it matters
- Where it is: a file path, an endpoint, or an affected component
- How to reproduce it
- Which commit you tested

## Scope

These count as vulnerabilities in this project:

- A way to bypass the webhook server's authentication
- A way to run extra commands on a router or radio through the MCP server's tools, beyond what the tool is supposed to allow
- A way around the write gates: the kill switch, the circuit breaker, or the host allowlist
- A way to read credentials or customer data through the tool or its logs

## Secrets

Real credentials belong in a local .env file and an encrypted store outside the repository, not in git. If you find a password, token, customer name, or network address committed to the repo, treat it the same way: file a private advisory. Do not paste the value into an issue, a comment, a pull request, or any other public place. Just say where you found it and which commit.
