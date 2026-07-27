# Agent Gateway

## Scope

The release includes a host-neutral, candidate-only Agent Gateway with an MCP
`stdio` transport. The gateway does not replace the JSON CLI. It invokes the CLI as
the authority, stores no credential in the model-visible bundle, and exposes only
one isolated semantic assignment at a time.

The first gateway release deliberately does not expose login, sync, local analysis,
publication, migration, or cleanup of historical results. A capable orchestrator
must prepare the selected job through `packet` first and must publish and reconcile
an ingested candidate through the normal CLI workflow.

## Start the MCP Server

Install the package and Skill normally. Configure an MCP-capable host to start the
installed Skill adapter over `stdio`:

```json
{
  "mcpServers": {
    "douyin-knowledge": {
      "command": "powershell",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "<installed-skill>/scripts/invoke-mcp.ps1"
      ]
    }
  }
}
```

Keep the absolute adapter path in the host's local configuration, not in prompts or
conversation output. The adapter resolves the installed gateway and bound private
instance through `runtime.local.json`; the model must never read that file.

For WorkBuddy, prefer `scripts/export-workbuddy-bundle.ps1` over manual JSON editing.
It creates an uploadable Skill ZIP and a machine-local MCP import JSON. Generate the
JSON independently on each computer; only the Skill ZIP is portable across users.

The server supports only local `stdio` in this release. Do not expose it through a
network listener or add arbitrary shell and filesystem tools around it.

## Tool Contract

| MCP tool | Mutation | Purpose |
| --- | --- | --- |
| `douyin_capabilities` | No | Negotiate protocol, mode, features, and limits |
| `douyin_doctor` | No | Return safe local capability checks |
| `douyin_status` | No | Return safe collection and resource state |
| `douyin_plan` | No | Select stable job references without changing state |
| `douyin_prepare_handoff` | Yes, confirmed | Materialize one isolated assignment for a packet-ready job |
| `douyin_get_manifest` | Receipt only | Return the immutable assignment inventory |
| `douyin_read_text` | Receipt only | Read one verified packet, schema, instruction, or evidence chunk |
| `douyin_open_visual` | Receipt only | Return one verified keyframe as MCP image content |
| `douyin_assignment_status` | No | Report unread text and visual handles |
| `douyin_submit_candidate` | Yes | Atomically write and deterministically ingest one candidate |
| `douyin_cleanup_assignment` | Yes, confirmed | Remove the verified handoff and release its slot |

All JSON tools return the normal seven-field CLI envelope. The image tool returns
real MCP image content. No tool returns an absolute path, cleanup token, Cookie,
platform URL, raw platform ID, database row, or private log.

## Candidate-only Workflow

1. Call `douyin_capabilities` and require `mode=candidate-only`.
2. Use a capable local orchestrator to prepare one fixed `job_ref` through packet.
3. Call `douyin_prepare_handoff` with that exact reference and scoped confirmation.
4. Call `douyin_get_manifest`.
5. Call `douyin_read_text` for every non-visual manifest handle. Read evidence chunks
   in the order given by `evidence-manifest.json`.
6. Call `douyin_open_visual` for every visual handle and inspect the pixels.
7. Call `douyin_assignment_status` and require both missing-handle arrays to be empty.
8. Generate one candidate that matches `candidate.schema.json`, then call
   `douyin_submit_candidate`. Conversational JSON is not a submission.
9. Treat the returned deterministic ingest envelope as authoritative.
10. Call `douyin_cleanup_assignment` after successful ingest or explicit abandonment.
11. Return control to the capable orchestrator for publication and reconciliation.

The gateway records resource retrieval so an incomplete traversal cannot submit a
candidate. Retrieval is not proof of comprehension; every host still requires the
visual and isolation smoke test in `host-adapters.md`.

## Portability

MCP is a transport adapter, not the business implementation. The reusable boundary
is the versioned JSON CLI, isolated handoff bundle, candidate schema, and deterministic
ingest contract. A host without MCP may use the documented file handoff directly.
Future transports must preserve the same opaque handles, evidence completeness,
atomic candidate write, scoped authorization, and cleanup guarantees.
