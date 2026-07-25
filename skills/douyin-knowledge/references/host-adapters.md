# Host Adapters

## Capability Gate

A full host must be able to:

1. Run the JSON CLI and capture one complete stdout object.
2. Read the returned packet, schema, and instruction handles.
3. Write one pure JSON candidate atomically at the returned output handle.
4. Run candidate import and rely on its result rather than conversational output.
5. Pause for explicit confirmations before gated commands.

## Codex

Load this Skill normally. Use shell commands for the CLI and the filesystem for the
candidate protocol. Keep the CLI as the only state authority.

## OpenClaw

Use an isolated worker with access only to the exported semantic task directory.
Return control to the orchestrator for candidate import, review, and publication.

## WorkBuddy and Other Hosts

Declare candidate-only support until atomic file output and JSON capture are verified.
If the host can only answer in chat, ask it for candidate JSON but let a capable host
write and import the file. Do not claim login, download, SQLite, or publication support
without an executed smoke test.
