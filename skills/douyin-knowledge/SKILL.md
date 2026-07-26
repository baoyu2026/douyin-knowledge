---
name: douyin-knowledge
description: Convert a user's own Douyin favorites into a private, reconciled local knowledge library with a user-chosen human-readable results archive, using the douyin-knowledge JSON CLI, local ASR/OCR, bounded semantic JSON candidates, deterministic validation, Obsidian publication, checkpoints, legacy-result migration and verified cleanup, and post-publication correction. Use when the user asks to configure or organize where Douyin results are stored, migrate or clean up historical results, log in to Douyin, sync favorites or 收藏, process one or a small batch of saved videos, resume a failed item, inspect or correct published knowledge, publish to the results archive/Obsidian, inspect pipeline status, or operate an installed douyin-knowledge release from Codex or another host that has passed the documented capability gate.
---

# Douyin Knowledge

Operate the installed `douyin-knowledge` CLI as the authority. Keep account access,
media, analysis, correction, publication state, and credentials in the user's private
instance directory. Never treat an assistant message as completion evidence.

## Start Safely

1. Invoke `scripts/invoke.ps1` from this Skill for every CLI operation. Never assume
   the bare `douyin-knowledge` command is on `PATH`. If the adapter is unavailable,
   read [references/installation-and-configuration.md](references/installation-and-configuration.md).
2. Use the instance root bound by the installer. To change it, rerun the installer
   with the user's explicit path; do not read or expose `runtime.local.json`.
3. Run `init --json`, then `doctor --json` and `status --json` through the adapter.
   If `results_root_configured=false`, ask the user where the human-readable results
   archive should live. Do not infer it from the private instance or Obsidian Vault.
   Explain that each accepted item uses `主分类/标题/` and contains the knowledge note,
   original video, timeline, cited frames, and a small manifest. After the user gives
   an absolute folder and explicitly confirms the configuration write, run
   `configure results`; then rerun `doctor`.
4. Read [references/cli-contract.md](references/cli-contract.md) before composing
   commands or interpreting errors.
5. Report only `safe_summary`, stable `job_ref` values, counts, booleans, relative
   handles, and documented error fields. During a user-requested correction, also
   permit a bounded generated-note excerpt. Never echo absolute paths, URLs, raw
   platform IDs, cookies, model logs, or private correction notes.

## Choose the Workflow

- For first use, initialize, establish the user-chosen results archive, diagnose,
  then explain the login confirmation gate.
- For historical results still under the private instance's legacy `library`, first
  configure the user-chosen archive, describe that migration copies and verifies
  complete entries while preserving the source and historical publication journal,
  obtain explicit confirmation, then run `migrate results`. Treat its verified count
  as migration evidence only; it does not create a new accepted publication.
- When the user explicitly asks to delete migrated legacy results, explain that the
  rollback copy and stale duplicates will be permanently removed while the verified
  results archive, migration checkpoint, and publication journal remain. Run
  `migrate cleanup` only after a successful migration and a fresh explicit cleanup
  confirmation. Never delete the legacy root manually.
- For collection refresh, run confirmed `sync`; it must not download media.
- For one item, use `plan --limit 1`; add `--status new` when the request requires an
  item whose authoritative collection status is new. Select that returned `job_ref`,
  then run a no-publish canary with the same status filter within the user's authorized
  scope. A new registry item can still have cached media from an earlier probe; require
  both returned reuse flags to be false before describing the execution as a cold run.
- For semantic work, export one packet and assign it to exactly one worker. That
  same worker must personally read every evidence chunk, inspect every visual, and
  atomically write one pure JSON candidate; never split or re-delegate a partially
  reviewed packet. Import it and check authoritative status. Read
  [references/semantic-worker-protocol.md](references/semantic-worker-protocol.md).
- After successful candidate import, do not stop for pre-publication human approval.
  The candidate is publishable only after deterministic schema, provenance, privacy,
  evidence, and content gates pass.
- Before starting an operational request, describe the exact job or bounded batch,
  network/model calls, expected time, human-readable results/Vault writes,
  journal/checkpoint behavior, and validation. A direct request such as "run one
  video end to end" or "process these items and publish them" authorizes all of
  those stages for that fixed scope when it uses the already configured destinations
  that the user previously selected or used. Do not pause at publication merely to
  collect a second mechanical confirmation. Publish serially and count the job as
  complete only when publication returns `accepted` after reconciliation.
- For correction, let the user inspect the accepted note in Obsidian. When the user
  requests a correction, treat that request as sufficient correction intent; do not
  ask them to complete a separate approve/reject workflow. Preserve candidate
  history and the user's unmanaged Obsidian sections, generate and import one
  corrected candidate, then republish and reconcile when the user's request clearly
  asks for the correction to be completed. A request to diagnose only does not
  authorize republishing. Record an internal review decision only
  when required for candidate history or requested for audit; it is never a
  completion gate.
- For failures or interrupted work, read
  [references/safety-and-recovery.md](references/safety-and-recovery.md) and resume
  the same `job_ref`. Never select a replacement implicitly.
- Give a confirmed `run` invocation at least two hours of host execution time, or
  use a host session that can yield and resume. If the host reports a timeout, run
  `status` before doing anything else. When the same `active_job_ref` is still
  present, poll no more often than every 30 to 60 seconds and do not start a duplicate
  analysis. Never delete a run lock manually. If it remains after the documented
  two-hour analysis window, retry only the same `job_ref` and the same previously
  confirmed stop point; the CLI decides whether the lease is stale and reuses
  existing checkpoints. If the stop point or start time is unknown, do not infer or
  expand it: keep monitoring or ask the user before a newly confirmed retry.

## Enforce Boundaries

- Treat confirmation as scoped authorization, not a phrase-matching ceremony. An
  imperative user request made after, or together with, a clear scope description is
  sufficient; never require the literal word `confirm`. One authorization may cover
  download, local analysis, candidate generation, and publication for the same fixed
  job or bounded batch when the user asks for the complete workflow.
- Never infer authorization for a new or changed results archive, Vault, account, or
  collection from a generic end-to-end request. Describe the new scope and wait for
  one user response before the first write or login involving that scope.
- Obtain new authorization only when the scope or destination changes, the request
  was read-only or ambiguous, login/CAPTCHA interaction becomes necessary, an
  overwrite/conflict cannot be resolved deterministically, or an irreversible action
  such as legacy cleanup is introduced. Folder configuration, historical migration,
  and cleanup remain separately scoped operations.
- Keep publishing disabled by default. Use `canary --limit 1 --no-publish` first.
- Never call an undocumented "next item" selector. Use `plan`, an explicit limit,
  an explicit status when state matters, and stable `job_ref` values.
- Limit execution to one CPU analysis, at most two semantic workers, and one serial
  publisher. Default batch size to one; do not exceed five after a successful canary.
- Stop after the same failure occurs twice. Preserve the checkpoint and state the
  required user action.
- Do not bypass schema, provenance, privacy, evidence, journal,
  reconciliation, or acceptance gates.
- Use only the user's own account and respect platform rules. Do not add CAPTCHA,
  anti-bot, login, signature, or rate-limit bypasses.

## Adapt to the Host

Read [references/host-adapters.md](references/host-adapters.md) before claiming host
support. A host that cannot reliably write the candidate file is candidate-only or
unsupported; do not imply end-to-end support.
