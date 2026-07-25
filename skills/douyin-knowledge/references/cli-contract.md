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
<invoke> plan --limit 1 --json
<invoke> login --confirm --json
<invoke> model install --name small --confirm --json
<invoke> sync --confirm --json
<invoke> canary --limit 1 --no-publish --confirm --json
<invoke> run --job-ref <ref> --stop-after packet --confirm --json
<invoke> packet export --job-ref <ref> --json
<invoke> candidate import --job-ref <ref> --input <file> --json
<invoke> candidate repair-contract --job-ref <ref> --json
# Optional post-publication audit/correction records:
<invoke> review list --job-ref <ref> --json
<invoke> review record --job-ref <ref> --decision <approve|reject> --json
<invoke> publish --job-ref <ref> --confirm --json
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
stable. Once journaled `results/` targets exist, the CLI refuses to rebind the root;
moving a published archive requires a future migration workflow, not a config edit.

`run` stops at `download`, `analysis`, `packet`, or `staging`. It never invokes an AI
model and never publishes. `canary` is fixed at one item and stops at `packet`.

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
and only those referenced frames are copied to the results archive and Obsidian.

## Confirmation Message

Before a gated command, state:

1. The stable job or collection scope.
2. External calls and whether a browser opens.
3. Expected local CPU/model work and rough duration.
4. Private directories or human-readable results/Vault targets written.
5. The stop point and deterministic checks.

Obtain a new confirmation for publication even if analysis was already confirmed.
The same rule applies after a correction request: correction intent permits candidate
revision but does not authorize another results archive or Obsidian write.
No `approve` record is required before publication. Use `review record` only when the
user explicitly gives that decision for the current published candidate; it is an
optional audit/correction record and never completion evidence.
