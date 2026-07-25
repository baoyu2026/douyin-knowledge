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

```text
douyin-knowledge --root <root> init --json
douyin-knowledge --root <root> doctor --json
douyin-knowledge --root <root> status --json
douyin-knowledge --root <root> plan --limit 1 --json
douyin-knowledge --root <root> login --confirm --json
douyin-knowledge --root <root> model install --name small --confirm --json
douyin-knowledge --root <root> sync --confirm --json
douyin-knowledge --root <root> canary --limit 1 --no-publish --confirm --json
douyin-knowledge --root <root> run --job-ref <ref> --stop-after packet --confirm --json
douyin-knowledge --root <root> packet export --job-ref <ref> --json
douyin-knowledge --root <root> candidate import --job-ref <ref> --input <file> --json
douyin-knowledge --root <root> candidate repair-contract --job-ref <ref> --json
douyin-knowledge --root <root> review list --job-ref <ref> --json
douyin-knowledge --root <root> review record --job-ref <ref> --decision approve --json
douyin-knowledge --root <root> publish --job-ref <ref> --confirm --json
douyin-knowledge --root <root> reconcile --job-ref <ref> --json
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
