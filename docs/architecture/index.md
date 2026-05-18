# Architecture

AdaOS is built as a local-first runtime with a layered Python codebase and a small control surface:

- the CLI builds and uses a shared `AgentContext`
- the FastAPI server exposes the same runtime over HTTP
- services manage skills, scenarios, node state, Yjs webspaces, and runtime lifecycle
- adapters isolate filesystem, database, git, audio, secret, and integration-specific IO

## Main runtime building blocks

- `src/adaos/apps`: CLI, API server, launchers, and process entry points
- `src/adaos/services`: orchestration and runtime logic
- `src/adaos/sdk`: public helpers for skills, scenarios, data access, and decorators
- `src/adaos/adapters`: filesystem, database, git, audio, secrets, and SDK bridge implementations
- `src/adaos/ports`: contracts for infrastructure-facing behavior
- `src/adaos/domain`: core types and registries

## Runtime model

In the current implementation:

- a node can operate as `hub` or `member`
- the local API exposes node, skill, scenario, observe, subnet, join, and service endpoints
- service-type skills are managed through a supervisor and health-aware status API
- Yjs-backed webspaces provide synchronized scenario and desktop state
- autostart and core-update flows are integrated with the runtime lifecycle

The pages in this section primarily summarize the implemented architecture.
When a page is explicitly labeled as a roadmap or target-state design, it captures planned control-plane evolution that should stay compatible with the current runtime.

Current target-state control-plane extensions are documented in:

- [AdaOS Product Terminology](product-terminology.md): product-facing terms and compatibility rules for Assistant, Webspace, Application, Device, Agent, Skill, Widget/Panel, Interface, and Catalog
- [Infrascope](infrascope.md): human-facing control-plane architecture over the canonical system model
- [UI Addressing](ui-addressing.md): target typed ref vocabulary for browser-facing state, projections, domain identity, and actions
- [Named Entities and Canonical Naming](named-entities.md): target architecture and roadmap for display names, localized labels, observed names, aliases, canonical refs, and NLU entity canonicalization
- [Web UI Architecture](web-ui-architecture.md): target stable browser-client architecture over `webui.v1`, semantic views, typed actions, Taiga renderers, and Ionic shell concerns
- [Operational Event Model](operational-event-model.md): target event, demand, lifecycle, and Yjs materialization contract for browser-facing projections
- [Operational Event Model Reference Plan](operational-event-model-reference-plan.md): top-level coverage gates, required contract shapes, review checklist, and completion definition for implementing the event model correctly
- [Operational Event Model Roadmap](operational-event-model-roadmap.md): master implementation order across communication, runtime contracts, Yjs shape, client adapters, platform emitters, and skill pilots
- [Projection Subscription Roadmap](projection-subscription-roadmap.md): priority checklist for moving skills and scenarios to demand-driven per-webspace projections
- [Skill Projection and Stream Boundary](skill-projection-and-stream-boundary.md): current stabilization status and target roadmap for skill-owned Yjs projections, stream data, node-aware addressing, and temporary per-skill bridges
- [Skill Projection Runtime SDK](skill-projection-runtime-sdk.md): target SDK/core rails for projection slots, stream receivers, dirty routing, fingerprinted Yjs writes, and skill migration checklists
- [Root MCP Foundation](root-mcp-foundation.md): root-hosted agent-facing foundation for future MCP development and operations surfaces
- [Root MCP Roadmap](root-mcp-roadmap.md): sequencing for planes, descriptor cache, session leases, and companion slices such as `ProfileOps`
- [AdaOS Supervisor](adaos-supervisor.md): local always-on process and update supervision authority above the restartable runtime
- [Runtime Guarding](runtime-guarding.md): target shared guard architecture and roadmap for memory, CPU, Yjs pressure, HTTP health, skill overload, quarantine, supervisor hard safety, and diagnostic snapshots
- [Member-Hub Connectivity](member-hub-connectivity.md): target control-plane architecture for member join, member-hub lifecycle ownership, restart-aware health semantics, and QR onboarding
- [Device Access and Browsers](device-access-and-browsers.md): target architecture for durable device identity, browser and member access policy, device-centric desktop UX, and reusable access management surfaces
- [Device Access Roadmap](device-access-roadmap.md): recommended migration order from bootstrap-only links and ad hoc UI actions to a shared access-link control plane
- [Semantic State Plane](semantic-state-plane.md): target kernel architecture for separating connectivity, shared-state sync freshness, and Yjs pressure governance without adding redundant status entities
- [Webspace Scenario Pointer/Projection Roadmap](webspace-scenario-pointer-projection-roadmap.md): target architecture and migration checklist for moving scenario switching from materialize-and-copy to pointer-first semantic rebuild
- [Skill Assets and Icons Roadmap](skill-assets-and-icons-roadmap.md): roadmap for loading skill-owned icons and resources without recompiling the browser client
