---
name: douyin-knowledge
description: Convert a user's own Douyin favorites into a private, reviewed local knowledge library using the douyin-knowledge JSON CLI, local ASR/OCR, bounded semantic JSON candidates, deterministic validation, Obsidian publication, checkpoints, and reconciliation. Use when the user asks to log in to Douyin, sync favorites or 收藏, process one or a small batch of saved videos, resume a failed item, review generated knowledge, publish to Library/Obsidian, inspect pipeline status, or operate an installed douyin-knowledge release from Codex or another host that has passed the documented capability gate.
---

# Douyin Knowledge

Operate the installed `douyin-knowledge` CLI as the authority. Keep account access,
media, analysis, review, publication state, and credentials in the user's private
instance directory. Never treat an assistant message as completion evidence.

## Start Safely

1. Invoke `scripts/invoke.ps1` from this Skill for every CLI operation. Never assume
   the bare `douyin-knowledge` command is on `PATH`. If the adapter is unavailable,
   read [references/installation-and-configuration.md](references/installation-and-configuration.md).
2. Use the instance root bound by the installer. To change it, rerun the installer
   with the user's explicit path; do not read or expose `runtime.local.json`.
3. Run `init --json`, then `doctor --json` and `status --json` through the adapter.
4. Read [references/cli-contract.md](references/cli-contract.md) before composing
   commands or interpreting errors.
5. Outside explicit human review, report only `safe_summary`, stable `job_ref` values,
   counts, booleans, relative handles, and documented error fields. During review,
   also permit a bounded generated-draft excerpt. Never echo absolute paths, URLs,
   raw platform IDs, cookies, model logs, or reviewer notes.

## Choose the Workflow

- For first use, initialize and diagnose, then explain the login confirmation gate.
- For collection refresh, run confirmed `sync`; it must not download media.
- For one item, use `plan --limit 1`, select that returned `job_ref`, then run a
  confirmed no-publish canary.
- For semantic work, export one packet, let one worker write one pure JSON
  candidate, import it, and check authoritative status. Read
  [references/semantic-worker-protocol.md](references/semantic-worker-protocol.md).
- For human review, provide the returned relative `draft_handle`, optionally show a
  bounded generated-draft excerpt, then stop. Record `approve` or `reject` only after
  the user gives that exact decision for the current candidate. Never infer approval
  from an earlier analysis or batch confirmation.
- For publication, describe the exact job, network/model calls, expected time,
  Library/Vault writes, backup/checkpoint behavior, and validation. Wait for a new,
  explicit confirmation, then publish serially and reconcile.
- For failures or interrupted work, read
  [references/safety-and-recovery.md](references/safety-and-recovery.md) and resume
  the same `job_ref`. Never select a replacement implicitly.

## Enforce Boundaries

- Require explicit confirmation for login, sync, run/download/local analysis,
  canary, and publish. Treat publish as a separate confirmation from analysis.
- Keep publishing disabled by default. Use `canary --limit 1 --no-publish` first.
- Never call an undocumented "next item" selector. Use `plan`, an explicit limit,
  and stable `job_ref` values.
- Limit execution to one CPU analysis, at most two semantic workers, and one serial
  publisher. Default batch size to one; do not exceed five after a successful canary.
- Stop after the same failure occurs twice. Preserve the checkpoint and state the
  required user action.
- Do not bypass schema, provenance, privacy, evidence, review, journal,
  reconciliation, or acceptance gates.
- Use only the user's own account and respect platform rules. Do not add CAPTCHA,
  anti-bot, login, signature, or rate-limit bypasses.

## Adapt to the Host

Read [references/host-adapters.md](references/host-adapters.md) before claiming host
support. A host that cannot reliably write the candidate file is candidate-only or
unsupported; do not imply end-to-end support.
