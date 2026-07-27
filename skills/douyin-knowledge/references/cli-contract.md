# CLI Contract

## Envelope

Every command prints one JSON object to stdout:

```json
{
  "schema_version": 1,
  "ok": true,
  "operation": "status",
  "data": {},
  "error": null,
  "warnings": [],
  "safe_summary": "..."
}
```

On failure, read `error.code`, `retryable`, `preserved_checkpoint`, and
`user_action`. Do not surface private stderr or reconstruct omitted data.

## Commands

Let `<invoke>` mean executing this Skill's `scripts/invoke.ps1` with PowerShell. Use
that adapter for every command; it resolves the installed CLI and bound private
instance without putting either absolute path in conversational output.

```text
<invoke> init --json
<invoke> doctor --json
<invoke> status --json
<invoke> configure results --path <absolute-folder> --confirm --json
<invoke> migrate inspect --json
<invoke> migrate results --confirm --json
<invoke> migrate cleanup --confirm --json
<invoke> plan --limit 1 [--status <new|failed|incomplete|downloaded|analyzed>] --json
<invoke> batch create --job-ref <ref> [--job-ref <ref> ...] [--status <status>] --confirm --json
<invoke> batch status --batch-ref <ref> --json
<invoke> batch resume --batch-ref <ref> --json
<invoke> login --confirm --json
<invoke> model install --name small --confirm --json
<invoke> sync --confirm --json
<invoke> canary --limit 1 [--status <new|failed|incomplete|downloaded|analyzed>] --no-publish --confirm --json
<invoke> run --job-ref <ref> --stop-after packet --confirm --json
<invoke> packet export --job-ref <ref> --json
<invoke> handoff materialize --job-ref <ref> --directory <empty-external-dir> --confirm --json
<invoke> handoff ingest --job-ref <ref> --directory <external-dir> --json
<invoke> handoff repair-contract --job-ref <ref> --directory <external-dir> --json
<invoke> handoff cleanup --job-ref <ref> --directory <external-dir> --token <token> --confirm --json
<invoke> candidate import --job-ref <ref> --input <file> --json
<invoke> candidate repair-contract --job-ref <ref> --json
# Optional post-publication audit/correction records:
<invoke> review list --job-ref <ref> --json
<invoke> review record --job-ref <ref> --decision <approve|reject> --json
<invoke> publish --job-ref <ref> [--idempotency-key <stable-key>] --confirm --json
<invoke> reconcile --job-ref <ref> --json
```

`init` creates `config/results.yml` with no selected root. Before the first publish,
require the user to choose the human-readable archive folder and confirm the
configuration write. `configure results` creates or verifies that folder, stores the
private binding, and returns only safe booleans and policy names. Do not echo the
absolute path from the command or read the configuration file into model context.

The archive layout is stable and intended for browsing:

```text
<results-root>/
  00-总索引/
  <主分类>/
    <标题>/
      内容整理.md
      原视频.mp4
      资料信息.yml
      附件/完整时间轴.md
      精选关键帧/
```

Chinese characters and spaces are preserved after Windows path sanitization. A
different video with the same category and title receives `标题 (2)`, then `(3)`.
An existing job keeps its established directory on correction so links remain
stable. Once journaled `results/` targets exist, the CLI refuses to rebind the root.

`migrate results` handles the one-time transition from the private instance's legacy
`library/` into the configured archive. It copies only complete human-readable
entries, verifies directory hashes, rebuilds `00-总索引`, updates the registry for
future corrections, and preserves the legacy source and old publication journal.
It is resumable and idempotent: a verified existing copy is reused, while a changed
same-entry target produces a controlled conflict instead of being overwritten. The
command returns only counts, booleans, and a relative state handle. Migration does
not change collection or publication status and is not a general relocation command
for an already journaled `results/` archive.

If migration reports an incomplete legacy entry, run `migrate inspect` before any
retry. It is read-only and returns only complete/incomplete counts plus issue-type
counts; it never returns titles, paths, entry references, or media content. A
`repairable` entry has every content artifact, lacks the manifest introduced by
newer releases, and maps uniquely to a legacy database record by its registered path
or source-video SHA-256. A duplicate fingerprint is never guessed. A repairable
entry is migration-ready: the manifest is generated only in the verified destination
copy. A `blocked` entry is not migration-ready.

When multiple legacy directories resolve to one stable entry, the uniquely
registered legacy path is authoritative and media-fingerprint-only duplicates remain
preserved under the legacy root; report them as `duplicates_skipped`. If there is no
single authoritative source, inspection reports a blocking duplicate-reference
conflict and migration must stop.

`migrate cleanup` is an optional irreversible follow-up. Run it only after migration
has completed and the user separately confirms deletion. It revalidates every
authoritative source, destination hash, generated manifest, checkpoint record, and
registry binding before atomically moving the complete legacy root into a private
deletion stage. It then deletes the migrated sources, legacy indexes, and stale
duplicates, while preserving the configured results archive, migration checkpoint,
and historical publication journal. The cleanup checkpoint makes an interrupted
delete resumable. Never substitute a manual recursive delete.

`run` stops at `download`, `analysis`, `packet`, or `staging`. It never invokes an AI
model and never publishes. `canary` is fixed at one item and stops at `packet`.
Use the same `--status` value on `plan` and `canary` when the requested workflow
depends on the item's authoritative starting state. In particular, use `new` instead
of accepting an analyzed collection item. This filter does not assert that media or
analysis files are absent: require `download_reused=false` and
`analysis_reused=false` in the run result before claiming a cold run.

`batch create` records exactly one to five supplied job references. More than one
requires a successful canary from the installed version. `batch status` and
`batch resume` are read-only reconciliation views: they report stages, timings,
verified completion, resource capacity, and the next single-job actions. Execute
those actions through the documented `run`, `handoff`, and `publish` commands. Never
use a dynamic next-item loop or replace a fixed job after a later collection sync.

`status.resources` reports the global one-CPU, two-semantic-slot, and one-publisher
capacity. `publication_drift_count` reports drift already recorded in the journal;
run `reconcile` to perform a fresh target check.

The CLI permits local analysis to run for up to two hours. The host invocation must
also allow at least two hours, or use a session that can yield and later resume. A
host timeout does not prove that the child process stopped. Run `status` first: if
the same `active_job_ref` is present, poll no more often than every 30 to 60 seconds
and never launch a duplicate analysis. Do not delete a run lock manually. If it is
still active after the two-hour analysis window, invoke `run` only for that same
`job_ref` and the same previously confirmed stop point; the CLI will reject an active
lease or quarantine a stale one, then reuse its checkpoint and completed stages. If
the original stop point or start time is unknown, do not infer a later scope or call
the lease stale; keep monitoring or ask before a newly confirmed retry. Use
`reconcile` after an interrupted publication, not after an analysis-only timeout.

`packet export` returns every keyframe already selected by local analysis in
`visual_handles` (currently at most 40). The evidence manifest reports the complete
visual count and requires the worker to inspect every handle. This full input does not
change the candidate or publication limit: `visual_evidence` selects 3 to 8 conclusions,
and only those referenced frames are copied to the results archive and Obsidian. New
candidates map every selected frame to an argument step so the image is embedded beside the
claim it supports; legacy candidates without placement retain the compact gallery fallback.

Use the `handoff` commands for delegated semantic work. `materialize` accepts
only a new or empty directory outside the private instance, copies the sanitized
packet atomically, and returns a cleanup token without storing that token in the
bundle. Give the worker only that directory. `ingest` verifies the exact file list,
hashes, current packet, and candidate before using the normal deterministic import
gates. After a repairable rejection, run `candidate repair-contract`, then `handoff
repair-contract` to attach that bounded contract to the existing hash-protected
handoff manifest. The same worker may edit only `content` once and must revalidate
every listed invariant before the second ingest attempt. `cleanup` removes only the verified handoff and is retryable with the same
token after an interrupted partial cleanup. At most two unfinished handoffs and only
one unfinished handoff per job are allowed.

## Scoped Authorization

Before starting an operational workflow, state once:

1. The stable job or collection scope.
2. External calls and whether a browser opens.
3. Expected local CPU/model work and rough duration.
4. Private directories or human-readable results/Vault targets written.
5. The stop point and deterministic checks.

Do not require a magic confirmation phrase. A direct imperative request is valid
authorization when its scope is clear. If the user asks to run a fixed job or bounded
batch end to end, the same authorization covers analysis and publication described
above when the destinations were already selected or used by the user; do not stop
at an artificial publication checkpoint. The CLI still requires
`--confirm` on each gated command so the adapter can prove that the host deliberately
invoked it.

Ask again only if the scope or destination changes, the original request was
read-only or ambiguous, login/CAPTCHA interaction is newly required, deterministic
validation reports an unresolved overwrite/conflict, or an irreversible cleanup is
introduced. A newly disclosed archive, Vault, account, or collection always requires
one user response before its first write or login. A request to correct and republish one accepted job is sufficient for
that correction workflow when the write targets are unchanged; a request to inspect
or diagnose is not.
No `approve` record is required before publication. Use `review record` only when the
user explicitly gives that decision for the current published candidate; it is an
optional audit/correction record and never completion evidence.

Omit `--idempotency-key` for a candidate's normal first publication. Reuse the same
explicit key only when retrying the exact same changed publication request. When a
correction, renderer upgrade, or managed-metadata change intentionally produces new
target digests for an already accepted candidate, use a new stable opaque key, keep
the previous journal entry, publish serially, and require the new transaction to reach
`accepted` after reconciliation.
