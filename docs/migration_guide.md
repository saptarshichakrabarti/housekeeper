# Migration guide

Back up the database with `housekeeper database backup OUTPUT`, run `housekeeper database migrate --dry-run`, then run `housekeeper database migrate`. The dry run reports current/target version, pending migrations, affected-entry estimate, temporary-space estimate, and the backup recommendation. Migration runs integrity checking first, records cursor progress in `migration_progress`, and can be rerun safely after an interruption.

Validate with `housekeeper database integrity-check`, `housekeeper database stats`, and optionally `housekeeper database checkpoint --mode FULL`. `housekeeper database vacuum --yes` is deliberately explicit because it can require roughly another database-sized temporary allocation. Migrations retain v1 tables and verified hash evidence. If a migration fails, preserve the original and restore the backup copy; never operate on a damaged database.
