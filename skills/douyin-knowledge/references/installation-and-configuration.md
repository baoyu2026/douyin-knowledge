# Installation and Configuration

## Source Checkout

From the repository root, run:

```powershell
.\scripts\bootstrap.ps1 -InstallCodexSkill
```

For a custom private instance, pass `-InstanceRoot` with an explicit path. The
bootstrap stores that binding under the ignored `.venv` directory and passes it to
the installed Skill. Re-run with `-ForceSkill` when replacing a different Skill
release; an identical installed bundle is reused safely.

## Wheel Installation

The wheel installs its portable files under
`<python-prefix>/share/douyin-knowledge`. Run the `install-skill.ps1` located there,
optionally passing `-InstanceRoot` and `-Destination`. This installer does not depend
on a working CLI import; it binds the installed CLI entry point for later execution.

After either installation, restart the host and execute the installed Skill's
`scripts/invoke.ps1`. Do not depend on a bare CLI command or activate a repository
virtual environment.

## WorkBuddy Upload Bundle

For WorkBuddy, install the backend with `scripts/bootstrap.ps1` without
`-InstallCodexSkill`, then run `scripts/export-workbuddy-bundle.ps1`. Upload the
generated `douyin-knowledge.zip` in WorkBuddy's Skill UI and import the generated
`douyin-knowledge.mcp.json` in its MCP UI.

Generate the MCP JSON separately on each machine. It contains that machine's local
Gateway launcher path but never the bound private instance path. The Skill archive
is portable and grants only the `mcp__douyin-knowledge` tool group; it does not grant
arbitrary shell, filesystem, or network tools.

After import, restart WorkBuddy and call `douyin_capabilities`. Require
`mode=candidate-only`, then pass the host capability gate with disposable evidence
before using a real packet. Uploading the Skill alone does not install or connect the
local backend.

For an MCP-capable host, configure its local MCP client to start the installed
Skill's `scripts/invoke-mcp.ps1` over `stdio`. Keep the adapter path in local host
configuration rather than model context. Read
[agent-gateway.md](agent-gateway.md) before enabling a non-Codex host; the current
gateway is candidate-only and does not authorize or expose full orchestration.

## Rebinding

To change the private instance, rerun `install-skill.ps1 -InstanceRoot <path>`. Do
not hand-edit or expose `runtime.local.json`; it intentionally contains local
absolute paths used only by the deterministic adapter.

## Human-readable Results Archive

The private instance remains the machine workspace for checkpoints, raw analysis,
and task references. Results intended for people must use a separately chosen
archive root. After `init`, ask the user for one absolute folder, explain the layout,
obtain explicit confirmation, and run:

```text
<invoke> configure results --path <absolute-folder> --confirm --json
```

Do not choose a location silently and do not expose the stored absolute path. The
CLI creates the folder when possible and stores its binding in `config/results.yml`.
Each published item is filed as `<主分类>/<标题>/`; only title collisions receive a
numeric suffix. The archive includes the original video as a real copy so it remains
self-contained if private checkpoints are later cleaned.

If a previous release already published human-readable entries under the private
instance's legacy `library/`, preserve that directory. After configuring the new
archive and receiving a separate explicit migration confirmation, run:

```text
<invoke> migrate results --confirm --json
```

The migration copies complete entries, verifies their hashes, rebuilds the new
archive index, and leaves the old directory and historical publication records in
place. Do not manually move or delete the legacy directory after migration.

Do not hand-edit or relocate a configured archive after publications have been
journaled. The root is locked because reconciliation must continue resolving every
historical `results/` handle to the same files.

## Obsidian Vault

Set the private instance's `config/obsidian.yml` to one existing Obsidian Vault:

```yaml
vault: 'D:/Knowledge/My Vault'
```

Use YAML quoting for spaces or non-ASCII paths. Require an existing `.obsidian`
directory, then run `doctor --json`. Require both `results_root_configured=true` and
`ready_for_publish=true` before offering publication.
