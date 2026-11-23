# Design

- **Compare, don't rebuild.** The stored view text is compared (whitespace-normalized) with the current definition. Equal means no drop and no create, so there is no window in which the view is missing. Different means the old drop and rebuild, now with `DROP VIEW IF EXISTS`.
- **Tolerate a lost race on create.** An "already exists" error is ignored only when the stored view is then the current definition; a stale view left by someone else still raises.
- **Left as is.** Migrations still run on every connection (they are idempotent and return early); only the view handling changed.
