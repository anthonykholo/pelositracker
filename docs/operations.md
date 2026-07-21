# Operations

Use one container and one worker. PostgreSQL is the production durability path;
SQLite is intended for local research and must be placed on persistent storage
if records need to survive redeploys.

- Liveness: `GET /api/health`.
- Readiness: `GET /api/ready` checks initialized repositories and native engine.
- Authenticated diagnostics: `GET /api/runtime` exposes provider counters,
  reconnects, quota headers, feed groups, and pending notifications.
- Polling: `ODDS_POLL_SECONDS=45` by default; keep it below the accepted maximum
  data age while respecting the provider quota.
- Models: `ENABLE_INDEPENDENT_MODELS=true` is inert unless
  `INDEPENDENT_MODEL_ARTIFACT` points to a valid reviewed exact-segment
  registry. Invalid artifacts abort startup; no artifact ships.
- Shutdown: provider groups are canceled/awaited, notifications are drained,
  and repositories are closed.

Back up the database before deploy. Migrations are forward-only, component
scoped, transactional, checksummed, and idempotent. A checksum mismatch aborts
startup rather than silently rewriting history. Roll back application code to
the pre-deploy branch/commit; do not reverse or delete recorded migration rows.
