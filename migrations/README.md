# Database migrations

Runtime migrations are immutable component/version/checksum units recorded in
`schema_migrations`. Existing SQLite and PostgreSQL databases are upgraded in
place; migration checksum drift aborts startup.

The SQL snapshots in the dialect directories document the cross-store ledger.
Compatibility column additions are performed through database introspection so
the same application release can upgrade databases created by pre-ledger builds.

Milestone E advances the ledger component to v5 and accounts to v2. Ledger v5
adds calibrated probability, uncertainty bounds, positive-net-EV probability,
net EV, consensus/sample metadata, serialized gate results, and requested/fill
economics. Accounts v2 adds sport, transparent correlation group, and decision
lineage. Existing rows remain nullable/unknown; no historical value is
reinterpreted.

Milestone F advances the ledger to v6. It adds nullable independent-model
probability, model/calibration versions and hashes, test sample/event counts,
and registry lineage to decision marks and eligible paper-fill rows. Historical
rows remain explicitly unknown and are not backfilled from later model output.
