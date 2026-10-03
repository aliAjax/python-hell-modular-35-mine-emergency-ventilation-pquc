from datetime import datetime

from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    PermissionDenied,
    ReplayConflictError,
    ValidationError,
)


def _parse_recorded_at(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValidationError("recorded_at must be ISO-8601")


def compute_refuge_occupancy(refuges, records):
    """Recheck chamber occupancy by replaying every applied movement in field-time order.

    Movements come from applied offline records (payload type
    ``refuge_movement``); records in any other state (pending/conflict/
    reviewed) are ignored. Returns ``{refuge_id: {capacity, occupied,
    worker_ids, over_capacity, ...}}``.
    """
    movements = []
    for record in records:
        if record["status"] != "applied":
            continue
        payload = record["data"].get("payload") or {}
        if payload.get("type") != "refuge_movement":
            continue
        movements.append(
            (
                _parse_recorded_at(record["data"].get("recorded_at")),
                record["id"],
                payload,
            )
        )
    movements.sort(key=lambda item: (item[0], item[1]))
    chambers = {}
    for refuge in refuges:
        chambers[refuge["id"]] = {
            "refuge_id": refuge["id"],
            "location_code": refuge["data"].get("location_code"),
            "capacity": _number(refuge["data"].get("capacity"), "capacity"),
            "worker_ids": [],
        }
    location = {}
    for _, _, payload in movements:
        chamber = chambers.get(payload.get("refuge_id"))
        if not chamber:
            continue
        worker_id = payload.get("worker_id")
        if payload.get("direction") == "enter":
            if worker_id not in chamber["worker_ids"] and worker_id not in location:
                chamber["worker_ids"].append(worker_id)
                location[worker_id] = payload.get("refuge_id")
        elif payload.get("direction") == "exit" and location.get(worker_id) == payload.get("refuge_id"):
            chamber["worker_ids"].remove(worker_id)
            location.pop(worker_id, None)
    for chamber in chambers.values():
        chamber["occupied"] = len(chamber["worker_ids"])
        chamber["over_capacity"] = chamber["occupied"] > chamber["capacity"]
    return chambers


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _number(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")


def _validate_worker(data):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("worker name is too short")


def _validate_sensor(data):
    gas = _number(data.get("gas_ppm"), "gas_ppm")
    threshold = _number(data.get("threshold_ppm"), "threshold_ppm")
    if gas < 0 or threshold <= 0:
        raise ValidationError("gas readings and thresholds must be positive")
    data["severity"] = "alarm" if gas >= threshold * 1.5 else "warning" if gas >= threshold else "normal"


def _validate_capacity(data, field):
    if _number(data.get(field), field) <= 0:
        raise ValidationError(field + " must be positive")


def _validate_passage(data):
    if _number(data.get("width_m"), "width_m") <= 0:
        raise ValidationError("width_m must be positive")
    if data.get("from_location") == data.get("to_location"):
        raise ValidationError("passage endpoints must differ")


def _validate_incident(data):
    if data.get("severity") not in ("low", "medium", "high", "critical"):
        raise ValidationError("invalid incident severity")


def _validate_task(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("closed",):
        raise ValidationError("task requires an open incident")
    if data.get("task_type") not in ("evacuation", "search", "rescue", "ventilation", "medical", "repair"):
        raise ValidationError("invalid task_type")
    key = data.get("dedupe_key")
    for task in _all(lookup, "task"):
        if task["data"].get("dedupe_key") == key and task["status"] not in ("completed", "cancelled"):
            raise ConflictError("active task already exists for dedupe_key: " + str(key))


def _validate_offline(data):
    if not isinstance(data.get("payload"), dict):
        raise ValidationError("offline payload must be an object")
    _parse_recorded_at(data.get("recorded_at"))


def _sensor_alarm(actor, entity, data, lookup):
    if float(entity["data"].get("gas_ppm", 0)) < float(entity["data"].get("threshold_ppm", 1)):
        raise ValidationError("alarm requires a reading at or above threshold")
    return {"acknowledged_by": actor.user_id}


def _clear_sensor(actor, entity, data, lookup):
    patch = {"severity": "normal"}
    if entity["status"] == "alarm":
        patch["cleared_by"] = actor.user_id
    return patch


def _complete_task(actor, entity, data, lookup):
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")
    return {"completed_by": actor.user_id}


def _restore_ventilation(actor, entity, data, lookup):
    """Before a fan restarts, its area must have no alarm gas and no one left behind."""
    area_code = entity["data"].get("area_code")
    alarming = [
        sensor
        for sensor in _all(lookup, "sensor")
        if sensor["data"].get("location_code") == area_code
        and (sensor["status"] == "alarm" or sensor["data"].get("severity") == "alarm")
    ]
    if alarming:
        raise ConflictError(
            "cannot restore ventilation while gas is alarming in area %s" % area_code
        )
    left_behind = [
        worker
        for worker in _all(lookup, "worker")
        if worker["data"].get("location_code") == area_code
        and worker["status"] in ("active", "missing", "located")
    ]
    if left_behind:
        raise ConflictError(
            "cannot restore ventilation while workers remain in area %s" % area_code
        )
    return {"restored_by": actor.user_id}


def _close_incident(actor, entity, data, lookup):
    if [w for w in _all(lookup, "worker") if w["status"] in ("missing", "located")]:
        raise ConflictError("cannot close incident while workers are missing or located")
    active_tasks = [t for t in _all(lookup, "task") if t["status"] not in ("completed", "cancelled")]
    if active_tasks:
        raise ConflictError("cannot close incident while tasks remain active")
    if [v for v in _all(lookup, "ventilation") if v["status"] != "running"]:
        raise ConflictError("cannot close incident until ventilation is restored")
    occupancy = compute_refuge_occupancy(_all(lookup, "refuge"), _all(lookup, "offline_record"))
    occupied = [c for c in occupancy.values() if c["occupied"] > 0]
    if occupied:
        raise ConflictError(
            "cannot close incident while refuges are occupied: %s"
            % ", ".join(sorted(c["refuge_id"] for c in occupied))
        )
    unresolved = [
        record
        for record in _all(lookup, "offline_record")
        if record["status"] == "conflict"
        and not record["data"].get("conflict_reviewed")
    ]
    if unresolved:
        raise ConflictError(
            "cannot close incident while %d offline record conflict(s) are unreviewed"
            % len(unresolved)
        )
    return {"closed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "workers": "worker", "sensors": "sensor", "ventilations": "ventilation",
        "passages": "passage", "refuges": "refuge", "incidents": "incident",
        "tasks": "task", "offline-records": "offline_record", "offline_records": "offline_record",
    }
    INITIAL_STATUS = {
        "worker": "active", "sensor": "normal", "ventilation": "running",
        "passage": "open", "refuge": "available", "incident": "detected",
        "task": "proposed", "offline_record": "pending",
    }
    TRANSITIONS = {
        "worker": {
            "mark_missing": (("active",), "missing"),
            "locate": (("missing",), "located"),
            "evacuate": (("missing", "located"), "evacuated"),
            "rescue": (("missing", "located"), "rescued"),
            "find_safe": (("missing",), "active"),
            "deactivate": (("active",), "inactive"),
        },
        "sensor": {
            "raise_warning": (("normal",), "warning"),
            "raise_alarm": (("normal", "warning"), "alarm"),
            "clear": (("warning", "alarm"), "normal"),
            "mark_faulty": (("normal", "warning", "alarm"), "faulty"),
            "verify_misread": (("faulty",), "normal"),
        },
        "ventilation": {
            "degrade": (("running",), "degraded"),
            "stop": (("running", "degraded"), "stopped"),
            "restore": (("stopped", "degraded"), "running"),
        },
        "passage": {
            "restrict": (("open",), "restricted"),
            "block": (("open", "restricted"), "blocked"),
            "clear": (("blocked", "restricted"), "open"),
        },
        "refuge": {
            "occupy": (("available",), "occupied"),
            "release": (("occupied",), "available"),
            "maintain": (("available",), "maintenance"),
            "reopen": (("maintenance",), "available"),
        },
        "incident": {
            "begin_evacuation": (("detected",), "evacuating"),
            "search": (("evacuating",), "searching"),
            "stabilize": (("searching",), "stabilizing"),
            "recover": (("stabilizing",), "recovering"),
            "close": (("recovering",), "closed"),
            "reopen": (("closed",), "detected"),
        },
        "task": {
            "assign": (("proposed",), "assigned"),
            "accept": (("assigned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
            "cancel": (("proposed", "assigned", "in_progress"), "cancelled"),
        },
        "offline_record": {
            "review_conflict": (("conflict",), "reviewed"),
        },
    }
    CREATE_REQUIRED = {
        "worker": ("name", "location_code", "team"),
        "sensor": ("location_code", "gas_ppm", "threshold_ppm"),
        "ventilation": ("name", "area_code", "capacity"),
        "passage": ("from_location", "to_location", "width_m"),
        "refuge": ("location_code", "capacity"),
        "incident": ("area_code", "severity", "summary"),
        "task": ("incident_id", "task_type", "target", "dedupe_key"),
        "offline_record": ("source_id", "record_id", "recorded_at", "payload"),
    }
    ACTION_REQUIRED = {
        ("worker", "rescue"): ("incident_id",),
        ("sensor", "mark_faulty"): ("reason",),
        ("ventilation", "restore"): ("tested_at",),
        ("ventilation", "degrade"): ("reason",),
        ("incident", "close"): ("summary",),
        ("task", "complete"): ("result",),
        ("task", "cancel"): ("reason",),
    }
    CREATE_ROLES = {
        "worker": ("admin", "safety", "dispatcher"),
        "sensor": ("admin", "safety", "field"),
        "ventilation": ("admin", "safety"),
        "passage": ("admin", "safety", "field"),
        "refuge": ("admin", "safety"),
        "incident": ("admin", "safety", "dispatcher"),
        "task": ("admin", "dispatcher", "safety"),
        "offline_record": ("admin", "safety", "dispatcher", "field"),
    }
    ROLE_ACTIONS = {
        "mark_missing": ("admin", "safety", "dispatcher"),
        "locate": ("admin", "field", "safety"),
        "evacuate": ("admin", "field", "dispatcher"),
        "rescue": ("admin", "field", "safety"),
        "find_safe": ("admin", "field", "safety"),
        "deactivate": ("admin", "safety"),
        "raise_warning": ("admin", "field", "safety"),
        "raise_alarm": ("admin", "field", "safety"),
        "clear": ("admin", "safety"),
        "mark_faulty": ("admin", "safety"),
        "verify_misread": ("admin", "safety"),
        "degrade": ("admin", "safety"),
        "stop": ("admin", "safety"),
        "restore": ("admin", "safety"),
        "restrict": ("admin", "safety", "field"),
        "block": ("admin", "safety", "field"),
        "clear": ("admin", "safety", "field"),
        "occupy": ("admin", "field", "safety"),
        "release": ("admin", "field", "safety"),
        "maintain": ("admin", "safety"),
        "reopen": ("admin", "safety"),
        "begin_evacuation": ("admin", "safety", "dispatcher"),
        "search": ("admin", "safety", "dispatcher"),
        "stabilize": ("admin", "safety", "dispatcher"),
        "recover": ("admin", "safety", "dispatcher"),
        "close": ("admin", "safety"),
        "assign": ("admin", "dispatcher", "safety"),
        "accept": ("admin", "field", "dispatcher"),
        "complete": ("admin", "field", "dispatcher"),
        "cancel": ("admin", "dispatcher", "safety"),
        "review_conflict": ("admin", "safety", "dispatcher"),
    }
    CUSTOM_CREATE = {
        "worker": lambda a, d, l: _validate_worker(d),
        "sensor": lambda a, d, l: _validate_sensor(d),
        "ventilation": lambda a, d, l: _validate_capacity(d, "capacity"),
        "passage": lambda a, d, l: _validate_passage(d),
        "refuge": lambda a, d, l: _validate_capacity(d, "capacity"),
        "incident": lambda a, d, l: _validate_incident(d),
        "task": lambda a, d, l: _validate_task(d, l),
        "offline_record": lambda a, d, l: _validate_offline(d),
    }
    CUSTOM_TRANSITIONS = {
        ("sensor", "raise_alarm"): _sensor_alarm,
        ("sensor", "clear"): _clear_sensor,
        ("ventilation", "restore"): _restore_ventilation,
        ("incident", "close"): _close_incident,
        ("task", "complete"): _complete_task,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def plan_replay(self, actor, records, lookup):
        """Replay pending field records as one batch in field-time order.

        Records (offline_record entities still in ``pending``) are sorted by
        their own ``recorded_at`` (``seq`` breaks ties), replayed against the
        current center state, and returned as an atomic plan plus the final
        refuge occupancy. If one person's movements contradict each other,
        replay stops and every contradiction is listed instead of partially
        applying the batch.
        """
        candidates = sorted(
            (record for record in records if record["status"] == "pending"),
            key=lambda record: (
                _parse_recorded_at(record["data"].get("recorded_at")),
                record["data"].get("seq") or 0,
                record["id"],
            ),
        )
        refuges = _all(lookup, "refuge")
        workers = _all(lookup, "worker")
        refuge_ids = {refuge["id"] for refuge in refuges}
        worker_ids = {worker["id"] for worker in workers}

        # Replay the already-posted ledger first so earlier batches still hold.
        occupancy = compute_refuge_occupancy(refuges, _all(lookup, "offline_record"))
        inside = {
            refuge_id: set(chamber["worker_ids"])
            for refuge_id, chamber in occupancy.items()
        }
        # worker_id -> refuge_id where the person is currently sheltering
        location = {
            worker_id: refuge_id
            for refuge_id, workers in inside.items()
            for worker_id in workers
        }

        conflicts = []
        actions = []
        record_updates = []
        audits = []
        # entity id -> simulated entity during this batch
        simulated = {}

        def replay_lookup(kind, field, value):
            """Lookup overlay: entities mutated earlier in this batch are visible."""
            rows = lookup(kind, field, value) if lookup else []
            if not simulated:
                return rows
            sims = [entity for entity in simulated.values() if entity["kind"] == kind]
            by_id = {entity["id"]: entity for entity in sims}
            merged_rows = [by_id.get(row["id"], row) for row in rows]
            if field == "*":
                existing = {row["id"] for row in rows}
                merged_rows.extend(entity for entity_id, entity in sims.items() if entity_id not in existing)
            return merged_rows

        def add_conflict(record, code, message, record_ids=None):
            conflicts.append(
                {
                    "code": code,
                    "record_id": record["id"],
                    "source_id": record["data"].get("source_id"),
                    "recorded_at": record["data"].get("recorded_at"),
                    "message": message,
                    "related": record_ids or [],
                }
            )

        def current_entity(kind, entity_id):
            if entity_id in simulated:
                return simulated[entity_id]
            found = _find_one(lookup, kind, "id", entity_id)
            return found

        for record in candidates:
            raw = record["data"]
            payload = raw.get("payload") or {}
            ptype = payload.get("type")
            if ptype == "refuge_movement":
                refuge_id = payload.get("refuge_id")
                worker_id = payload.get("worker_id")
                direction = payload.get("direction")
                if refuge_id not in refuge_ids:
                    add_conflict(record, "unknown_refuge", "unknown refuge_id: %s" % refuge_id)
                    continue
                if worker_id not in worker_ids:
                    add_conflict(record, "unknown_worker", "unknown worker_id: %s" % worker_id)
                    continue
                if direction not in ("enter", "exit"):
                    add_conflict(record, "invalid_direction", "direction must be enter or exit")
                    continue
                chamber = inside.setdefault(refuge_id, set())
                current_refuge = location.get(worker_id)
                if direction == "enter":
                    if current_refuge == refuge_id:
                        related = [
                            item["id"]
                            for item in candidates
                            if item is not record
                            and (item["data"].get("payload") or {}).get("worker_id") == worker_id
                            and (item["data"].get("payload") or {}).get("refuge_id") == refuge_id
                        ]
                        add_conflict(
                            record,
                            "movement_contradiction",
                            "worker %s enters refuge %s while already inside" % (worker_id, refuge_id),
                            related,
                        )
                    elif current_refuge is not None:
                        add_conflict(
                            record,
                            "movement_contradiction",
                            "worker %s enters refuge %s without exiting refuge %s"
                            % (worker_id, refuge_id, current_refuge),
                        )
                    else:
                        chamber.add(worker_id)
                        location[worker_id] = refuge_id
                        record_updates.append({"id": record["id"], "status": "applied", "data": dict(raw)})
                        audits.append(
                            {
                                "entity_id": record["id"],
                                "action": "replay_apply",
                                "from_status": "pending",
                                "to_status": "applied",
                                "detail": {"type": "refuge_movement", "recorded_at": raw.get("recorded_at")},
                            }
                        )
                elif worker_id not in chamber:
                    related = [
                        item["id"]
                        for item in candidates
                        if item is not record
                        and (item["data"].get("payload") or {}).get("worker_id") == worker_id
                        and (item["data"].get("payload") or {}).get("refuge_id") == refuge_id
                    ]
                    add_conflict(
                        record,
                        "movement_contradiction",
                        "worker %s exits refuge %s without an entry" % (worker_id, refuge_id),
                        related,
                    )
                else:
                    chamber.discard(worker_id)
                    location.pop(worker_id, None)
                    record_updates.append({"id": record["id"], "status": "applied", "data": dict(raw)})
                    audits.append(
                        {
                            "entity_id": record["id"],
                            "action": "replay_apply",
                            "from_status": "pending",
                            "to_status": "applied",
                            "detail": {"type": "refuge_movement", "recorded_at": raw.get("recorded_at")},
                        }
                    )
            elif ptype == "entity_action":
                target_kind = self.normalize_kind(payload.get("kind") or "")
                entity_id = payload.get("entity_id")
                action_name = payload.get("action")
                action_data = dict(payload.get("data") or {})
                if target_kind not in self.INITIAL_STATUS or target_kind == "offline_record":
                    add_conflict(record, "invalid_target", "unknown target kind: %s" % target_kind)
                    continue
                entity = current_entity(target_kind, entity_id)
                if not entity:
                    add_conflict(record, "unknown_entity", "unknown entity_id: %s" % entity_id)
                    continue
                if not action_name:
                    add_conflict(record, "missing_action", "payload.action is required")
                    continue
                base_version = payload.get("base_version")
                if base_version is None:
                    add_conflict(
                        record,
                        "missing_base_version",
                        "entity_action replay requires base_version",
                    )
                    continue
                try:
                    base_version = int(base_version)
                except (TypeError, ValueError):
                    add_conflict(record, "invalid_base_version", "base_version must be an integer")
                    continue
                stale = entity["version"] != base_version
                # The center version moved on: recompute against the current
                # state instead of blindly overwriting the confirmed status.
                try:
                    next_status, patch = self.validate_transition(
                        actor, entity, action_name, dict(action_data), replay_lookup
                    )
                except DomainError as exc:
                    add_conflict(
                        record,
                        "stale_action" if stale else "invalid_action",
                        "replay action %s rejected on current %s state: %s"
                        % (action_name, target_kind, exc),
                    )
                    continue
                merged = dict(entity["data"])
                merged.update(patch)
                simulated[entity_id] = {
                    "id": entity["id"],
                    "kind": entity["kind"],
                    "status": next_status,
                    "version": entity["version"] + 1,
                    "data": merged,
                }
                actions.append(
                    {
                        "type": "entity_action",
                        "entity_id": entity_id,
                        "expected_version": entity["version"],
                        "status": next_status,
                        "data": merged,
                    }
                )
                audits.append(
                    {
                        "entity_id": entity_id,
                        "action": action_name,
                        "from_status": entity["status"],
                        "to_status": next_status,
                        "detail": {
                            "replayed_from": record["id"],
                            "recorded_at": raw.get("recorded_at"),
                            "base_version": base_version,
                            "recomputed": stale,
                        },
                    }
                )
                record_updates.append({"id": record["id"], "status": "applied", "data": dict(raw)})
                audits.append(
                    {
                        "entity_id": record["id"],
                        "action": "replay_apply",
                        "from_status": "pending",
                        "to_status": "applied",
                        "detail": {"type": "entity_action", "recorded_at": raw.get("recorded_at")},
                    }
                )
            else:
                # Informational record (gas readings, notes, ...): accept it
                # without touching domain state.
                record_updates.append({"id": record["id"], "status": "applied", "data": dict(raw)})
                audits.append(
                    {
                        "entity_id": record["id"],
                        "action": "replay_apply",
                        "from_status": "pending",
                        "to_status": "applied",
                        "detail": {"type": ptype or "info", "recorded_at": raw.get("recorded_at")},
                    }
                )

        if conflicts:
            raise ReplayConflictError(
                "offline replay stopped with %d conflict(s)" % len(conflicts), conflicts
            )

        # Recompute final occupancy from the simulated inside sets and sync
        # refuge state for the chambers this batch touched.
        for refuge in refuges:
            chamber = inside.get(refuge["id"], set())
            capacity = _number(refuge["data"].get("capacity"), "capacity")
            occupancy[refuge["id"]]["worker_ids"] = sorted(chamber)
            occupancy[refuge["id"]]["occupied"] = len(chamber)
            occupancy[refuge["id"]]["over_capacity"] = len(chamber) > capacity
            next_refuge_status = "occupied" if chamber else "available"
            if refuge["status"] not in ("maintenance", next_refuge_status):
                actions.append(
                    {
                        "type": "refuge_status",
                        "entity_id": refuge["id"],
                        "expected_version": refuge["version"],
                        "status": next_refuge_status,
                        "data": dict(refuge["data"]),
                    }
                )
                audits.append(
                    {
                        "entity_id": refuge["id"],
                        "action": "replay_sync",
                        "from_status": refuge["status"],
                        "to_status": next_refuge_status,
                        "detail": {"occupied": len(chamber)},
                    }
                )

        over = [chamber for chamber in occupancy.values() if chamber["over_capacity"]]
        return {
            "actions": actions,
            "records": record_updates,
            "audits": audits,
            "occupancy": occupancy,
            "capacity_exceeded": over,
            "processed_ids": [record["id"] for record in candidates],
        }
