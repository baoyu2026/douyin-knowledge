# Safety and Recovery

## Failure Handling

For an interrupted `migrate results`, keep both the legacy `library/` and configured
results archive in place, then rerun the same confirmed command. Verified copies are
reused. A `results_migration_conflict` means the same entry differs between roots;
do not overwrite either copy until the difference is resolved. Migration never
changes accepted/completed state.

For an interrupted `migrate cleanup`, rerun the same confirmed cleanup command. The
cleanup checkpoint identifies whether the legacy root was still in place, atomically
staged, or already deleted. Do not move or delete the staging directory manually.
Cleanup is permitted only after every authoritative destination and registry binding
has been reverified; it never deletes the migration checkpoint or publication journal.

For an interrupted fixed batch, run `batch status` or `batch resume` with the same
`batch_ref`. Resume only the recommended original `job_ref`; never replace an item
because favorites were added or removed after batch creation. A batch is complete
only when every fixed job's latest publication is `accepted` and all targets are
verified.

For an interrupted semantic handoff, keep its directory and cleanup token. Retry
`handoff ingest` after correcting an atomic candidate, or retry `handoff cleanup`
with the same token. Do not manually remove bundle files or edit the private
assignment registry. A partial cleanup is designed to resume safely.

For `results_migration_entry_incomplete`, do not retry the same migration blindly.
Run read-only `migrate inspect` and use only its issue counts to determine whether a
legacy-format compatibility repair is required. Entries reported as `repairable`
may be migrated normally; the CLI generates their missing manifests only in the new
copy. Stop for any entry reported as `blocked`.

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
- `superseded`: a historical accepted publication retained for audit after a newer
  accepted correction replaced it. Its recorded target state is not refreshed.

Never update `completed` directly. Reconcile partial writes before retrying publish.
Reuse the same idempotency key for the same request; a changed request needs a new key.
Human inspection is post-publication and optional for state progression. An imported
candidate, a written but unverified note, or a review record is never completion
evidence; only `accepted` is.

A user correction request authorizes creating and importing the corrected candidate.
Before writing a correction to the results archive or Obsidian, describe its publication
scope. A direct request to complete that correction is sufficient authorization when
the job and configured destinations are unchanged; do not require a second mechanical
confirmation.

## Credential and Privacy Rules

Keep Cookie files under `<root>/config` with private ACLs. Never read Cookie values into
the model context. Do not print or paste raw source IDs, request URLs, signatures,
absolute paths, private logs, reviewer notes, source media, or quarantine contents.

Do not commit `config`, `data`, `library`, `vault`, `logs`, `output`, `orchestration`,
`quarantine`, model files, databases, media, or generated candidates.
