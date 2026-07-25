# Safety and Recovery

## Failure Handling

Use `status`, then `reconcile --job-ref <ref>`. Resume only the same stable `job_ref`.
Do not delete checkpoints, quarantine, publication intents, backups, or accepted
artifacts to make a retry appear clean.

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

## Credential and Privacy Rules

Keep Cookie files under `<root>/config` with private ACLs. Never read Cookie values into
the model context. Do not print or paste raw source IDs, request URLs, signatures,
absolute paths, private logs, reviewer notes, source media, or quarantine contents.

Do not commit `config`, `data`, `library`, `vault`, `logs`, `output`, `orchestration`,
`quarantine`, model files, databases, media, or generated candidates.
