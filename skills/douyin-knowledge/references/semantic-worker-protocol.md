# Semantic Worker Protocol

## Inputs

Use the relative handles returned by `packet export`:

- `content-packet.json`: bounded, sanitized evidence.
- `candidate.schema.json`: the complete candidate contract.
- `worker-instructions.md`: job-specific immutable instructions.

Read no other job, database, Cookie, log, Library, or orchestration file. Do not use
network retrieval to enrich the evidence.

## Output

Write exactly one UTF-8 JSON object to the returned candidate output handle. Write a
temporary sibling file first, close it, then atomically rename it. Include exactly:

```text
protocol_version, schema_version, job_ref, packet_sha256, content
```

Do not add Markdown fences, commentary, URLs, credentials, absolute paths, raw
platform IDs, or unsupported facts. Preserve the packet hash and job reference
exactly.

## Import and Repair

Run `candidate import`. Treat its result as authoritative. When import rejects the
candidate, run `candidate repair-contract`.

- If `repairable=false`, discard the candidate and regenerate from the current
  packet and schema.
- If `repairable=true`, permit one worker attempt restricted to the contract's
  editable fields.
- Never alter provenance fields or perform a second repair attempt.
- Stop after two failures for the same stage and preserve all checkpoints.
