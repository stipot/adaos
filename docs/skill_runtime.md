# Skill Runtime Lifecycle

AdaOS provisions an isolated runtime per skill with versioned A/B slots. Each installation produces a fully self-contained copy of the skill sources, dependencies, resolved manifest, and metadata that can be activated atomically.

## Directory layout

Every skill lives under `skills/<name>` in the workspace. Runtime artefacts are stored separately:

```
skills/<name>/
    skill.yaml
    requirements.in            # optional dependency input
    handlers/
    migrations/
        data_migration.py      # reserved optional bucket migration file
    tests/

skills/.runtime/<name>/current_version
skills/.runtime/<name>/current_runtime.json
skills/.runtime/<name>/previous_runtime.json
skills/.runtime/<name>/v<major>.<minor>/
    active                      # marker with the current slot (A or B)
    previous                    # marker with the last healthy slot
    meta.json                   # test results, timestamps, history
    vendor/                     # shared pip --target deps for this bucket
    venv/                       # shared service-skill interpreter for this bucket
    data/
        db/
            skill_env.json      # shared state for this compatibility bucket
        files/
            secrets.json        # per-bucket secrets/artifacts
            .skill_env.json     # optional persisted environment snapshot
        internal/               # schema-bound internal data for this bucket
    slots/<A|B>/
        src/                    # snapshot of the skill sources
            skills/<name>/
                skill.yaml
                handlers/
                migrations/
                    data_migration.py  # reserved optional bucket migration file
                tests/
        node_modules/
        bin/
        cache/
        runtime/
            logs/
            tmp/
        resolved.manifest.json
```

Runtime isolation is keyed by semantic `major.minor`, not full SemVer. For example, `0.14.0` and `0.14.3` share `v0.14`; `0.15.0` uses `v0.15`.
Slots are A/B code deployments inside the same bucket. Data, `vendor/`, and `venv/` are not A/B-slotted inside a bucket.

## Version policy

The default publication bump for skills is `patch`. A patch release stays in the same runtime bucket and uses the existing `data/`, `vendor/`, and `venv/` trees.

If the skill has the reserved migration file, or a legacy manifest data migration hook, a requested/default patch bump is promoted to `minor`. A minor release creates a new `v<major>.<minor>` bucket and prepares a migrated copy of data there before activation.

Major releases are manual. They also land in a new bucket, but the decision to publish one is outside automatic CI/CD policy.

## Install → test → activate

`adaos skill install <name>` performs the pipeline below:

1. Select the inactive slot (A/B) for the target version and wipe any previous contents.
2. Copy the current contents of `skills/<name>` into `slots/<slot>/src`.
3. Build bucket dependencies (either reusing the host interpreter with bucket `vendor/` or creating the bucket `venv/` for service skills).
4. Enrich `manifest.json` into `resolved.manifest.json`, resolving tool entry points, interpreter paths, timeouts, and policy defaults.
5. Prepare bucket data. Patch installs in the same `v<major>.<minor>` bucket reuse the existing shared `data/` tree without copying. A new bucket safely looks for the reserved data migration file and runs it when present.
6. Optionally run `src/skills/<name>/tests/` (`--test`) from the prepared slot. Commands execute inside the staged environment (interpreter, `PYTHONPATH`, `.skill_env.json`), and logs are streamed to `slots/<slot>/logs/tests.log`.
7. Persist slot metadata (tests, timestamps, default tool, data migration result) for status and rollback operations.

`adaos skill activate <name>` switches the active version/slot markers atomically and records the previous version/slot for `adaos skill rollback`. Activation does not run data migration; migration belongs to prepare. Setup flows must run **after activation** so that secrets and runtime paths are stable.

After a successful activation, AdaOS keeps only the current runtime bucket and the previous rollback bucket. Older runtime buckets are pruned automatically because the runtime supports only one rollback step.

`adaos skill rollback <name>` rolls back the active version/slot marker. For a patch rollback this means old code over the same bucket data. For a minor rollback this points back to the previous bucket and therefore to that bucket's older data copy. AdaOS does not try to detect or block writes that happened after the minor activation.

Important architectural note:

- activation is a slot-pointer switch, not a generic live-memory migration
- in-process skills typically pick up new code on the next invocation from the active slot
- service skills are explicitly restarted by the runtime lifecycle
- durable migration authority belongs to persisted bucket data under `v<major>.<minor>/data`, while derived caches/projections should be rebuilt after activation

For the target kernel-facing migration architecture, including rehydrate and rollback semantics for stateful skills, see [AdaOS Supervisor](architecture/adaos-supervisor.md#skill-runtime-migration-lifecycle).

## Deactivate lifecycle

AdaOS may keep the core switch committed while quarantining a subset of skills.
For that case the runtime lifecycle now includes explicit deactivation:

- a deactivated skill remains installed
- its prepared slot and metadata remain inspectable
- tool execution is blocked with a clear `skill is deactivated` error
- ordinary `activate` clears the deactivation marker and returns the skill to service

This is intended for post-commit checks where rolling back the whole core is unnecessary, but continuing to serve a broken skill would be unsafe.
Core-update orchestration may trigger this automatically after a successful runtime switch if post-commit skill checks fail.
When that happens, the deactivation record now persists the failure contract itself, including `failure_kind`, `failed_stage`, `source`, and whether the core switch was already committed.

## Optional internal data migration

This feature is optional. A skill can ignore `data/internal` completely and continue using only:

- `data/db/skill_env.json`
- `data/files/*`

Use `data/internal` only for state that must evolve together with runtime schema changes.

### Default behavior

If a skill has no migration file, AdaOS does not copy data during patch prepare. The prepared slot uses the same bucket-level `data/` directory as the currently active slot.

When preparing a new minor/major bucket without a migration file, AdaOS writes a warning to the AdaOS log and copies the previous bucket `data/` tree into the target bucket without schema mutation.

### Reserved migration file

The standard migration source is reserved at:

```text
skills/<name>/migrations/data_migration.py
```

In the staged runtime this becomes:

```text
slots/<A|B>/src/skills/<name>/migrations/data_migration.py
```

The file should expose `migrate(payload: dict) -> dict | None`. During prepare of a new compatibility bucket, AdaOS runs it against the staged skill sources for the target slot. The migration file owns target data population: it should copy the source data it wants to preserve and mutate schema-bound state as needed.

Manifest-level `data_migration_tool` declarations are legacy-compatible, but LLM-authored skills should prefer the reserved file so core and generated code agree without extra manifest wiring.

The hook receives a payload with:

- `source_version`
- `target_version`
- `source_runtime_bucket`
- `target_runtime_bucket`
- `source_data_root`
- `target_data_root`
- `source_internal_dir`
- `target_internal_dir`
- `data_root`
- `internal_root`
- `runtime_slot`
- `version`

AdaOS also exposes convenience environment variables while the hook runs:

- `ADAOS_SKILL_INTERNAL_DATA_ROOT`
- `ADAOS_SKILL_INTERNAL_ACTIVE_PATH`
- `ADAOS_SKILL_INTERNAL_TARGET_PATH`

Important notes:

- the hook is optional
- if the reserved file is absent on a minor/major bucket change, AdaOS logs a warning and falls back to a plain data copy
- the hook is expected to populate the target bucket data it owns
- on migration failure, AdaOS clears the target bucket data and fails `prepare_runtime`

### Target direction

The target AdaOS migration model separates state classes:

- canonical durable state:
  must survive restart, rollback, and rebuild
- bucket-bound schema state:
  belongs under `v<major>.<minor>/data/internal`
- derived runtime state:
  caches, indexes, projections, and similar rebuildable material
- live memory:
  in-flight objects and subscriptions that should be drained and recreated, not migrated implicitly

This means the reserved data migration file should be used for schema-sensitive persisted state, not as a platform promise that arbitrary process memory can be moved across activation.

After activation, stateful skills are expected to rebuild derived runtime state from durable truth.

## Vendor vs venv

`vendor/` and `venv/` are separated because they solve different dependency problems:

- `vendor/` is a bucket-level `pip --target` package overlay. It is added to `PYTHONPATH` for ordinary Python skills that can run in the hub interpreter but need extra pure-Python packages.
- `venv/` is a bucket-level isolated Python environment. Service skills use it when they need their own interpreter process, ABI boundary, or dependencies that must not be installed into the hub runtime.

Both live under `v<major>.<minor>` so patch A/B deployments do not duplicate dependency environments. A minor or major bucket gives the skill a fresh dependency boundary when the migration model says compatibility changed.

### Runtime lifecycle hooks

AdaOS now supports optional lifecycle hooks in the resolved skill manifest.

Preferred declaration shape:

```yaml
lifecycle:
  persist_before_switch: persist_state
  after_activate: after_activate
  rehydrate: rehydrate
  drain: drain
  dispose: dispose
  before_deactivate: before_deactivate
```

The hook names resolve through the ordinary skill `tools` table. Data migration itself should use the reserved `migrations/data_migration.py` file for new skills.

Current behavior:

- `persist_before_switch` runs against the currently active slot before pointer cutover when an active prepared runtime exists
- `after_activate` runs after the new slot becomes active
- `rehydrate` runs after activation to rebuild derived runtime state
- `drain` runs before rollback/deactivate or activation-failure cleanup when declared
- `dispose` runs after `drain` and before `before_deactivate` when declared
- `before_deactivate` runs before explicit deactivate or rollback of the current slot
- global runtime drain now reuses the same contract:
  `subnet.draining` triggers active-skill `drain`, and `subnet.stopping` triggers `dispose` then `before_deactivate` as best-effort shutdown hooks for active installed runtimes

Lifecycle diagnostics are persisted into slot metadata and surfaced by `adaos skill status --json` through `runtime_status().lifecycle`.

If activation already switched to a new version/slot and `rehydrate` then fails, AdaOS now attempts to restore:

- the previous active version marker
- the previous active slot selection
- the previous runtime bucket data, by restoring the previous active version/slot marker
- the previous deactivation state

The failed target slot keeps its lifecycle diagnostics so operators can inspect the failed `rehydrate`, shutdown hooks, and rollback result.

Post-commit migration checks now also consume these lifecycle diagnostics.
That means a skill may be marked failed or selectively deactivated because `rehydrate` / `healthcheck` is already unhealthy even before any explicit post-commit test suite runs.

Operator-facing migration reports also surface lifecycle failures separately from test failures, so a `lifecycle/rehydrate` failure is visible as a first-class shutdown/migration issue rather than only as a generic failed skill.
This same metadata is also written into the deactivation marker when a skill is selectively quarantined after a committed core switch.
Supervisor-facing validation status and operator projections now also surface a compact quarantine summary, so post-commit status can show which skill was quarantined and at which lifecycle/test stage.

## Tool execution and setup

`adaos skill run <name> [<tool>]` reads the active slot’s `resolved.manifest.json`, adds the staged source directory to `sys.path`, and executes the tool callable with per-invocation timeouts. `adaos skill test <name>` reuses the same active slot to execute `src/skills/<name>/tests` without preparing a new build. If a skill declares a `setup` tool it is available via `adaos skill setup <name>` **only after activation**; attempting to run setup while the version is pending reports a clear error instructing the operator to activate first.

## Secrets management

Secrets are stored under `skills/.runtime/<name>/v<major>.<minor>/data/files/secrets.json` and are never copied into the source tree. Runtime execution injects secrets at process start and keeps placeholders (`${secret:NAME}`) inside `resolved.manifest.json`.

Use the CLI to manage secrets either globally or per skill:

```
adaos secrets set WEATHER_API_KEY <value> --skill weather_skill
adaos secrets list                      # lists all skills with stored secrets
adaos secrets export > backup.json      # exports secrets grouped by skill (values redacted)
adaos secrets import backup.json        # restores secrets into installed skills
adaos secrets list --skill weather_skill
adaos secrets export --skill weather_skill --show
adaos secrets import dump.json --skill weather_skill
```

`adaos skill setup weather_skill` is a thin wrapper around the skill-defined setup tool that typically requests credentials and persists them via the per-skill secrets backend.

## Observability

Every install/test/activate/run operation logs under `slots/<slot>/logs/`. `adaos skill status --json` surfaces runtime state (active version/slot/readiness/tests). For progress checks:

- Workspace: `adaos skill status <NAME> --fetch --diff` compares `skills/<NAME>` against the workspace registry remote (`adaos-registry.git` main).
- Dev: `adaos skill status --space dev <NAME> --fetch --diff` compares the dev folder against the hub draft state via Root API (requires hub mTLS keys from the bootstrap `node.yaml`).

Workspace markers are split by state plane. `git-dirty` means local filesystem
changes are not committed, while `git-ahead` means path-level commits exist
locally and still need a registry push. `git-behind` means the registry base has
newer path-level commits, and `git-different` is the fallback when the path
differs but Git cannot classify the divergence. `git-error` means the CLI could
not compute the Git comparison. Runtime markers are separate: `runtime-ahead`
means the workspace skill version is ahead of the active runtime slot,
`runtime-behind` means the active slot is newer than the workspace source, and
`runtime-different` means the versions differ but cannot be ordered.

`adaos skill push` without a skill name is a batch release command, not a raw
Git transport command. It finds skills with dirty files, committed `git-ahead`
changes, or an explicit manifest-vs-registry version drift, then runs the same
version bump, registry update, commit, and push flow used by
`adaos skill push <name> -m ...`. Batch release commits use the standard message
`chore(<skill>): release workspace changes`.

## Weather skill reference

`.adaos/skills/weather_skill/` demonstrates the complete lifecycle:

1. Install the reference skill with tests: `adaos skill install weather_skill --test`.
2. Activate the freshly prepared slot: `adaos skill activate weather_skill`.
3. Run setup to capture the API key via secrets: `adaos skill setup weather_skill`.
4. Execute the default tool: `adaos skill run weather_skill --json '{"city": "Paris"}'`.

The repository contains smoke and contract tests under `src/skills/<name>/tests/` and an optional health probe that can be used by the platform for readiness checks.
