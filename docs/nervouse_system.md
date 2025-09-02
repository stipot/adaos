# AdaOS Nervous System – README (draft)

## Idea

AdaOS is designed as a distributed **nervous system**:

* **Sensors** receive events (audio, http, timers).
* **Reflexes** process them deterministically and fast.
* **Effectors** perform actions (tts, notifications, leds).
* **Cortex** integrates context and scenarios.
* **Volitional module** manages goals and priorities.
* **Reality model** keeps norms and short-term predictions.
* **Attention** shifts resources toward the *unpredictable*.

## Key principles

* **Norm + delta**: every device is described by a base bundle (NB) and a patch (DD).
* **Masks/projections**: data exposed per goal (perf, policy, llm).
* **Links**: details via references; top-level descriptors stay compact.
* **Latent channel**: events/commands exchanged in discrete latent space (codebooks); JSON remains fallback.
* **Network adapters**: hub ↔ edge use a unified ABI; adapters translate into device-specific dialects (vqvae.low, vqvae.high, gguf…).
* **QoS/RT**: explicit traffic classes, deadlines, priorities, backpressure.
* **Security**: scopes, signatures, attestation, two-phase actions for llm-driven calls.
* **Observability**: trace-id, health, perplexity/residual metrics for latent, skill watchdogs.
* **Lifecycle**: version pinning, conformance-suite, migrations of norms/codebooks, skill marketplace.

## Core structure

* `abi/` — JSON Schemas: dcd, nb, masks, latent, lrpc.
* `spi/adapter.py` — interface for latent adapters.
* `protocol/` — http/mqtt/bus implementations.
* `policy/` — scopes, privacy tags, allow/deny rules.
* `codebook/registry/` — codebook formats, bridges.
* `tests/conformance/` — reference test cases.

## What’s missing (minimum for MVP+)

* Time sync and QoS classes.
* Error taxonomy and unified reply format.
* Reflex-FSM layer for critical reflex loops.
* Policy-as-code and two-phase actions.
* Latent monitoring (perplexity, residual, fallback rate).
* Conflict resolver for effectors (e.g. TTS arbitration).
* Data lifecycle (ttl, retention, privacy tags).

---

## Two MVP cases

### Case 1: **Wakeword → Say (sensorimotor loop)**

* Sensor: microphone + wakeword.
* Reflex: `event.wake`.
* Cortex: scenario `wake → tts.speak("hello")`.
* Effector: TTS.
* DCD: nb (low/high), patch = “tts offline”.
* Masks: `perf`, `llm`.
* Latent-RPC: `tts.speak` via low/high codebooks.
* Outcome: end-to-end verification including fallback.

### Case 2: **Health telemetry (latent-stream)**

* Sensor: system metrics (cpu, ram, temp).
* Publication: `health.tick` every second.
* Hub: latent-stream decoder (cb://health/v3/low, high).
* QoS: predictive mode, tol ±1°C, latency p95±10ms.
* Residual: if error > tol → fallback JSON packet.
* Monitoring: perplexity, residual share.
* Outcome: measurable bitrate/energy savings.

---

📌 Next steps:

1. Finalize schemas.
2. Implement both cases (wakeword+say, health telemetry).
3. Conformance tests.
4. Baseline metrics (latency, bitrate, success rate).
