# Semantic Worker Protocol

## Inputs

Treat one packet as an indivisible worker assignment. The same worker must perform
the complete manifest/chunk traversal, visual inspection, candidate generation, and
atomic write. Do not delegate the remaining frames or output step to another worker,
because conversational handoff is not evidence that the receiving worker preserved
the complete ordered inventory, schema handle, output handle, and packet hash.

Use the relative handles returned by `packet export`:

- `content-packet.json`: bounded, sanitized summary packet.
- `candidate.schema.json`: the complete candidate contract.
- `evidence-manifest.json`: counts, hashes, and ordered inventory for the complete
  sanitized evidence bundle.
- Every path in `evidence_chunk_handles`: complete sanitized ASR, OCR, timeline, and
  summary records split into bounded JSON chunks.
- Every path in `visual_handles`: the complete ordered inventory of keyframes already
  selected by local analysis (currently at most 40), all of which must be inspected
  as images.
- `worker-instructions.md`: job-specific immutable instructions.

Read the manifest first, then read every evidence chunk in returned order. Check the
job reference, chunk indexes/count, and manifest inventory; do not silently skip a
chunk because the summary packet appears sufficient. The chunks, not the bounded
summary packet alone, are the complete sanitized textual evidence.

Verify `complete_visual_inventory=true` and that the visual count matches
`required_visual_inspection_count`. Open every visual handle with an image-capable
tool and actually inspect its pixels before writing `visual_evidence`. Do not infer
visual claims from OCR, filenames, timestamps, or text summaries. If the host cannot
view every image, stop before generating a candidate, omit no required field, and
report `candidate-only` plus a visual capability gap to the orchestrator. It must not
fabricate or generate `visual_evidence`.

The visual inventory is complete input, not the publication selection. Use its stable
`frame_index` mapping to choose 3 to 8 supported conclusions for the candidate's
`visual_evidence`; the results archive and Obsidian publish only those referenced frames.

Read no other job, database, Cookie, log, results archive, or orchestration file. Do not use
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

Before writing the candidate:

- Use 2 to 8 unique, specific tags. Do not use generic or placeholder tags.
- Register every number that appears anywhere in publishable content in
  `numeric_review`, with evidence and a verdict. When the content contains no
  numbers, use exactly one `not_applicable` row rather than inventing a number.
- Keep uncertainty consistent: every `unresolved` noun or number must have a
  corresponding `pending_review` item, and any pending item requires
  `review_status=needs_review`; otherwise use `review_status=verified`.
- Exclude privacy-triggering values and fields, including URLs, cookies,
  signatures, request metadata, raw platform IDs, JobId values, credentials, and
  absolute or internal paths.

## Import and Repair

Run `candidate import`. Treat its result as authoritative. When import rejects the
candidate, run `candidate repair-contract`.

- If `repairable=false`, discard the candidate and regenerate from the current
  packet and schema.
- If `repairable=true`, permit one worker attempt restricted to the contract's
  editable fields. Fix the reported error and revalidate every
  `required_content_invariants` entry before writing; deterministic validation may
  report only the first failing gate, so a one-field patch is insufficient evidence
  that the bounded repair is complete.
- Never alter provenance fields or perform a second repair attempt.
- Stop after two failures for the same stage and preserve all checkpoints.

## Publication and Correction

After successful import, treat the candidate as staged. When the user's scoped request
already authorized the fixed job through publication to configured destinations, do
not request another mechanical confirmation. Do not pause for draft approval and do
not count candidate import as completion. Publish to the results archive and Obsidian,
reconcile the sealed targets, and require `accepted` before reporting the job complete.

The published Obsidian note starts with `review_status: unreviewed`; its independent
`evidence_status` reflects the candidate's evidence checks. If the user later reports
a problem from Obsidian, treat that report as correction intent without asking for a
separate approve/reject step. Use the current packet to produce one corrected candidate,
preserve the old publication through backups/journal, republish the same job, and
reconcile again. Never expose raw packet evidence or private source material.
