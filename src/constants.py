"""Shared-product constants.

`DEFAULT_API_KEY` is the magic-string handshake between this manager
and the BarHandler / FitStudio web apps. It's **not a secret** —
it's checked into the public source tree on both sides, and anyone
who reads the repo can pull it out. Its job is to keep random other
software on the same host from accidentally driving the printer or
cash drawer: a tool that doesn't know to send this header in
`X-Api-Key` gets 401 from every protected route.

The matching constant lives in
`bar-handler-app/src/app/constants/urls.ts::BARHANDLER_MANAGER_API_KEY`.
If you need to rotate it (you almost certainly don't), bump both
ends in lockstep and ship a coordinated release.

Operators can still override on a per-install basis by setting
`server.api_key` in `config.yaml` — useful when a single host runs
multiple isolated POS apps that shouldn't share keys.
"""

DEFAULT_API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"
