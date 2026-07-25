# Semantic Worker Protocol

## Inputs

Use the relative handles returned by `packet export`:

- `content-packet.json`: bounded, sanitized summary packet.
- `candidate.schema.json`: the complete candidate contract.
- `evidence-manifest.json`: counts, hashes, and ordered inventory for the complete
  sanitized evidence bundle.
- Every path in `evidence_chunk_handles`: complete sanitized ASR, OCR, timeline, and
  summary records split into bounded JSON chunks.
- Every path in `visual_handles`: image evidence that must be inspected as images.
- `worker-instructions.md`: job-specific immutable instructions.

Read the manifest first, then read every evidence chunk in returned order. Check the
job reference, chunk indexes/count, and manifest inventory; do not silently skip a
chunk because the summary packet appears sufficient. The chunks, not the bounded
summary packet alone, are the complete sanitized textual evidence.

Open every visual handle with an image-capable tool and actually inspect its pixels
before writing `visual_evidence`. Do not infer visual claims from OCR, filenames,
timestamps, or text summaries. If the host cannot view images, stop before generating
a candidate, omit no required field, and report `candidate-only` plus a visual
capability gap to the orchestrator. It must not fabricate or generate
`visual_evidence`.

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

## Human Review

After a successful import, use its `draft_handle` as the review artifact. Give the
user that relative handle and, only when requested, show a bounded excerpt from the
generated draft. Do not show raw packet evidence or private source material.

Stop and wait for an explicit `approve` or `reject` decision tied to the current
candidate. General permission to continue, approval of a batch plan, or confirmation
of analysis does not satisfy this gate.
