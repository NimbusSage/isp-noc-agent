# Ticket naming conventions — example (all names are fictional)

Copy to `site-pack/ticket-context/naming-conventions.md` and replace with your org's
real convention. `ticket-context` reads this file to tell a fiber network from a tower
site and to decode host abbreviations. If it is absent the agent uses the default
pattern in the skill and asks the operator to confirm. The best way to build the real
file is to let the agent derive it from live inventory (hostnames, `/ppp active print`,
ticket subjects) and then correct it.

## Fiber networks (named after places)

Alpha, Bravo, Charlie, Delta, Echo, Foxtrot

## Tower sites (named after the tower host)

If the host is John Smith, his towers are named in this order:

| Tower # | Site name |
|---|---|
| 1 | `Smith` |
| 2 | `JSmith` |
| 3 | `John Smith` |
| 4 | `John Smith Two` |
| 5 | `John Smith Three` |

## Abbreviations

`[FirstInitial][LastName]`, e.g. `JSmith` = J. Smith. The initial alone does not identify the
host: check the ticket comments for `TH:` / `Tower Host:` before choosing between
`John Smith` and `Jane Smith`.

## Subject patterns

- `[FiberNetwork] tower_site pppoe - description`
- `(FiberNetwork) tower_site pppoe - description`
- `FiberNetwork tower_site - description`
