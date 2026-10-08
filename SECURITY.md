# Security model

This is a local trading research project. A public repository does not imply
that its running services are safe to expose to the internet. Offline tests
use mocked broker clients; reviews and development must never place real orders.

## Credentials and local services

Use three independent, randomly generated keys of at least 32 characters:
`TV_WEBHOOK_SECRET`, `RISK_SERVER_API_KEY`, and `RISK_ADMIN_API_KEY`. The admin
key is used only by an operator for `/set_state` and `/reset_day`, never by a
generator. Blank/placeholder keys fail closed. Do not reuse production keys in
test fixtures or CI. Do not pass credentials on command lines or in query URLs.
Keep real IBKR account identifiers in ignored `.env` files.

Only loopback clients and loopback Host headers are accepted. Bind both servers
to `127.0.0.1`; use one worker because order locks and deduplication state are
in memory. Do not expose the services with a tunnel, reverse proxy, port forward
or a publicly reachable TWS API. Local proxy connections also look local to the
application; the middleware is not an internet deployment boundary. There is
no internet-grade rate limiting or distributed/durable replay ledger.

The risk key can submit orders and read account state. Treat it as highly
privileged. Administrative writes require a separate admin key. Public health
returns only liveness; API schema/docs are disabled. Bodies are bounded while
streaming and have a read timeout. Only registered feed subscriptions allocate
bar history. Entry IDs cannot use the reserved close-order prefix. Exit signals
also require fresh timestamps. Internal HTTP clients ignore proxy environment
variables; outbound routing requires HTTPS and does not follow redirects.

HTTP client URL logging is disabled because router URLs embed secrets. Decision
logs use an allowlist and do not store arbitrary extras or request headers.
Logs still contain sensitive account state and trade details: keep them private.
Standalone HTML reports escape text but are also excluded from publication.

## Before making a public repository

1. Publish reviewed source files, not an unfiltered archive of the local folder.
2. Exclude `.env` variants, private keys, account exports, logs, caches, virtual
   environments, backups, generated reports and market-data CSVs. `.gitignore`
   does not remove already-tracked files or earlier commits.
3. Run `python tools/publication_guard.py --tree` on a candidate copy. After Git
   is initialized, run it without `--tree` to scan the index and reachable history.
   It reports locations/types and redacts values. Its patterns are a safeguard,
   not a complete secret detector. Also enable GitHub secret scanning and push
   protection where available and review all staged diffs manually.
   Optionally enable the supplied local hook with `git config core.hooksPath .githooks`.
4. Use `shared/requirements.lock` with `--require-hashes --only-binary=:all:`.
   The lock records exact tested versions and public PyPI wheel hashes. Audit
   it regularly and regenerate deliberately when applying dependency updates.
   The editable requirements file expresses dependency ranges, not a lock.
5. Require the supplied CI checks before merges. The workflow has read-only
   repository permissions, pins action commits and does not persist Git auth.
   It never receives broker credentials or starts trading services. The workflow
   has not been exercised on GitHub until the repository is actually created.
6. Verify rights to redistribute historical market data and choose your code
   license before publishing. Do not treat a clean secret scan as legal approval.

## If a secret is exposed

Revoke/rotate it with its provider immediately, including token-bearing webhook
URLs. Removing the file or rewriting history does not invalidate a leaked key.
Then remove it from the index, accessible history, release artifacts and logs.
Review provider access logs. Never paste the secret into a public issue.

## Limits of this review

Static review and tests cannot establish that a project is "airtight." They do
not certify the host OS, TWS configuration, future dependencies, repository
settings, remote clones, inaccessible history or the absence of every possible
secret. Dependency audits cover published advisories at the time of the check;
they do not prove that packages are free of malware or undiscovered bugs.
