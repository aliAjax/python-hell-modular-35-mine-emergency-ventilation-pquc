import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ReplayConflictError,
    ValidationError,
)
from .rules import RuleEngine, compute_refuge_occupancy


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    @staticmethod
    def _offline_id(raw):
        source_id = str(raw.get("source_id", "")).strip()
        record_id = str(raw.get("record_id", "")).strip()
        digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        return "offline-" + digest

    def merge_offline(self, actor, records):
        """Stage field records, then replay them as one batch in field-time order.

        Already-posted records are skipped idempotently. Records that are still
        unprocessed remain staged as ``pending`` so a write failure simply means
        the same batch is retried. Self-contradictory movements stop the replay
        and stay in ``conflict`` until reviewed; exceeding refuge capacity
        rejects and rolls back the whole batch.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        items = []
        seen = set()
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            payload = dict(raw)
            # Structural validation happens before staging so malformed records
            # never poison later replays.
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity_id = self._offline_id(raw)
            if entity_id in seen:
                raise ValidationError("duplicate record in batch: %s/%s" % (source_id, record_id))
            seen.add(entity_id)
            items.append(
                {
                    "id": entity_id,
                    "kind": "offline_record",
                    "status": "pending",
                    "data": payload,
                    "actor_id": actor.user_id,
                }
            )

        staged = self.repository.stage_entities(items)

        # Unreviewed conflicts already known to the center stop new batches too.
        unresolved = [
            record
            for record in self.repository.list_entities(kind="offline_record")
            if record["status"] == "conflict" and not record["data"].get("conflict_reviewed")
        ]
        if unresolved:
            raise ReplayConflictError(
                "unreviewed offline conflict(s) block replay: %d" % len(unresolved),
                [
                    {
                        "code": "unreviewed_conflict",
                        "record_id": record["id"],
                        "source_id": record["data"].get("source_id"),
                        "recorded_at": record["data"].get("recorded_at"),
                        "message": "conflict must be reviewed before more records replay",
                        "related": [
                            entry.get("record_id")
                            for entry in record["data"].get("conflicts", [])
                        ],
                    }
                    for record in unresolved
                ],
            )

        pending = [record for record in staged.values() if record["status"] == "pending"]
        if not pending:
            return {
                "applied": [],
                "skipped": [record["id"] for record in staged.values()],
                "occupancy": self.refuge_occupancy(),
            }

        try:
            plan = self.rules.plan_replay(actor, pending, self._lookup)
        except ReplayConflictError as exc:
            # Stop the batch: every contradicting record is kept and listed,
            # the remaining ones stay pending and can be retried after review.
            self.mark_replay_conflicts(actor, exc.conflicts)
            raise
        for entry in plan["audits"]:
            entry["actor_role"] = actor.role

        if plan["capacity_exceeded"]:
            over = plan["capacity_exceeded"]
            raise ConflictError(
                "replay rejected: refuge capacity exceeded for %s"
                % ", ".join(
                    "%s(%d/%s)" % (c["refuge_id"], c["occupied"], c["capacity"]) for c in over
                )
            )

        # apply_replay commits the batch atomically; on failure the whole
        # transaction rolls back while staged records stay pending for retry.
        self.repository.apply_replay(plan, actor.user_id)

        return {
            "applied": [self.repository.get_entity(item["id"]) for item in plan["records"]],
            "skipped": [
                record["id"]
                for record in staged.values()
                if record["status"] != "pending"
            ],
            "occupancy": self.refuge_occupancy(),
        }

    def mark_replay_conflicts(self, actor, conflicts):
        """Persist contradictory records as ``conflict`` without touching the rest."""
        ids = [entry["record_id"] for entry in conflicts]
        records = {
            record["id"]: record
            for record in self.repository.list_entities(kind="offline_record")
            if record["id"] in ids
        }
        by_id = {}
        for entry in conflicts:
            by_id.setdefault(entry["record_id"], []).append(entry)
        items = []
        for record_id, entries in by_id.items():
            record = records.get(record_id)
            if not record:
                continue
            data = dict(record["data"])
            data["conflicts"] = entries
            items.append({"id": record_id, "conflicts": entries, "data": data})
        self.repository.mark_replay_conflicts(items, actor.user_id)

    def review_conflict(self, actor, entity_id, note=""):
        """Mark a contradictory record reviewed after a human decision."""
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        next_status, patch = self.rules.validate_transition(
            actor, entity, "review_conflict", {"note": note}, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        merged["conflict_reviewed"] = True
        updated = self.repository.update_entity(entity_id, entity["version"], next_status, merged)
        self.audit.record(entity_id, actor, "review_conflict", entity["status"], next_status, {"note": note})
        return updated

    def refuge_occupancy(self):
        return list(
            compute_refuge_occupancy(
                self.repository.list_entities(kind="refuge"),
                self.repository.list_entities(kind="offline_record"),
            ).values()
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
