# Host Adapters

## Release Capability Matrix

| Host | Release status | Allowed claim |
| --- | --- | --- |
| Codex on Windows with PowerShell and filesystem tools | Verified | Full workflow through the installed adapter and scoped authorization |
| Generic local MCP `stdio` gateway | Protocol smoke-tested | Candidate-only transport; each consuming host must still pass the capability gate |
| OpenClaw | Not release-verified | Do not claim end-to-end support; permit candidate-only use only after an isolation and atomic-output smoke test passes |
| WorkBuddy and other hosts | Not release-verified | Candidate-only or unsupported until an executed smoke test passes |

"Candidate-only" means the host may transform one exported, sanitized evidence
bundle into one candidate JSON object only when it can satisfy every field's evidence
requirements. It does not mean the host can log in, download, mutate SQLite, approve
content, or publish. A text-only host has a visual capability gap and cannot produce
a valid candidate that requires `visual_evidence`.

## Capability Gate

A full host must be able to:

1. Execute this Skill's `scripts/invoke.ps1` and capture one complete stdout JSON object.
2. Read the evidence manifest and every returned evidence chunk in order without
   truncating or silently skipping the complete sanitized evidence.
3. Verify the complete visual inventory count, then open every returned visual handle
   with an image-capable tool and inspect the image pixels before selecting 3 to 8
   `visual_evidence` conclusions.
4. Read no unlisted private artifact and perform no network enrichment.
5. Write one pure UTF-8 JSON candidate atomically at the returned output handle.
6. Run candidate import and rely on its result rather than conversational output.
7. Bind every mutation to clear user authorization and require accepted publication
   before reporting completion. A single end-to-end request may authorize the fixed
   analysis and publication scope; do not invent phrase-matching or pre-publication
   review gates.
8. Keep runtime bindings, credentials, raw evidence, and private paths out of model context.

## Codex

Install with the bundled `install-skill.ps1`, restart Codex so discovery metadata is
reloaded, and use `scripts/invoke.ps1` from the installed Skill. Shell output is not
completion evidence; parse the CLI envelope and honor its error fields.

For a delegated worker, use `handoff materialize` with a dedicated empty directory
outside the private instance. Give the worker only that directory, ingest its atomic
candidate with `handoff ingest`, then remove the verified bundle with `handoff cleanup`
and the returned token. Never copy source media, credentials, logs, the database, or
another job into the handoff manually. Do not assign the same job twice.

## Generic MCP Gateway

Read [agent-gateway.md](agent-gateway.md). The installed
`scripts/invoke-mcp.ps1` starts a local `stdio` server without putting the bound
private path into model context. The gateway is candidate-only in this release. It
tracks manifest, text, and visual retrieval; refuses premature candidate submission;
atomically writes the candidate; and relies on normal handoff ingest for deterministic
validation.

MCP support by itself does not certify a host. Run the capability gate with a
disposable fixture before assigning real evidence. Do not use this gateway to claim
that the host can log in, analyze, publish, or reconcile.

## OpenClaw, WorkBuddy, and Other Hosts

The release includes a WorkBuddy-specific upload Skill and a local exporter that
generates `douyin-knowledge.zip` plus a machine-local MCP import JSON. This is an
installation surface, not host certification. Upload the ZIP in WorkBuddy's Skill UI
and import the JSON in its MCP UI; never share the generated MCP JSON across machines
because it binds that machine's launcher path.

No OpenClaw installation path or WorkBuddy end-to-end orchestration is certified in
this release. Before candidate-only use, execute a disposable smoke test proving JSON
capture, complete chunk traversal, real image viewing, task-directory isolation,
atomic output, and return of control to a capable orchestrator. A host that fails the
image test must report a candidate-only visual capability gap and stop before writing
a candidate. Until these checks pass, report the host as unsupported rather than
improvising.
