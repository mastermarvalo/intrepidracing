-- Register the `driver_registered` transaction kind used by the
-- `/market-admin driver` enrolment commands (add, sync, sync-all).
-- `contract_ledger.kind` has a FK to `transaction_kinds(code)`, so
-- without this row `driver_ops.enrol_driver` would fail on the ledger
-- insert. The row is global (not season-scoped), so a single migration
-- covers both fresh installs and already-deployed databases.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

INSERT INTO transaction_kinds (code, label)
VALUES ('driver_registered', 'Driver registered')
ON CONFLICT (code) DO NOTHING;
