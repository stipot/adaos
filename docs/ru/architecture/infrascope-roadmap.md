# Infrascope Roadmap

Эта дорожная карта разбивает `Infrascope` на последовательные итерации, которые можно реализовывать поверх текущего кода AdaOS.

Архитектура зафиксирована в [Infrascope](infrascope.md). Здесь описаны порядок, deliverables и критерии готовности.

## Текущий статус

- локальный draft `.adaos/workspace/scenarios/infrascope/*` подготовлен и провалидирован как реальный desktop scenario; публикация идёт через `adaos scenario push`, а не прямым git-коммитом
- shell сценария уже закрывает `overview`, `inventory` и unified inspector поверх `data/infrascope/*`
- `node_config` закрепляет canonical subnet identity в `subnet.bootstrap_id`; при восстановлении конфига сертификат остаётся главным repair-source, а `nats.user` используется только как аварийный fallback
- `Infrascope` snapshot теперь сохраняет usable partial payload и одновременно явно помечает ошибки/partial state в проекции
- topology/runtime/resource modes и дальнейшая полировка operator UX остаются отдельными инкрементами

## Принципы последовательности

1. Сначала контракты, потом экраны.
   Каноническая модель объекта и projections должны появиться раньше специальных UI-виджетов.
2. Переиспользовать текущие runtime producers.
   Node, runtime, Yjs, workspaces и observe-сервисы должны стать источниками нового control plane.
3. Польза для оператора важнее визуальной сложности.
   `overview`, `inventory` и inspector должны появиться раньше амбициозного полного графа.
4. LLM-совместимость с первого дня.
   Каждая итерация должна улучшать машиночитаемый контекст, а не откладывать его на потом.
5. Governance — часть объектной модели.
   Ownership, visibility и policy нельзя переносить на этап после сборки интерфейса.

## Checkpoint: Subnet Shared State

Ниже зафиксирован отдельный delivery track для subnet operational shared state. Он поддерживает и `Infrascope`, и SDK/LLM planning, и не должен расходиться с общим control-plane vocabulary.

### Цель

Сделать subnet SQLite durable read model для operational state нод, а не только каталогом heartbeat/capacity.

### Checklist

- [x] отделить collaborative Yjs state от operational subnet state на уровне архитектуры
- [x] зафиксировать `subnet directory -> system model -> planning` как основной путь потребления
- [x] завести durable runtime projection member-ноды в subnet SQLite
- [x] реплицировать runtime projection с hub на members вместе с subnet snapshot
- [x] поднять runtime projection в canonical node/neighborhood projections
- [x] использовать persisted runtime projection в reliability для linkless/aging members
- [x] довести capacity-репликацию до симметрии по `io`, `skills` и `scenarios`
- [x] добавить task-packet friendly subnet planning projection поверх durable read model
- [ ] убрать planning-зависимость от raw `node.yaml` и transport-specific payloads там, где есть canonical projection
- [x] формализовать freshness/staleness contract для persisted subnet runtime projection

### Ближайшие инкременты

1. `planning dependency cleanup`
   Убрать оставшиеся planning-зависимости от raw `node.yaml` и transport-specific payloads там, где уже есть canonical projection.
2. `projection hardening`
   Довести поверх одного durable read model rollout, media/direct-path и control-result semantics до общего, непротиворечивого контракта.
3. `operator adoption`
   Подтянуть UI и SDK consumers к новому subnet planning context без обходов через старые runtime payloads.

Текущий статус этого трека:

- SDK/API получили явный `subnet planning context` поверх canonical projections
- `Infrascope` inspector начал публиковать `subnet_planning` как отдельный operator-facing payload
- bootstrap-origin subnet identity теперь хранится отдельно от transport hints и не должен деградировать до следов из `nats.user`, пока есть canonical bootstrap source
- полный cleanup ещё не завершён: в кодовой базе остаются legacy места, где используются raw bootstrap/runtime payloads вне canonical surface

## Checkpoint: Node.yaml Transition

Target architecture for the node bootstrap document and adjacent state layers:

- [x] keep `node.yaml` as the minimal bootstrap/identity document for `zone_id`, `node_id`, `subnet_id`, `role`, `root.base_url`, and key/cert paths
- [x] move runtime `hub_url` and node token out of `node.yaml` into `state/node_runtime.json`
- [x] move NATS runtime credentials (`ws_url`, `user`, `pass`) out of `node.yaml` into `state/node_runtime.json`
- [x] move subnet alias metadata out of `node.yaml` into SQLite durable state
- [x] move durable root auth cache / `root_state` out of `node.yaml` into SQLite durable state
- [x] move local capacity projection out of `node.yaml` into the SQLite-backed subnet directory read model
- [x] prune legacy `node.yaml` `nats` / `capacity` payloads during migration once the new durable state is seeded
- [x] migrate bootstrap and diagnostics tooling (`tools/bootstrap*`, `tools/diag_*`) to prefer runtime state with legacy `node.yaml` fallback
- [x] finish the remaining legacy readers/writers outside the Python core runtime (`external integrations`, examples, and user-facing docs now treat `node.yaml` as bootstrap/static config only)

Operational split after this checkpoint:

- `node.yaml`: bootstrap identity and static paths only
- `state/*.json`: runtime snapshots such as supervisor/runtime/update state and dynamic local runtime connection data
- SQLite durable state: structured long-lived state such as local capacity, root auth cache, and subnet alias metadata

Checkpoint status:

- [x] functional transition is complete for the Python runtime, bootstrap scripts, diagnostics, and user-facing docs
- [x] external integrations that still look at `node.yaml` do so only for bootstrap/static config, not for runtime transport credentials or mutable projections
- [ ] optional follow-up cleanup may still remove compatibility fallbacks from scripts/tools once legacy environments no longer rely on them

## Опорные сценарии

Каждая итерация должна улучшать хотя бы один из этих сквозных сценариев:

1. Разобраться, почему перестал работать сценарий.
2. Определить, вызвана ли поломка автоматизации root, hub, member, browser или device.
3. Показать impact перед перезапуском hub или обновлением skill.
4. Объяснить, какой объект превысил LLM или external-service квоту.
5. Подготовить безопасный, policy-aware context packet для LLM assistant.

## Companion Track: Root MCP Foundation

`Infrascope` — это human-facing track control plane. Параллельно AdaOS должен заложить [Root MCP Foundation](root-mcp-foundation.md) на `root` как agent-facing companion layer.

Этот companion track важен потому, что:

- vocabulary Phase 0 должна быть общей для web, SDK и будущих MCP surfaces
- projections и task packets из Phase 1 должны переиспользоваться будущей `MCP Development Surface`
- Phases 2-4 должны рассматривать `infra_access_skill` как first-class operational skill, чьи web UI и observability можно инспектировать в `Infrascope`
- Phases 5-6 должны свести human и agent control surfaces к общему policy, audit и action vocabulary

Ожидаемая последовательность Root MCP Foundation:

1. `Architectural fixation`: терминология, root-first boundaries, split development/operations, managed target model, `infra_access_skill` и operational event model
2. `Foundation skeleton`: minimal root MCP entrypoint, typed envelopes, tool-contract registry и audit primitives
3. `MCP-to-SDK base`: machine-readable SDK и contract descriptors для LLM-assisted development
4. `Test-hub operational pilot`: первый managed target, `infra_access_skill`, сначала read-only diagnostics, затем controlled writes с observability
5. `Convergence`: shared objects, actions, event history и review flows для web и MCP surfaces

`Infrascope` не должен ждать завершения всего этого трека, но и расходиться с ним тоже не должен.

## Итерация 0: Канонический словарь

### Цель

Создать стабильный контракт объекта и общий словарь статусов, связей и действий.

### Deliverables

- определить `CanonicalObject` и реестр поддерживаемых `kind`
- нормализовать status и health facets для root, hub, member, browser, device, skill, scenario, runtime и quota
- зафиксировать relation kinds вроде `parent`, `depends_on`, `hosted_on`, `connected_to`, `uses` и `affects`
- определить формальные action descriptors с риском и metadata по правам

### Точки опоры

- `src/adaos/services/reliability.py`
- `src/adaos/services/node_config.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/user/profile.py`
- `src/adaos/apps/api/node_api.py`
- `src/adaos/apps/api/skills.py`
- `src/adaos/apps/api/scenarios.py`

### Готово, когда

- каждый базовый тип объекта можно представить через `id`, `kind`, `title`, `status`, `relations` и `governance`
- одинаковые статусные слова значат одно и то же в node- и runtime-API

### Текущий срез реализации

Первый кодовый срез для Iteration 0 намеренно узкий:

- добавить общий canonical vocabulary и object envelope в `src/adaos/services/system_model/*`
- добавить adapter functions для текущих форм node, skill, scenario, profile и workspace
- добавить SDK entry points для canonical self, skill, scenario и reliability projection objects и оставить API тонким фасадом
- сначала проверить словарь и адаптеры точечными unit tests, а уже потом расширять покрытие API

Текущий checkpoint распространяет тот же envelope на selected reliability snapshots, добавляет явные реестры `kind` и `relation`, а также расширяет canonical coverage на workspace, profile, browser-session, device, capacity и quota objects. Он также добавляет shared governance defaults для SDK-facing objects, protocol traffic-budget quota projections и subnet-neighborhood projection поверх directory snapshots и nearby root/control connections. Local inventory projection теперь объединяет локальные catalog objects с selected reliability-derived root/quota objects для SDK/LLM use. Рост external API остаётся намеренно узким: current raw responses не ломаются, а более широкий surface остаётся SDK-first, при этом HTTP-фасады ограничены aggregate projections.

## Итерация 1: Projection Layer

### Цель

Построить машиночитаемые projections, которые будут кормить и UI, и LLM workflows.

### Deliverables

- сервис object projection
- сервис topology projection
- narrative projection для summary и operator focus
- action projection для формальных безопасных действий
- task packet builder для LLM и automation

### Точки опоры

- `src/adaos/services/scenario/projection_service.py`
- `src/adaos/services/scenario/projection_registry.py`
- `src/adaos/apps/api/observe_api.py`
- `src/adaos/apps/api/subnet_api.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/eventbus.py`

### Готово, когда

- выбранный объект можно получить как `object`, `neighborhood` и `task_packet`
- UI и LLM больше не зависят от отдельных сериализаторов под каждый тип объекта

## Итерация 2: Operator Workspace MVP

### Цель

Поставить первую реально полезную operator-facing версию control plane.

### Deliverables

- `overview` с health strip, active incidents, quota summary, active runtimes и recent changes
- `inventory` tabs для hubs, members, browsers, devices, skills и scenarios
- единый правый `object inspector`
- drill-down от incident к объекту без потери контекста

### Точки опоры

- `src/adaos/services/workspaces/*`
- `src/adaos/services/yjs/*`
- `src/adaos/services/scenario/webspace_runtime.py`
- текущие browser-facing workspace shells, уже работающие с Yjs state

### Готово, когда

- оператор может понять, где именно болит подсеть, меньше чем за пять секунд
- детали объекта открываются в контексте, а не как несвязанные страницы

## Итерация 3: Topology и Impact Mode

### Цель

Сделать видимыми связи, пути деградации и последствия изменений.

### Deliverables

- layered topology map для режимов `physical`, `connectivity`, `runtime`, `resource` и `capability`
- neighborhood isolation и graph filters
- dependency graph для выбранного объекта
- impact preview перед `restart`, `reconnect`, `update` или `disable`
- карта version и sync drift

### Точки опоры

- `src/adaos/services/subnet/link_manager.py`
- `src/adaos/services/subnet/link_client.py`
- `src/adaos/services/subnet/runtime.py`
- `src/adaos/services/root/service.py`
- `src/adaos/services/reliability.py`

### Готово, когда

- оператор видит не только упавший узел, но и путь отказа
- потенциально disruptive действия показывают затронутые объекты до выполнения

## Итерация 4: Runtime, Incidents и Resources

### Цель

Превратить `Infrascope` из статичной инвентаризации в полноценный операционный cockpit.

### Deliverables

- runtime timeline для выполнений scenarios и skills
- поверхности для failed runs, retries и event backlog
- incident model с impact radius и root-cause candidates
- views по ресурсам и квотам, привязанным к owning objects
- trace от browser или user action до scenario, skill и device effect

### Точки опоры

- `src/adaos/services/scenario/workflow_runtime.py`
- `src/adaos/services/skill/runtime.py`
- `src/adaos/services/skill/service_supervisor_runtime.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/resources/*`
- `src/adaos/services/chat_io/telemetry.py`

### Готово, когда

- runtime failure можно пройти по execution и event flow
- всплеск квоты можно привязать к конкретному scenario, skill или subnet object

## Итерация 5: Profile-Aware Overlays

### Цель

Поддержать нескольких человеческих и машинных потребителей поверх одних и тех же canonical objects.

### Deliverables

- overlays для identities, profiles, workspaces, roles и ownership
- profile-aware стартовые страницы и дефолтные фильтры
- представления объекта для `operator`, `developer`, `household` и `llm`
- ownership и review metadata на изменениях и инцидентах

### Точки опоры

- `src/adaos/services/user/profile.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/policy/*`
- Yjs overlays, уже присутствующие в workspace manifest

### Готово, когда

- один и тот же hub рендерится по-разному для admin, household user и LLM, но сохраняет общий `id` и `relations`
- видимость и действия задаются тем же governance layer, который формирует UI

## Итерация 6: LLM-Assisted Operations и Change Loop

### Цель

Перевести LLM из роли пассивного наблюдателя в управляемого участника диагностики и контролируемых изменений.

### Deliverables

- task packets с `desired_state`, `actual_state`, `gap` и `constraints`
- change proposals, ссылающиеся на формальные actions, а не на произвольные текстовые инструкции
- dry-run и impact simulation перед отправкой предложения
- review и approval flow для изменений, предложенных LLM
- recommendation и anomaly layers поверх структурированных projections
- LLM-assisted conflict resolution для git/workspace merge conflicts: нода
  обнаруживает конфликт, через root запрашивает LLM-резолв, передает
  ограниченный набор артефактов и принимает результат только как проверяемый
  patch/action proposal

### Перспективная задача: LLM Conflict Resolution

Для workflows вроде `skill push`, обновления workspace registry и других
управляемых git-операций нужен отдельный conflict-resolution контур. Если
нода обнаруживает merge/rebase conflict, она не должна пытаться бесконтрольно
чинить рабочее дерево локальной эвристикой. Целевой flow:

1. Нода фиксирует конфликт как runtime incident и собирает `conflict_pack`:
   `git status`, список conflicted paths, base/ours/theirs версии, фрагменты с
   conflict markers, commit metadata, intended operation и policy context.
2. Нода отправляет `conflict_pack` через root-managed LLM endpoint, не раскрывая
   секреты, токены и персональные данные из workspace.
3. LLM возвращает не произвольную команду shell, а структурированный
   `resolution_proposal`: объяснение, patch или file replacements, confidence,
   affected paths, risk notes и список проверок.
4. Нода применяет proposal во временном/изолированном рабочем дереве, запускает
   JSON/schema/tests/dry-run проверки и строит impact summary.
5. Только после успешной валидации и, где требуется, operator approval,
   изменения попадают в реальный rebase/merge flow через существующие git
   адаптеры.

Первые безопасные кандидаты: конфликты `registry.json` при `skill push` /
`scenario push`, где LLM может сопоставить remote additions с локальным bump
версии, сохранить оба набора изменений и обновить metadata без потери entries.

### Точки опоры

- projection services из Итерации 1
- governance и ownership overlays из Итерации 5
- текущие CLI- и API-control paths в `src/adaos/apps/cli`, `src/adaos/apps/api` и `src/adaos/sdk/manage/*`

### Готово, когда

- LLM может объяснить проблему, предложить изменение и передать reviewable action plan без обхода policy
- операторы могут принять, отклонить или доработать предложение внутри того же control plane

## Группировка по релизам

Итерации можно собрать в более крупные пользовательские релизы:

- `v1 / operational MVP`: Итерации 0-2
- `v2 / topology and runtime control`: Итерации 3-4
- `v3 / profile-aware and LLM-native control plane`: Итерации 5-6

## Рекомендуемый порядок реализации

1. Нормализовать object envelopes в существующих API- и service-ответах.
2. Ввести projection services и builders для neighborhood и task packet.
3. Построить `overview`, `inventory` и unified inspector поверх этих projections.
4. Добавить layered topology и impact views.
5. Добавить runtime traces, incidents и resource attribution.
6. Добавить profile overlays, ownership и review/change workflows.

## Практический первый backlog

Если начинать реализацию прямо сейчас, первый backlog стоит держать узким:

- определить `CanonicalObject` и `ActionDescriptor`
- собрать projection adapters для `root`, `hub`, `member`, `skill` и `scenario`
- отдать один `overview` endpoint, собранный из текущих health и runtime signals
- отдать один `object inspector` endpoint с object, incidents, recent events и actions
- добавить один `task_packet` endpoint для выбранного объекта и цели задачи

Этого уже достаточно, чтобы начать поставлять `Infrascope`, не дожидаясь полного графа и полной многопользовательской полировки.
