# Safety and Recovery

## Failure Handling

For an interrupted `migrate results`, keep both the legacy `library/` and configured
results archive in place, then rerun the same confirmed command. Verified copies are
reused. A `results_migration_conflict` means the same entry differs between roots;
do not overwrite either copy until the difference is resolved. Migration never
changes accepted/completed state.

Use `status`, then `reconcile --job-ref <ref>`. Resume only the same stable `job_ref`.
Do not delete checkpoints, quarantine, publication intents, backups, or accepted
artifacts to make a retry appear clean.

After a host timeout, poll `status` no more often than every 30 to 60 seconds. An
unchanged `active_job_ref` means wait; do not launch a second analysis or delete its
lock. If it remains after the two-hour analysis window, retry only that same job and
the same previously confirmed stop point, and let the CLI reject an active lease or
quarantine a stale one before resuming from the checkpoint. If the original stop
point or start time is unknown, keep monitoring or ask the user; never infer a later
stop point or assume the lease is stale. Run `reconcile` only when publication was
attempted or interrupted, not for an analysis-only timeout.

After one failure, follow `error.user_action` and retry only if `retryable=true` or the
stated prerequisite was corrected. After the same failure twice, stop and report the
preserved checkpoint.

After the user fixes that prerequisite and explicitly confirms another attempt, pass
`--retry-after-fix` to `run`. This resets only the repeated-failure budget; it does not
skip the failed stage, replace the selected `job_ref`, or bypass validation.

## Publication States

- `intent`: publication is planned, missing, mismatched, or not fully observed.
- `published_unaccepted`: every sealed target hash is verified; registry remains
  `analyzed`.
- `accepted`: SQLite integrity, privacy, and content checks passed; registry may now
  be `completed`.

Never update `completed` directly. Reconcile partial writes before retrying publish.
Reuse the same idempotency key for the same request; a changed request needs a new key.
Human inspection is post-publication and optional for state progression. An imported
candidate, a written but unverified note, or a review record is never completion
evidence; only `accepted` is.

A user correction request authorizes creating and importing the corrected candidate.
Before writing the correction to the results archive or Obsidian, describe the publication scope
again and obtain a new explicit publication confirmation.

## Credential and Privacy Rules

Keep Cookie files under `<root>/config` with private ACLs. Never read Cookie values into
the model context. Do not print or paste raw source IDs, request URLs, signatures,
absolute paths, private logs, reviewer notes, source media, or quarantine contents.

Do not commit `config`, `data`, `library`, `vault`, `logs`, `output`, `orchestration`,
`quarantine`, model files, databases, media, or generated candidates.
