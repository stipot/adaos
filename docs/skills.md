# Skills

## What a skill is

In the current AdaOS runtime, a skill is a managed unit that can be scaffolded, validated, installed, updated, activated, and in some cases supervised as a long-running service.

## Common commands

```bash
adaos skill list
adaos skill create my_skill
adaos skill validate my_skill
adaos skill install my_skill
adaos skill migrate
adaos skill activate my_skill
adaos skill rollback my_skill
```

## Runtime-oriented commands

```bash
adaos skill run my_skill --topic some.topic --payload '{}'
adaos skill setup my_skill
adaos skill status
adaos skill doctor my_skill
adaos skill gc
```

For runtime bucket layout, rollback semantics, and the reserved `migrations/data_migration.py` flow, see [Skill Runtime Lifecycle](skill_runtime.md).

`adaos skill list --local` and `adaos skill status <name>` include state
markers for workspace skills:

- `git-dirty`: the skill source has uncommitted filesystem changes.
- `git-ahead`: commits touching the skill exist locally but are not in the
  workspace registry/upstream base yet.
- `git-behind`: the workspace registry/upstream base has newer commits touching
  the skill.
- `git-different`: the source differs from the base, but Git could not classify the
  path-level divergence as ahead or behind.
- `git-error`: the CLI could not compute the Git comparison, usually because
  the base ref is not fetched or the workspace is not a Git repository.
- `runtime-ahead`: the workspace skill version is ahead of the active runtime
  slot version, so install/activate is needed before runtime catches up.
- `runtime-behind`: the active runtime slot version is newer than the workspace
  skill version.
- `runtime-different`: the workspace and active runtime versions differ, but
  the CLI could not order the versions.

Use `adaos skill status <name> --fetch --diff` before publishing when you need
the exact comparison against the registry base.

For browser-facing or LLM-authored skills, follow
[LLM-Safe Skill Development Guide](guides/llm-skill-development.md). That guide
defines the current Yjs, stream, projection, details, and guard/quarantine
contracts for skills.

## Service-type skills

Some skills are exposed through the service supervisor:

```bash
adaos skill service list
adaos skill service status <name>
adaos skill service restart <name>
```

This path is backed by `/api/services/*`.

## Publishing split

The repository now distinguishes between:

- workspace git push commands such as `adaos skill push`
- workspace publish-commit commands such as `adaos skill push <name> --message ...`
- Root-backed developer publishing through `adaos dev skill ...`

That split is important when reading older documentation.

`adaos skill push` has two workspace modes:

- `adaos skill push`: release every workspace skill with pending source changes
  through the normal publication rails. For each candidate skill it bumps the
  manifest version, updates `registry.json`, commits with the standard message
  `chore(<skill>): release workspace changes`, and pushes.
- `adaos skill push <name> -m "message"`: update that skill's registry entry,
  bump the manifest version, commit `skills/<name>` plus `registry.json`, and
  push.

Use the no-argument form when one or more skills show `git-dirty`, `git-ahead`,
or when `skill.yaml` and `registry.json` disagree on the skill version. Use the
named `-m` form when a human-authored release message matters.
