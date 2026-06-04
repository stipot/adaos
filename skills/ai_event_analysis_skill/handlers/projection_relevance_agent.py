from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Mapping


PROJECTION_KEYS = [
    "overview",
    "inventory:members",
    "inventory:skills",
    "inspector:hub-1",
    "topology:hub-1",
    "platform:notifications",
    "platform:diagnostics",
]

EVENT_TYPES = [
    "browser.session.changed",
    "subnet.member.link.down",
    "skill.installed",
    "core.update.status",
    "entity.alias.conflict.detected",
    "projection.refresh.failed",
    "browser.transport.not_ready",
]

EVENT_CLASSES = {
    "browser.session.changed": "runtime_signal",
    "subnet.member.link.down": "domain_fact",
    "skill.installed": "domain_fact",
    "core.update.status": "platform_fact",
    "entity.alias.conflict.detected": "language_diagnostic",
    "projection.refresh.failed": "projection_lifecycle",
    "browser.transport.not_ready": "transport_degradation",
}

BROAD_RULE_TARGETS = {
    "browser.session.changed": ["overview", "platform:diagnostics"],
    "subnet.member.link.down": ["topology:hub-1", "inventory:members", "platform:diagnostics"],
    "skill.installed": ["inventory:skills", "overview"],
    "core.update.status": ["overview", "platform:diagnostics", "platform:notifications"],
    "entity.alias.conflict.detected": ["platform:notifications", "inspector:hub-1"],
    "projection.refresh.failed": ["platform:notifications", "platform:diagnostics"],
    "browser.transport.not_ready": ["overview", "platform:diagnostics", "platform:notifications"],
}

CRITICAL_PROJECTIONS = {"platform:diagnostics", "platform:notifications"}
STATUS_VALUES = ["ready", "stale", "refreshing", "error"]
CONSUMER_KINDS = ["page", "widget", "modal"]


@dataclass
class RelevanceModel:
    projection_keys: list[str]
    weights: dict[str, dict[str, float]]
    threshold: float = 0.5

    def predict_scores(self, features: Mapping[str, float]) -> dict[str, float]:
        return {
            projection_key: _sigmoid(_dot(weights, features))
            for projection_key, weights in self.weights.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "single_layer_sigmoid_multilabel",
            "projection_keys": list(self.projection_keys),
            "threshold": self.threshold,
            "weight_count": sum(len(weights) for weights in self.weights.values()),
        }


def generate_projection_relevance_dataset(sample_count: int = 420, *, seed: int = 42) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    for index in range(max(1, int(sample_count))):
        event_type = EVENT_TYPES[index % len(EVENT_TYPES)] if index < len(EVENT_TYPES) else rng.choice(EVENT_TYPES)
        active_subscriptions = _sample_active_subscriptions(rng, event_type)
        projection_statuses = {
            projection_key: rng.choices(STATUS_VALUES, weights=[0.62, 0.18, 0.12, 0.08], k=1)[0]
            for projection_key in PROJECTION_KEYS
        }
        consumer_kinds = {projection_key: rng.choice(CONSUMER_KINDS) for projection_key in PROJECTION_KEYS}
        pinned_flags = {
            projection_key: bool(projection_key in active_subscriptions and rng.random() < 0.35)
            for projection_key in PROJECTION_KEYS
        }
        event = {
            "event_type": event_type,
            "event_class": EVENT_CLASSES[event_type],
            "entity_ref": _entity_ref_for_event(event_type, rng),
            "node_id": rng.choice(["node:hub-1", "node:member-1", "node:member-2"]),
            "webspace_id": rng.choice(["desktop", "operations", "lab"]),
            "active_subscriptions": active_subscriptions,
            "projection_statuses": projection_statuses,
            "consumer_kinds": consumer_kinds,
            "pinned_flags": pinned_flags,
        }
        label = _oracle_label(event)
        records.append({"sample_id": f"synthetic:{seed}:{index}", "event": event, "label": label})
    return records


def train_projection_relevance_agent(
    *,
    sample_count: int = 420,
    seed: int = 42,
    epochs: int = 90,
    learning_rate: float = 0.18,
    threshold: float = 0.5,
) -> dict[str, Any]:
    sample_count = _bounded_int(sample_count, default=420, low=64, high=5000)
    epochs = _bounded_int(epochs, default=90, low=1, high=400)
    learning_rate = min(max(float(learning_rate or 0.18), 0.01), 1.0)
    threshold = min(max(float(threshold or 0.5), 0.05), 0.95)

    dataset = generate_projection_relevance_dataset(sample_count, seed=seed)
    split_at = max(1, int(len(dataset) * 0.72))
    train_rows = dataset[:split_at]
    eval_rows = dataset[split_at:] or dataset[:]
    model = _train_model(train_rows, epochs=epochs, learning_rate=learning_rate, threshold=threshold)

    ml_metrics = evaluate_model(model, eval_rows, threshold=threshold)
    rule_metrics = evaluate_broad_rule_baseline(eval_rows)
    examples = [_prediction_example(model, row, threshold=threshold) for row in eval_rows[:6]]

    return {
        "mode": "projection_relevance_training",
        "model": model.to_dict(),
        "dataset": {
            "kind": "synthetic_projection_relevance",
            "sample_count": len(dataset),
            "train_count": len(train_rows),
            "eval_count": len(eval_rows),
            "seed": seed,
            "projection_count": len(PROJECTION_KEYS),
            "event_type_count": len(EVENT_TYPES),
        },
        "metrics": ml_metrics,
        "rule_baseline": rule_metrics,
        "comparison": {
            "f1_delta": round(ml_metrics["f1"] - rule_metrics["f1"], 4),
            "write_reduction_delta": round(
                ml_metrics["write_reduction_ratio"] - rule_metrics["write_reduction_ratio"], 4
            ),
            "missed_critical_delta": int(
                ml_metrics["missed_critical_projection_total"]
                - rule_metrics["missed_critical_projection_total"]
            ),
        },
        "examples": examples,
    }


def predict_projection_refresh_plan(
    event: Mapping[str, Any] | None = None,
    *,
    model_result: Mapping[str, Any] | None = None,
    threshold: float = 0.5,
) -> dict[str, Any]:
    event_payload = _normalize_event(event)
    if model_result and isinstance(model_result.get("model"), Mapping):
        trained = train_projection_relevance_agent(sample_count=420, seed=42, threshold=threshold)
        model = _train_model(
            generate_projection_relevance_dataset(420, seed=42)[:300],
            epochs=90,
            learning_rate=0.18,
            threshold=float(model_result["model"].get("threshold") or threshold),
        )
    else:
        trained = train_projection_relevance_agent(sample_count=420, seed=42, threshold=threshold)
        model = _train_model(
            generate_projection_relevance_dataset(420, seed=42)[:300],
            epochs=90,
            learning_rate=0.18,
            threshold=threshold,
        )
    features = encode_event_features(event_payload)
    scores = model.predict_scores(features)
    recommended_action = _recommended_actions(event_payload, scores, threshold=threshold)
    affected = [key for key, action in recommended_action.items() if action == "refresh"]
    ranked_refresh_plan = _ranked_refresh_plan(scores, recommended_action)
    return {
        "mode": "projection_relevance_prediction",
        "event": event_payload,
        "affected_projections": affected,
        "stale_projections": [key for key, action in recommended_action.items() if action == "mark_stale"],
        "refresh_priority": {key: round(scores[key], 4) for key in PROJECTION_KEYS},
        "ranked_refresh_plan": ranked_refresh_plan,
        "recommended_action": recommended_action,
        "decision_contract": {
            "agent_role": "advisory_refresh_optimizer",
            "authoritative_components": [
                "event envelope validation",
                "active demand registry",
                "guarded dispatcher",
                "authorization checks",
                "projection lifecycle writer",
            ],
            "blocked_decisions": [
                "grant data access",
                "declare event truth",
                "write projection without demand",
                "execute user command",
            ],
        },
        "guardrail": "model suggests refresh order only; dispatcher and demand checks remain authoritative",
        "training_metrics": trained["metrics"],
    }


def build_projection_relevance_trial(
    event: Mapping[str, Any] | None = None,
    *,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Compare one advisory agent plan with the deterministic rule baseline."""

    normalized = _normalize_event(event)
    prediction = predict_projection_refresh_plan(normalized, threshold=threshold)
    rule_label = _oracle_label(normalized)
    rule_actions = _mapping(rule_label.get("recommended_action"))
    agent_actions = _mapping(prediction.get("recommended_action"))
    scores = _mapping(prediction.get("refresh_priority"))
    active = set(_list_text(normalized.get("active_subscriptions")))
    statuses = _mapping(normalized.get("projection_statuses"))

    agent_refresh = {key for key, action in agent_actions.items() if action == "refresh"}
    rule_refresh = {key for key, action in rule_actions.items() if action == "refresh"}
    missed_refresh = sorted(rule_refresh - agent_refresh)
    extra_refresh = sorted(agent_refresh - rule_refresh)
    missed_critical = sorted((rule_refresh - agent_refresh) & CRITICAL_PROJECTIONS)
    matching_actions = sum(1 for key in PROJECTION_KEYS if agent_actions.get(key) == rule_actions.get(key))

    plan_rows = []
    for projection_key in PROJECTION_KEYS:
        agent_action = str(agent_actions.get(projection_key) or "ignore")
        rule_action = str(rule_actions.get(projection_key) or "ignore")
        if agent_action == rule_action:
            comparison = "match"
        elif rule_action == "refresh" and agent_action != "refresh":
            comparison = "missed_refresh"
        elif agent_action == "refresh" and rule_action != "refresh":
            comparison = "extra_refresh"
        else:
            comparison = "different_advice"
        plan_rows.append(
            {
                "id": projection_key,
                "projection_key": projection_key,
                "demanded": projection_key in active,
                "status": str(statuses.get(projection_key) or "unknown"),
                "score": round(float(scores.get(projection_key) or 0.0), 4),
                "agent_action": agent_action,
                "rule_action": rule_action,
                "comparison": comparison,
                "critical": projection_key in CRITICAL_PROJECTIONS,
            }
        )
    priority_by_action = {"refresh": 0, "mark_stale": 1, "ignore": 2}
    plan_rows.sort(
        key=lambda row: (
            priority_by_action.get(str(row["agent_action"]), 9),
            -float(row["score"]),
            str(row["projection_key"]),
        )
    )

    safety_passed = not missed_critical
    return {
        "mode": "projection_relevance_agent_trial",
        "event": normalized,
        "active_projection_set": sorted(active),
        "agent_plan": {
            "refresh": sorted(agent_refresh),
            "mark_stale": sorted(key for key, action in agent_actions.items() if action == "mark_stale"),
            "ignore": sorted(key for key, action in agent_actions.items() if action == "ignore"),
            "recommended_action": dict(agent_actions),
        },
        "rule_baseline": {
            "broad_targets": _broad_rule_targets(normalized),
            "demanded_refresh": sorted(rule_refresh),
            "recommended_action": dict(rule_actions),
        },
        "comparison": {
            "matched_refreshes": sorted(agent_refresh & rule_refresh),
            "extra_refreshes": extra_refresh,
            "missed_refreshes": missed_refresh,
            "missed_critical_projections": missed_critical,
            "action_agreement_ratio": round(matching_actions / max(len(PROJECTION_KEYS), 1), 4),
        },
        "safety": {
            "passed": safety_passed,
            "missed_critical_projection_total": len(missed_critical),
            "dispatch_applied": False,
            "agent_is_advisory": True,
            "authoritative_next_step": "guarded dispatcher rechecks demand, authorization, and projection lifecycle",
        },
        "plan_rows": plan_rows,
        "prediction": prediction,
        "guardrail": prediction["guardrail"],
    }


def summarize_projection_relevance_agent(
    *,
    sample_count: int = 420,
    seed: int = 42,
    epochs: int = 90,
    threshold: float = 0.5,
) -> dict[str, Any]:
    training = train_projection_relevance_agent(
        sample_count=sample_count,
        seed=seed,
        epochs=epochs,
        threshold=threshold,
    )
    metrics = training["metrics"]
    baseline = training["rule_baseline"]
    comparison = training["comparison"]
    demo_plan = predict_projection_refresh_plan(
        {
            "event_type": "skill.installed",
            "event_class": "domain_fact",
            "entity_ref": "skill:infrascope",
            "node_id": "node:hub-1",
            "webspace_id": "desktop",
            "active_subscriptions": ["overview", "inventory:skills", "platform:notifications"],
            "projection_statuses": {
                "overview": "ready",
                "inventory:skills": "ready",
                "platform:notifications": "ready",
            },
            "consumer_kinds": {
                "overview": "page",
                "inventory:skills": "widget",
                "platform:notifications": "modal",
            },
            "pinned_flags": {"platform:notifications": True},
        },
        threshold=threshold,
    )
    gates = [
        _gate("f1_at_least_0_90", metrics["f1"] >= 0.90, metrics["f1"], ">= 0.90"),
        _gate("beats_rule_f1", comparison["f1_delta"] > 0, comparison["f1_delta"], "> 0"),
        _gate(
            "write_reduction_at_least_0_15",
            metrics["write_reduction_ratio"] >= 0.15,
            metrics["write_reduction_ratio"],
            ">= 0.15",
        ),
        _gate(
            "no_missed_critical_projections",
            metrics["missed_critical_projection_total"] == 0,
            metrics["missed_critical_projection_total"],
            "0",
        ),
        _gate("precision_at_3_at_least_0_55", metrics["precision_at_3"] >= 0.55, metrics["precision_at_3"], ">= 0.55"),
        _gate("recall_at_3_at_least_0_80", metrics["recall_at_3"] >= 0.80, metrics["recall_at_3"], ">= 0.80"),
    ]
    advisory_ready = all(item["passed"] for item in gates)
    metric_rows = [
        _metric_row("F1", metrics["f1"], baseline["f1"], "multi-label projection relevance quality"),
        _metric_row("Precision@3", metrics["precision_at_3"], None, "quality of top refresh candidates"),
        _metric_row("Recall@3", metrics["recall_at_3"], None, "coverage of affected projections in the top plan"),
        _metric_row("Hamming loss", metrics["hamming_loss"], baseline["hamming_loss"], "per-projection label error"),
        _metric_row(
            "Write reduction ratio",
            metrics["write_reduction_ratio"],
            baseline["write_reduction_ratio"],
            "extra refresh recommendations avoided against broad rules",
        ),
        _metric_row(
            "Missed critical projections",
            metrics["missed_critical_projection_total"],
            baseline["missed_critical_projection_total"],
            "safety gate for diagnostics and notifications",
        ),
    ]
    return {
        "mode": "projection_relevance_agent_summary",
        "advisory_ready": advisory_ready,
        "decision": "ready_for_advisory_refresh_planning" if advisory_ready else "needs_more_training_or_review",
        "training": training,
        "metrics": metrics,
        "rule_baseline": baseline,
        "comparison": comparison,
        "gates": gates,
        "metric_rows": metric_rows,
        "demo_refresh_plan": {
            "event_type": demo_plan["event"]["event_type"],
            "affected_projections": demo_plan["affected_projections"],
            "stale_projections": demo_plan["stale_projections"],
            "ranked_refresh_plan": demo_plan["ranked_refresh_plan"],
            "guardrail": demo_plan["guardrail"],
        },
        "agent_boundary": {
            "allowed": [
                "rank projection refresh candidates",
                "recommend refresh / mark_stale / ignore",
                "surface confidence scores",
                "support baseline comparison",
            ],
            "not_allowed": [
                "grant access",
                "change canonical event history",
                "publish sensitive projection data by itself",
                "override guarded dispatcher checks",
            ],
        },
        "limitations": [
            "training data is synthetic and must be replaced or calibrated with reviewed AdaOS logs",
            "the model is advisory and must not become the source of truth",
            "real browser adapter measurements are still required for final performance claims",
        ],
        "next_steps": [
            "collect reviewed real event/subscription logs",
            "compare the same scenarios before and after agent-assisted refresh planning",
            "keep missed critical projections as a blocking safety gate",
        ],
    }


def evaluate_model(model: RelevanceModel, rows: list[Mapping[str, Any]], *, threshold: float = 0.5) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    hamming_errors = 0
    predicted_refresh_total = 0
    broad_refresh_total = 0
    critical_missed = 0
    precision_at_k_sum = 0.0
    recall_at_k_sum = 0.0
    k = 3

    for row in rows:
        event = _normalize_event(row.get("event") if isinstance(row, Mapping) else None)
        labels = set(_label_projections(row))
        scores = model.predict_scores(encode_event_features(event))
        predicted = {key for key, value in scores.items() if value >= threshold}
        broad = set(_broad_rule_targets(event))
        predicted_refresh_total += len(predicted)
        broad_refresh_total += len(broad)
        top_k = {key for key, _value in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]}
        precision_at_k_sum += len(top_k & labels) / max(len(top_k), 1)
        recall_at_k_sum += len(top_k & labels) / max(len(labels), 1)

        for key in PROJECTION_KEYS:
            y = key in labels
            y_hat = key in predicted
            if y and y_hat:
                tp += 1
            elif not y and y_hat:
                fp += 1
            elif y and not y_hat:
                fn += 1
                if key in CRITICAL_PROJECTIONS:
                    critical_missed += 1
            else:
                tn += 1
            if y != y_hat:
                hamming_errors += 1

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    total_labels = max(len(rows) * len(PROJECTION_KEYS), 1)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "precision_at_3": round(_safe_ratio(precision_at_k_sum, len(rows)), 4),
        "recall_at_3": round(_safe_ratio(recall_at_k_sum, len(rows)), 4),
        "hamming_loss": round(hamming_errors / total_labels, 4),
        "predicted_refresh_total": int(predicted_refresh_total),
        "broad_rule_refresh_total": int(broad_refresh_total),
        "write_reduction_ratio": round(1.0 - _safe_ratio(predicted_refresh_total, broad_refresh_total), 4),
        "missed_critical_projection_total": int(critical_missed),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def evaluate_broad_rule_baseline(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    hamming_errors = 0
    predicted_refresh_total = 0
    broad_refresh_total = 0
    critical_missed = 0

    for row in rows:
        event = _normalize_event(row.get("event") if isinstance(row, Mapping) else None)
        labels = set(_label_projections(row))
        predicted = set(_broad_rule_targets(event))
        predicted_refresh_total += len(predicted)
        broad_refresh_total += len(predicted)
        for key in PROJECTION_KEYS:
            y = key in labels
            y_hat = key in predicted
            if y and y_hat:
                tp += 1
            elif not y and y_hat:
                fp += 1
            elif y and not y_hat:
                fn += 1
                if key in CRITICAL_PROJECTIONS:
                    critical_missed += 1
            else:
                tn += 1
            if y != y_hat:
                hamming_errors += 1

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    total_labels = max(len(rows) * len(PROJECTION_KEYS), 1)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "precision_at_3": None,
        "recall_at_3": None,
        "hamming_loss": round(hamming_errors / total_labels, 4),
        "predicted_refresh_total": int(predicted_refresh_total),
        "broad_rule_refresh_total": int(broad_refresh_total),
        "write_reduction_ratio": 0.0,
        "missed_critical_projection_total": int(critical_missed),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def encode_event_features(event: Mapping[str, Any]) -> dict[str, float]:
    normalized = _normalize_event(event)
    features: dict[str, float] = {"bias": 1.0}
    event_type = str(normalized["event_type"])
    event_class = str(normalized["event_class"])
    features[f"event_type={event_type}"] = 1.0
    features[f"event_class={event_class}"] = 1.0
    features[f"node_id={normalized.get('node_id')}"] = 1.0
    active = set(_list_text(normalized.get("active_subscriptions")))
    projection_statuses = _mapping(normalized.get("projection_statuses"))
    consumer_kinds = _mapping(normalized.get("consumer_kinds"))
    pinned_flags = _mapping(normalized.get("pinned_flags"))

    for projection_key in PROJECTION_KEYS:
        if projection_key in active:
            features[f"active={projection_key}"] = 1.0
            features[f"event_active={event_type}|{projection_key}"] = 1.0
        status = str(projection_statuses.get(projection_key) or "unknown")
        features[f"status={projection_key}:{status}"] = 1.0
        consumer_kind = str(consumer_kinds.get(projection_key) or "unknown")
        features[f"consumer={projection_key}:{consumer_kind}"] = 1.0
        if bool(pinned_flags.get(projection_key)):
            features[f"pinned={projection_key}"] = 1.0
    return features


def _train_model(
    rows: list[Mapping[str, Any]],
    *,
    epochs: int,
    learning_rate: float,
    threshold: float,
) -> RelevanceModel:
    weights: dict[str, dict[str, float]] = {key: {} for key in PROJECTION_KEYS}
    for _epoch in range(epochs):
        for row in rows:
            event = _normalize_event(row.get("event") if isinstance(row, Mapping) else None)
            features = encode_event_features(event)
            labels = set(_label_projections(row))
            for projection_key in PROJECTION_KEYS:
                y = 1.0 if projection_key in labels else 0.0
                score = _sigmoid(_dot(weights[projection_key], features))
                error = score - y
                for feature_name, feature_value in features.items():
                    previous = weights[projection_key].get(feature_name, 0.0)
                    weights[projection_key][feature_name] = previous - learning_rate * error * feature_value
    return RelevanceModel(projection_keys=list(PROJECTION_KEYS), weights=weights, threshold=threshold)


def _prediction_example(model: RelevanceModel, row: Mapping[str, Any], *, threshold: float) -> dict[str, Any]:
    event = _normalize_event(row.get("event") if isinstance(row, Mapping) else None)
    scores = model.predict_scores(encode_event_features(event))
    return {
        "event_type": event["event_type"],
        "active_subscriptions": list(event["active_subscriptions"]),
        "expected": _label_projections(row),
        "predicted": [key for key, value in scores.items() if value >= threshold],
        "top_scores": [
            {"projection_key": key, "score": round(value, 4)}
            for key, value in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:3]
        ],
    }


def _sample_active_subscriptions(rng: random.Random, event_type: str) -> list[str]:
    targets = list(BROAD_RULE_TARGETS[event_type])
    active = set(rng.sample(PROJECTION_KEYS, rng.randint(2, min(5, len(PROJECTION_KEYS)))))
    if rng.random() < 0.78:
        active.add(rng.choice(targets))
    if rng.random() < 0.45:
        active.update(rng.sample(targets, rng.randint(1, len(targets))))
    return sorted(active)


def _oracle_label(event: Mapping[str, Any]) -> dict[str, Any]:
    active = set(_list_text(event.get("active_subscriptions")))
    targets = set(_broad_rule_targets(event))
    refresh = sorted(active & targets)
    projection_statuses = _mapping(event.get("projection_statuses"))
    recommended_action = {}
    refresh_priority = {}
    for projection_key in PROJECTION_KEYS:
        if projection_key in refresh:
            recommended_action[projection_key] = "refresh"
            refresh_priority[projection_key] = 0.9 if projection_key in CRITICAL_PROJECTIONS else 0.72
        elif projection_key in targets and projection_statuses.get(projection_key) == "ready":
            recommended_action[projection_key] = "mark_stale"
            refresh_priority[projection_key] = 0.35
        else:
            recommended_action[projection_key] = "ignore"
            refresh_priority[projection_key] = 0.08
    return {
        "affected_projections": refresh,
        "refresh_priority": refresh_priority,
        "recommended_action": recommended_action,
    }


def _recommended_actions(event: Mapping[str, Any], scores: Mapping[str, float], *, threshold: float) -> dict[str, str]:
    active = set(_list_text(event.get("active_subscriptions")))
    projection_statuses = _mapping(event.get("projection_statuses"))
    actions: dict[str, str] = {}
    for projection_key in PROJECTION_KEYS:
        score = float(scores.get(projection_key) or 0.0)
        if score >= threshold and projection_key in active:
            actions[projection_key] = "refresh"
        elif score >= threshold and projection_statuses.get(projection_key) in {"ready", "stale"}:
            actions[projection_key] = "mark_stale"
        else:
            actions[projection_key] = "ignore"
    return actions


def _ranked_refresh_plan(scores: Mapping[str, float], actions: Mapping[str, str]) -> list[dict[str, Any]]:
    priority_by_action = {"refresh": 0, "mark_stale": 1, "ignore": 2}
    rows = [
        {
            "projection_key": projection_key,
            "score": round(float(scores.get(projection_key) or 0.0), 4),
            "recommended_action": str(actions.get(projection_key) or "ignore"),
        }
        for projection_key in PROJECTION_KEYS
    ]
    return sorted(rows, key=lambda row: (priority_by_action.get(row["recommended_action"], 9), -row["score"], row["projection_key"]))


def _gate(name: str, passed: bool, value: Any, target: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "value": value, "target": target}


def _metric_row(name: str, agent_value: Any, baseline_value: Any, interpretation: str) -> dict[str, Any]:
    return {
        "metric": name,
        "agent": agent_value,
        "rule_baseline": baseline_value,
        "interpretation": interpretation,
    }


def _normalize_event(event: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(event or {})
    event_type = str(source.get("event_type") or "skill.installed")
    if event_type not in EVENT_TYPES:
        event_type = "skill.installed"
    active = _list_text(source.get("active_subscriptions")) or ["overview", "inventory:skills"]
    projection_statuses = {
        projection_key: str(_mapping(source.get("projection_statuses")).get(projection_key) or "ready")
        for projection_key in PROJECTION_KEYS
    }
    consumer_kinds = {
        projection_key: str(_mapping(source.get("consumer_kinds")).get(projection_key) or "page")
        for projection_key in PROJECTION_KEYS
    }
    pinned_flags = {
        projection_key: bool(_mapping(source.get("pinned_flags")).get(projection_key))
        for projection_key in PROJECTION_KEYS
    }
    return {
        "event_type": event_type,
        "event_class": str(source.get("event_class") or EVENT_CLASSES[event_type]),
        "entity_ref": str(source.get("entity_ref") or "skill:weather"),
        "node_id": str(source.get("node_id") or "node:hub-1"),
        "webspace_id": str(source.get("webspace_id") or "desktop"),
        "active_subscriptions": sorted(projection_key for projection_key in active if projection_key in PROJECTION_KEYS),
        "projection_statuses": projection_statuses,
        "consumer_kinds": consumer_kinds,
        "pinned_flags": pinned_flags,
    }


def _label_projections(row: Mapping[str, Any]) -> list[str]:
    label = row.get("label") if isinstance(row, Mapping) else None
    if isinstance(label, Mapping):
        return [item for item in _list_text(label.get("affected_projections")) if item in PROJECTION_KEYS]
    return []


def _broad_rule_targets(event: Mapping[str, Any]) -> list[str]:
    event_type = str(event.get("event_type") or "skill.installed")
    return list(BROAD_RULE_TARGETS.get(event_type) or BROAD_RULE_TARGETS["skill.installed"])


def _entity_ref_for_event(event_type: str, rng: random.Random) -> str:
    if event_type == "skill.installed":
        return rng.choice(["skill:weather", "skill:infrascope", "skill:voice"])
    if event_type == "subnet.member.link.down":
        return rng.choice(["node:member-1", "node:member-2"])
    if event_type == "entity.alias.conflict.detected":
        return rng.choice(["entity:room:kitchen", "entity:skill:weather"])
    return rng.choice(["projection:overview", "webspace:desktop", "node:hub-1"])


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return min(max(parsed, low), high)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_text(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dot(weights: Mapping[str, float], features: Mapping[str, float]) -> float:
    return sum(float(weights.get(name) or 0.0) * float(value) for name, value in features.items())


def _sigmoid(value: float) -> float:
    if value >= 35:
        return 1.0
    if value <= -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0
