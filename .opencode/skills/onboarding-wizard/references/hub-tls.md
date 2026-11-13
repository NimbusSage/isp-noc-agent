# Hub reference - trusted HTTPS over ACME DNS-01

Only needed when the hub serves something a browser talks to (a web UI, or the Google
sign-in redirect). Ticket processing does not need it.

The hub gets a publicly-trusted certificate **without any inbound port**: the ACME
DNS-01 challenge is answered with one TXT record in the organization's own DNS server.
`scripts/hub_tls.py` decides what to ask for and renders the Caddy site block; `lego`
does the ACME work.

## Ask the operator (one at a time)

1. Public host name for the hub (for example `hub.example.com`) and the email that gets
   certificate expiry notices.
2. Which DNS server holds the zone and whether it accepts RFC 2136 dynamic updates
   (Technitium, BIND, PowerDNS, Knot: yes; NSD: no). If it does, the operator creates a
   TSIG key and scopes it to `_acme-challenge.<host>` / `TXT`.
3. Only if there is no dynamic update: the Technitium console URL, for the API-token
   backend. Say plainly that such a token can usually rewrite the whole zone.

## Facts to state before touching anything

- Do not forward 80 or 443 for the certificate. DNS-01 needs no inbound port, and an
  open port is exposed to anyone who finds it.
- The host name is published in Certificate Transparency logs. The address itself stays
  private if the `A` record points at the LAN address.
- The TSIG secret and any API token go in through `env_set.py` (hidden prompt). You never
  see them, never repeat them, never put them on a command line.

## Commands

```bash
python3 scripts/env_set.py DNSUPDATE_TSIG_SECRET      # operator runs this
python3 scripts/hub_tls.py --check                     # names every missing setting
python3 scripts/hub_tls.py --issue --staging           # proves the DNS path, untrusted cert
python3 scripts/hub_tls.py --issue                     # real certificate
sudo python3 scripts/hub_tls.py --write-caddy          # renders the Caddy site block
sudo systemctl reload caddy
```

`HUB_TLS_DNS_PROVIDER=dnsupdate` (default) uses `DNSUPDATE_NAMESERVER`,
`DNSUPDATE_TSIG_KEY`, `DNSUPDATE_TSIG_SECRET`, `DNSUPDATE_TSIG_ALGORITHM`.
`HUB_TLS_DNS_PROVIDER=technitium` uses `TECHNITIUM_SERVER_BASE_URL` and
`TECHNITIUM_API_TOKEN`.

## Proof for the checkpoint

```bash
python3 scripts/hub_tls.py --check --json          # ok: true, certificate.not_after set
systemctl list-timers noc-hub-tls                  # after install.sh --with-hub-tls
```

Green means: `--check` exits 0, the certificate on disk is the host name asked for, and
the timer is scheduled. A browser test is the operator's, not yours.

## Failure notes

- `lego does not support --dns <provider>`: the installed lego is older than the
  provider (`dnsupdate` since v0.3, `technitium` since v4.20).
- ACME refuses with `NXDOMAIN` on `_acme-challenge.<host>`: the zone has no update
  permission for that key, or the name in `DNSUPDATE_NAMESERVER` is not authoritative
  for the zone.
- Caddy serves an old certificate: `--renew` reloads Caddy through `systemctl reload
  caddy`; check `journalctl -u caddy` when that unit does not exist.
