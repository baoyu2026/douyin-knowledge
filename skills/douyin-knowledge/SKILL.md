---
name: douyin-knowledge
description: Convert a user's own Douyin favorites into a private, reviewed local knowledge library using the douyin-knowledge JSON CLI, local ASR/OCR, bounded semantic JSON candidates, deterministic validation, Obsidian publication, checkpoints, and reconciliation. Use when the user asks to log in to Douyin, sync favorites or 收藏, process one or a small batch of saved videos, resume a failed item, review generated knowledge, publish to Library/Obsidian, inspect pipeline status, or operate this repository from Codex, OpenClaw, or a candidate-only host.
---

# Douyin Knowledge

Operate the installed `douyin-knowledge` CLI as the authority. Keep account access,
media, analysis, review, publication state, and credentials in the user's private
instance directory. Never treat an assistant message as completion evidence.

## Start Safely

1. Resolve the instance root from the user's explicit choice or
   `DOUYIN_KNOWLEDGE_ROOT`. Do not guess a production directory.
2. Run `douyin-knowledge --root <root> init --json`, then `doctor --json` and
   `status --json`.
3. Read [references/cli-contract.md](references/cli-contract.md) before composing
   commands or interpreting errors.
4. Report only `safe_summary`, stable `job_ref` values, counts, booleans, relative
   handles, and documented error fields. Do not echo absolute paths, URLs, raw
   platform IDs, cookies, model logs, or reviewer notes.

## Choose the Workflow

- For first use, initialize and diagnose, then explain the login confirmation gate.
- For collection refresh, run confirmed `sync`; it must not download media.
- For one item, use `plan --limit 1`, select that returned `job_ref`, then run a
  confirmed no-publish canary.
- For semantic work, export one packet, let one worker write one pure JSON
  candidate, import it, and check authoritative status. Read
  [references/semantic-worker-protocol.md](references/semantic-worker-protocol.md).
- For human review, record `approve` or `reject`. Never publish an unapproved current
  candidate.
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
