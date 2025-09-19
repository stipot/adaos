# SDK Control Plane Tools

AdaOS exposes a safe control plane to skills through a curated set of SDK tools.
These helpers wrap internal services and enforce capability checks, idempotency and
quota guards so that LLM-powered skills can manage their own lifecycle without
breaking the host runtime.

## Capabilities overview

Every tool requires an explicit capability token on `ctx.caps`:

| Tool prefix                | Capability token                |
|---------------------------|---------------------------------|
| `manage.self.*`           | `manage.self.validate`, `manage.self.state`, `manage.self.update` |
| `skills.*`                | `manage.skills.install`, `manage.skills.uninstall`, `manage.skills.list` |
| `scenarios.*`             | `manage.scenarios.toggle`, `manage.scenarios.bind` |
| `resources.*`             | `manage.resources.request`, `manage.resources.status` |

Missing capabilities raise `CapabilityError` immediately. SDK calls made before
runtime initialisation raise `SdkRuntimeNotInitialized`.

## Self-management tools (`manage.self.*`)

### `manage.self.validate`

*Purpose:* validate the current skill against static schema and runtime exports.

```json
{
  "ok": true,
  "issues": [
    {"level": "warning", "code": "tools.missing_export", "message": "...", "where": "tools[]"}
  ]
}
```

Inputs support `strict` and `probe_tools` booleans. The call resolves the
currently selected skill and delegates to `SkillValidationService`. The issues
array mirrors the service `ValidationReport`.

### `manage.self.state.get`

Fetch private per-skill state from the KV store under
`skills/{skill_id}/state/{key}` (or `global/state/...` when no skill is active).

```json
{
  "status": "ok",
  "key": "last_run",
  "value": {"ts": 1690000000},
  "found": true
}
```

### `manage.self.state.put`

Store/update state with idempotency. The request requires `request_id` and
supports `dry_run`. Stored results are cached under both
`requests/manage.self.state.put/{skill}/{request_id}` and
`skills/{skill}/requests/{request_id}`.

```json
{
  "status": "ok",
  "key": "last_run",
  "value": {"ts": 1690000000},
  "stored": true,
  "dry_run": false,
  "request_id": "abc",
  "previous": null,
  "previous_exists": false
}
```

`dry_run=true` performs validation without writing. Repeating a request with the
same `request_id` returns the cached result.

### `manage.self.update.request`

Request pulling the latest version of the active skill. Results include the
updated flag and optional version string:

```json
{"status": "ok", "updated": true, "version": "1.2.3", "dry_run": false, "request_id": "abc"}
```

If the skill directory is read-only the SDK translates the underlying
permission error into `CapabilityError`.

## Skill administration (`skills.*`)

Available only to deployments that grant `manage.skills.*` tokens.

* `skills.install(ref, request_id, dry_run=false)` → installs from monorepo or git
  URL. Idempotent per `request_id`.
* `skills.uninstall(skill_id, request_id, dry_run=false)` → removes the skill if
  present, otherwise returns `action: "noop"`.
* `skills.list()` → returns `{ "skills": [{"id": "demo", "name": "demo", "version": "1.0.0"}] }`.

Monorepo mode respects the catalog enforced by `GitSkillRepository`. Dry runs
never perform git mutations but still validate references.

## Scenario control (`scenarios.*`)

* `scenarios.toggle(scenario_id, enabled, request_id, dry_run=false)` writes
  `scenarios/{id}/enabled` in KV and caches the result.
* `scenarios.bind.set(scenario_id, key, value, request_id, dry_run=false)` stores
  bindings under `scenarios/{id}/bindings/{key}`. Unknown scenarios return
  `{ "status": "error", "code": "not_found" }` without raising.

## Resource requests (`resources.*`)

* `resources.request(name, scope, request_id, dry_run=false)` creates a ticket at
  `resources/requests/{request_id}` with payload `{skill_id, name, scope, status}`.
* `resources.status(ticket_id)` retrieves ticket state (`pending`, `approved`,
  `denied`) or `{"status": "not_found"}`.

These calls only create control-plane tickets; actual device access is governed
by host policy.

## Idempotency & dry runs

All mutating methods accept `request_id` and `dry_run` (default `false`). The
SDK stores successful responses under `requests/{namespace}/{skill|global}/{request_id}`
so repeated calls reuse the cached result. `dry_run=true` performs the same
validation paths without writing state or git changes.

## Error model

* `SdkRuntimeNotInitialized` – runtime context missing.
* `CapabilityError` – capability token missing or host policy rejects the action.
* `QuotaExceeded` – KV/FS/NET quota guards deny an operation.
* `ConflictError` – reserved for idempotency conflicts.

Errors bubble up with structured messages so LLM skills can react accordingly.

## Security considerations

* Keys are namespaced (`skills/{id}/state`, `scenarios/{id}/...`) with validation
  to prevent path traversal.
* Repository actions reuse adapters (`GitSkillRepository`, `GitScenarioRepository`),
  inheriting catalog allow-lists in monorepo deployments.
* No raw subprocess or network calls escape the SDK; all effects run through
  services guarded by capability checks and quotas.
