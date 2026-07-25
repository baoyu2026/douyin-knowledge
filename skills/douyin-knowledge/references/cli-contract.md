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
<invoke> plan --limit 1 --json
<invoke> login --confirm --json
<invoke> model install --name small --confirm --json
<invoke> sync --confirm --json
<invoke> canary --limit 1 --no-publish --confirm --json
<invoke> run --job-ref <ref> --stop-after packet --confirm --json
<invoke> packet export --job-ref <ref> --json
<invoke> candidate import --job-ref <ref> --input <file> --json
<invoke> candidate repair-contract --job-ref <ref> --json
<invoke> review list --job-ref <ref> --json
<invoke> review record --job-ref <ref> --decision <approve|reject> --json
<invoke> publish --job-ref <ref> --confirm --json
<invoke> reconcile --job-ref <ref> --json
```

`run` stops at `download`, `analysis`, `packet`, or `staging`. It never invokes an AI
model and never publishes. `canary` is fixed at one item and stops at `packet`.

## Confirmation Message

Before a gated command, state:

1. The stable job or collection scope.
2. External calls and whether a browser opens.
3. Expected local CPU/model work and rough duration.
4. Private directories or Library/Vault targets written.
5. The stop point and deterministic checks.

Obtain a new confirmation for publication even if analysis was already confirmed.
An operation confirmation is not a content review decision. Never run `review record`
until the user has inspected the current draft and explicitly said `approve` or
`reject` for it.
