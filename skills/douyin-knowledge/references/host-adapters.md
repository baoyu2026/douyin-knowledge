# Host Adapters

## Release Capability Matrix

| Host | Release status | Allowed claim |
| --- | --- | --- |
| Codex on Windows with PowerShell and filesystem tools | Verified | Full workflow through the installed adapter, with human confirmation gates |
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

## OpenClaw and Other Hosts

No OpenClaw installation path or end-to-end orchestration is certified in this
release. Before candidate-only use, execute a disposable smoke test proving JSON
capture, complete chunk traversal, real image viewing, task-directory isolation,
atomic output, and return of control to a capable orchestrator. A host that fails the
image test must report a candidate-only visual capability gap and stop before writing
a candidate. Until these checks pass, report the host as unsupported rather than
improvising.
