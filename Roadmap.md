# AdaOS Roadmap

## LLM Skill Creation in AdaOS

### Scaffold

- [ ] `adaos skill new` supports template `llm-min`
- [ ] Minimal skeleton created (`skill.yaml`, `handlers/`, `prep/prep_request.md`, `.ado/manifest.json`)

### Prep Stage (Task Clarification)

- [ ] Command: `adaos skill prep start <skill>` runs prep workflow
- [ ] LLM generates multiple implementation **options** (`prep/options.json`)
- [ ] Mark incompatible or conflicting options explicitly
- [ ] Produce comparative scoring (`prep/scorecard.json`)
- [ ] Interactive user review: `adaos skill review open --stage prep`
- [ ] User can: **approve**, **refine task**, **reject**, or **defer to backlog**
- [ ] Record decision in ADR (`prep/ADR-0001-decision.md`)
- [ ] Support for **profiles** (`profiles/*.yaml`, `adaos skill profile set <profile>`)

### Code Generation

- [ ] Build prompt for LLM (`skill-gen.request.json`)
- [ ] Parse response and write files (handlers, schemas, tests, readme)
- [ ] Automatic refine loop if compilation/validation fails
- [ ] Update `.ado/manifest.json` with generation status

### Testing

- [ ] Command: `adaos skill test <skill>`
- [ ] **Smoke tests**: CLI + HTTP Tool API
- [ ] **Contract tests**: input/output schema validation
- [ ] **Fixtures/mocks** for LLM responses
- [ ] Refine loop if tests fail

### Review & Release

- [ ] Command: `adaos skill review open --stage release`
- [ ] Acceptance checklist shown to the user:
  - Goal is met
  - Input/output contracts respected
  - No incompatible variants hidden in one skill
  - Latency and cost within expected range
  - Negative cases handled
- [ ] Approve release: `adaos skill review approve --stage release`
- [ ] Reject/revisit backlog if necessary

### Versioning

- [ ] Version scheme **A.B.C**
  - **A** — user-approved releases (new requirements/major variants)
  - **B** — user-driven refinements (new device, yearly improvement)
  - **C** — model-driven refinements (auto-fixes, retries)
- [ ] Command: `adaos skill version bump --level major|minor|patch`
- [ ] Automatic git tag on release

### Git Integration

- [ ] Commands: `adaos skill git stage|commit|push`
- [ ] Ignore secrets and `prep_result.json`
- [ ] Save all LLM prompts/responses into `.ado/sessions/`
- [ ] Store change requests/backlog in `.ado/changes/` and `.ado/backlog.json`

### Backlog Handling

- [ ] Command: `adaos skill defer <skill> --reason ...`
- [ ] Store unresolved tasks (`.ado/backlog.json`)
- [ ] List backlog: `adaos skill backlog list`
- [ ] Resume task from backlog: `adaos skill revive <id>`

### Observability (minimal for creation)

- [ ] Log all refine iterations
- [ ] Persist all LLM sessions (prompts + responses)
- [ ] Status summary: `adaos skill status <skill>`

## AdaOS Skill Release Acceptance (Minimal Checklist)

Please confirm the following before approving the release:

### 1. Goal

- [ ] Does the skill do what you asked for? (main task is covered)

### 2. Output

- [ ] Is the answer format acceptable? (text/JSON/cards as expected)

### 3. Reliability (basic check)

- [ ] Did the quick test (`adaos skill test <skill>`) run without obvious failures?

### 4. Decision

- [ ] Approve release (version will be bumped and committed)
- [ ] Refine (return to code generation or prep)
- [ ] Defer to backlog (needs human developer attention)

## AdaOS Skill Release Acceptance Checklist

Please confirm that the generated skill meets your expectations.  
Mark each item as [x] if satisfied, or leave unchecked to refine.

### 1. Goal & Scope

- [ ] The skill fulfills the original task / user story
- [ ] The task statement is precise and does not hide contradictions
- [ ] No incompatible variants are mixed in one implementation

### 2. Input / Output

- [ ] Input entities match the agreed schema
- [ ] Output format is correct (e.g. JSON-only, text, cards)
- [ ] Negative cases are handled gracefully (invalid input, missing data)

### 3. Performance & Cost

- [ ] Response time is acceptable (latency target met)
- [ ] Token usage / cost is within expected budget
- [ ] No excessive retries or unnecessary calls

### 4. Reliability & Robustness

- [ ] Failures are reported clearly (user-friendly errors)
- [ ] API limits / rate limits are handled (backoff, caching if needed)
- [ ] Skill does not expose sensitive data (keys, PII masked)

### 5. Documentation

- [ ] README.md explains purpose, usage, and examples
- [ ] `skill.yaml` metadata is correct
- [ ] Tests are present and runnable (`adaos skill test` passes)

### 6. Decision

- [ ] Approve release (skill will be versioned and committed)
- [ ] Reject and refine (return to code generation or prep)
- [ ] Defer to backlog (skill needs human developer attention)
