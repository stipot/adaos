# AdaOS

![AdaOS CI](https://github.com/stipot-com/adaos/actions/workflows/ci.yml/badge.svg)

AdaOS is an environment for personal digital assistants. It turns devices, agents, skills, applications, and interfaces into one assistant environment while keeping the lower-level subnet, scenario, widget, and hub/member machinery available for diagnostics and developer workflows.

[Documentation](https://stipot-com.github.io/adaos/)

## Overview

AdaOS is designed around a small core and extensible edges:

- an **Assistant** is the persistent user-facing environment, internally backed by a subnet
- **Webspaces** define web access and projection contexts such as Main, Owner, Guests, or Developer
- **Applications** are user-facing scenarios inside a webspace; `web_desktop` is shown as **Capabilities**
- **Devices** are physical or virtual hosts, while **Agents** are software participants running on them
- `skills` implement focused capabilities such as integrations, automations, interface logic, or assistant behaviors
- `SDK`, `CLI` and `API` provide local control, automation, diagnostics, and operational workflows

This repository contains the open developer platform: core runtime components, SDK modules, bootstrap scripts, documentation, and tests for local development, experimentation, and integration work.

## Why AdaOS

Most real-world assistant and automation tasks do not live inside a single chat window or a single device. They span browsers, local services, devices, users, and external systems. AdaOS is built to support this kind of distributed environment with a model based on composable skills, scenarios, and node roles.

AdaOS is intended for:

- local and private assistant deployments
- distributed automation and service orchestration
- experimental multi-node assistant environments
- university labs, applied research, and student-built products
- integrators and teams building domain-specific assistant solutions

## Features

- Python 3.11.9+ CLI exposed as `adaos`
- Local HTTP API for node and runtime control
- Skill and scenario development workflows
- Bootstrap scripts for Linux, macOS, and Windows
- Runtime support for hub/member node roles
- Developer-oriented project structure for extensions, testing, and automation
- Optional submodules for related client, backend, and infrastructure work
- MkDocs-based documentation site

## What You Can Build

With AdaOS you can:

- develop and run skills for integrations, automation, and assistant behavior
- compose scenarios that coordinate multi-step flows across services and nodes
- expose runtime functionality through a local API and CLI
- experiment with distributed assistant topologies using hub and member roles
- prototype local or private digital assistant environments
- use the platform as a base for campus, lab, and applied product development

## Quick Start

## Install from a single line (init scripts)

### Linux

```bash
# use optional key to join member to subnet: --zone ru --join-code CODE 
curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --zone ru|us|eu
# if your PATH points at a uv shim, force a real interpreter path:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --python /usr/bin/python3.11 --zone ru
# set a friendly node name that will be shown in the desktop:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --node-name "Codespace Member" --zone ru
# disable hub/member core updates from CI/CD signals for this node:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --no-core-update --zone ru
# bootstrap from a fork instead of the upstream core repo:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --use-git-from https://github.com/<you>/adaos.git --rev my-branch --zone ru
# use a fork of the workspace registry repo:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --workspace-registry-repo https://github.com/<you>/adaos-registry.git --zone ru
# in GitHub Codespaces, reuse the current checkout directly and keep core updates manual:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --codespaces --node-name "Codespace Member" --no-core-update --zone ru
# or install directly into the current directory:
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --dest . --zone ru
```

### Windows (PowerShell)

```powershell
# requires Windows PowerShell 5.1+ or PowerShell 7+
# use optional key to join member to subnet: -JoinCode CODE
& ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content))
# pick zone explicitly when needed:
# & ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content)) -ZoneId ru
# bootstrap from a fork instead of the upstream core repo:
# & ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content)) -UseGitFrom https://github.com/<you>/adaos.git -Rev my-branch
# use a fork of the workspace registry repo:
# & ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content)) -WorkspaceRegistryRepo https://github.com/<you>/adaos-registry.git -ZoneId ru
# enable dev bootstrap when needed:
# & ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content)) -ZoneId ru -Dev
```

### Windows (CMD)

```bat
REM requires Windows PowerShell 5.1+ or PowerShell 7+
# use optional key to join member to subnet: -JoinCode CODE
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -JoinCode CODE
REM pick zone explicitly when needed:
REM powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -ZoneId ru
REM bootstrap from a fork instead of the upstream core repo:
REM powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -UseGitFrom https://github.com/<you>/adaos.git -Rev my-branch
REM use a fork of the workspace registry repo:
REM powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -WorkspaceRegistryRepo https://github.com/<you>/adaos-registry.git -ZoneId ru
REM enable dev bootstrap when needed:
REM powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -ZoneId ru -Dev
```

## Переключить текущий shell на runtime активного core slot, не трогая корневой `.venv`, используйте source-able script из `tools/`

```bash
source tools/slot-shell.sh
source tools/slot-shell.sh --cd
```

Для PowerShell:

```powershell
. .\tools\slot-shell.ps1
. .\tools\slot-shell.ps1 -Cd
```

State-changing production CLI commands try to re-exec into the active slot
automatically. If the current process is still outside the active slot context,
the CLI emits `slot_shell_required`; run `source tools/slot-shell.sh --cd`
or `. .\tools\slot-shell.ps1 -Cd` before retrying.

Benchmark

```
adaos node yjs benchmark-scenario --webspace default --scenario-id infrascope --baseline-scenario web_desktop --iterations 5 --detail
```

## AdaOS Service management

```bash
# Restart
systemctl --user daemon-reload
systemctl --user restart adaos.service
# Проверить
systemctl --user status adaos.service --no-pager
journalctl --user -u adaos.service -n 120 --no-pager
ADAOS_TOKEN=********
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8777/api/node/status
adaos autostart enable
adaos autostart status
adaos autostart update-complete
adaos autostart inspect
adaos autostart inspect --json
adaos autostart disable


adaos autostart update-status
export ADAOS_TOKEN='********'
adaos autostart update-start
# Checkup
cat ~/adaos/.adaos/state/core_update/status.json
cat ~/.adaos/state/core_update/status.json
adaos autostart update-cancel
adaos autostart update-rollback
adaos autostart smoke-update

adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 5 --json
adaos autostart update-cancel --json
adaos autostart update-rollback --json

# Recommended smoke-order:
adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 30 --json
adaos autostart update-cancel --json
adaos autostart smoke-update --countdown-sec 5 --json


adaos node reliability
adaos node status
adaos node status --probe

adaos autostart inspect
adaos autostart inspect --json
adaos autostart inspect --sample-sec 0.5

# CPU inspection
adaos autostart inspect --json
runtime_pid = 26718 runtime_process.pid
/root/.adaos/state/core_slots/slots/A/venv/bin/python -m pip install py-spy
/root/adaos/.adaos/state/core_slots/slots/B/venv/bin/python -m pip install 'websockets>=13,<16'
/root/.adaos/state/core_slots/slots/B/venv/bin/py-spy dump --pid 26718

# Web client URL parameters:
# Full list: src/adaos/integrations/adaos-client/README.md#client-url-parameters
# https://myinimatic.web.app/?zone=ru&mode=login&auto_login=1
# https://myinimatic.web.app/?boot_debug=1
# https://myinimatic.web.app/?runtime_debug=0
# https://myinimatic.web.app/?yjs_persist=0

```

19193

### Clone

```bash
git clone -b rev2026 https://github.com/stipot-com/adaos.git
cd adaos
```

### Linux / macOS

```bash
bash tools/bootstrap.sh
source .venv/bin/activate
adaos --help
# bash tools/bootstrap.sh --zone ru --dev
# bash tools/bootstrap.sh --python /usr/bin/python3.11 --zone ru --dev
# bash tools/bootstrap.sh --node-name "Local Dev Node" --zone ru --dev
```

### Windows PowerShell

Using `uv`:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope Process
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
.\.venv\Scripts\Activate.ps1
adaos --help
# powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -ZoneId ru -Dev
```

Using `pip`:

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -ZoneId ru -Dev
.\.venv\Scripts\Activate.ps1
adaos --help
# powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -ZoneId ru -Dev
```

Repo bootstrap scripts support zone-aware Root routing via `--zone` or `-ZoneId`. Use only a two-letter country or region code such as `ru`. When the default public Root URL is in use, national zones follow the `[zone].api.inimatic.com` rule; right now `ru` resolves to `https://ru.api.inimatic.com`, while the other zones still stay on `https://api.inimatic.com`. The optional `--dev` / `-Dev` flag writes `ENV_TYPE=dev` into `.env`.

### Install from an existing environment

If you already have Python 3.11.9+ and want a manual editable install:

```bash
pip install -e ".[dev]"
adaos --help
```

## Common Commands

```bash
adaos --help
adaos api serve --host 127.0.0.1 --port 8777
adaos skill list
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city":"Berlin"}'
```

Local API runtime notes:

- `adaos api serve` starts the local hub/runtime HTTP API directly, without the slot supervisor.
- In development, an explicit `--port` is persisted into `.adaos/node.yaml` as `local_api_url`, and the next `adaos api serve` reuses it.
- `8777` and `8778` are the normal browser-discoverable local hub ports.
- Use a different port such as `8779` when you do not want `https://myinimatic.web.app/` to auto-attach to the local runtime and prefer it to stay on Root.
- Supervisor-managed runtime mode is separate: it owns port `8776`, manages slots, and sets `ADAOS_SUPERVISOR_ENABLED=1`.
- Development runtimes with `ENV_TYPE=dev` do not follow hub/root core-update signals by default; set `ADAOS_DEV_ALLOW_CORE_UPDATE=1` only when deliberately testing the update machinery.
- Use the same hub-to-root NATS-over-WS defaults on Windows and Linux. Leave `HUB_NATS_WS_PROXY` unset or set to `auto`; use `HUB_NATS_WS_PROXY=none` only when deliberately diagnosing direct-route failures.

Health endpoints are available once the API is running:

```bash
curl -i http://127.0.0.1:8777/health/live
curl -i http://127.0.0.1:8777/health/ready
```

Backend versioning:

- Current backend base version: `0.1.0`
- CI/CD publishes backend builds with auto-incremented patch version in the form `MAJOR.MINOR.PATCH`, where `PATCH` is derived from the backend git history during image build.
- The deployed backend exposes `version`, `build_date`, and `commit` via `https://ru.api.inimatic.com/healthz` and `/v1/health`.

## Documentation

- Project docs: `docs/`
- Quick start: `docs/quickstart.md`
- Architecture overview: `docs/architecture/overview.md`
- Skill activation and scenario binding: `docs/architecture/skill-activation-and-scenario-binding.md`
- Device access architecture: `docs/architecture/device-access-and-browsers.md`
- Device access transition roadmap: `docs/architecture/device-access-roadmap.md`
- ABI schemas: `src/adaos/abi/`
- CLI reference: `docs/reference/cli.md`
- SDK docs: `docs/sdk/`
- English docs: `docs/en/`

## Scope and Status

AdaOS is an evolving platform. This repository focuses on the developer-facing and runtime foundations needed to build, test, and operate skills, scenarios, and node services.

Some ecosystem layers may evolve separately from this repository, including hosted or managed infrastructure, trust and publication workflows, operator tooling, and broader distribution services. The current repository should be understood as the core development and runtime foundation rather than the entirety of the AdaOS ecosystem.

## Development

### Run tests

```bash
pytest
```

### Build documentation locally

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

### Project layout

```text
src/adaos/        Core package, apps, services, SDK, templates
tests/            Test suite
docs/             Documentation source
tools/            Bootstrap and diagnostic scripts
```

## Contributing

Contributions are welcome in areas such as:

- core runtime improvements
- skills and scenario tooling
- documentation
- developer workflows
- tests and diagnostics
- extension contracts and platform-facing specifications

For larger architectural changes, please open an issue or discussion first so proposals can be aligned with the platform direction.

## License

MIT. See [LICENSE](LICENSE).
