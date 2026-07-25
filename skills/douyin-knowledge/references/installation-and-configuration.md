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

## Rebinding

To change the private instance, rerun `install-skill.ps1 -InstanceRoot <path>`. Do
not hand-edit or expose `runtime.local.json`; it intentionally contains local
absolute paths used only by the deterministic adapter.

## Obsidian Vault

Set the private instance's `config/obsidian.yml` to one existing Obsidian Vault:

```yaml
vault: 'D:/Knowledge/My Vault'
```

Use YAML quoting for spaces or non-ASCII paths. Require an existing `.obsidian`
directory, then run `doctor --json` and require `ready_for_publish=true` before
offering publication.
