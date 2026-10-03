import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, BatchConflictError, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailingRepository:
    """Wrapper that simulates a write failure on one offline-record status update."""

    def __init__(self, real, fail_on_record_id):
        self._real = real
        self._fail_on = fail_on_record_id
        self.failed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def update_entity(self, entity_id, expected_version, status, data):
        if not self.failed and entity_id == self._fail_on and status == "applied":
            self.failed = True
            raise RuntimeError("simulated write failure")
        return self._real.update_entity(entity_id, expected_version, status, data)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def offline(self, source_id, record_id, recorded_at, payload):
        return {
            "source_id": source_id,
            "record_id": record_id,
            "recorded_at": recorded_at,
            "payload": payload,
        }

    def test_replay_orders_by_on_site_time_not_record_number(self):
        # Records arrive out of order (record numbers), but replay must sort by recorded_at.
        records = [
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        refuge = self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        result = self.service.replay_offline(self.actor)
        self.assertEqual(result["replayed"], 2)
        updated = self.service.get(refuge["id"])
        self.assertEqual(updated["data"]["occupants"], [])
        self.assertEqual(updated["data"]["occupancy"], 0)

    def test_contradictory_order_stops_and_lists_conflicts(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "3", "2026-09-27T10:03:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        with self.assertRaises(BatchConflictError) as ctx:
            self.service.replay_offline(self.actor)
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertEqual(ctx.exception.conflicts[0]["record_id"], "3")
        self.assertIn("not inside any refuge", ctx.exception.conflicts[0]["reason"])
        # The offending record is marked conflict (未核对).
        all_records = self.service.list("offline_record")
        conflict = [r for r in all_records if r["data"]["record_id"] == "3"][0]
        self.assertEqual(conflict["status"], "conflict")
        # The whole batch is rejected: nothing applied.
        self.assertTrue(all(r["status"] == "pending" for r in all_records if r["data"]["record_id"] != "3"))

    def test_double_entry_is_a_conflict(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        with self.assertRaises(BatchConflictError) as ctx:
            self.service.replay_offline(self.actor)
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertEqual(ctx.exception.conflicts[0]["record_id"], "2")

    def test_over_capacity_rejects_whole_batch_and_rolls_back(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "entry", "worker_id": "w2", "location_code": "REFUGE-1"}),
            self.offline("field-a", "3", "2026-09-27T10:03:00Z",
                         {"event": "entry", "worker_id": "w3", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        refuge = self.create("refuge", {"location_code": "REFUGE-1", "capacity": 2})
        with self.assertRaises(BatchConflictError) as ctx:
            self.service.replay_offline(self.actor)
        self.assertEqual(len(ctx.exception.over_capacity), 1)
        self.assertEqual(ctx.exception.over_capacity[0]["occupancy"], 3)
        self.assertEqual(ctx.exception.over_capacity[0]["capacity"], 2)
        # Nothing written: refuge has no occupants, records stay pending.
        updated = self.service.get(refuge["id"])
        self.assertNotIn("occupants", updated["data"])
        self.assertTrue(all(r["status"] == "pending" for r in self.service.list("offline_record")))

    def test_recompute_overwrites_stale_center_state(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "entry", "worker_id": "w2", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        refuge = self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        self.service.replay_offline(self.actor)
        # Simulate the center holding stale/incorrect occupancy (version moved).
        current = self.service.get(refuge["id"])
        stale = dict(current["data"])
        stale["occupants"] = ["w3"]
        stale["occupancy"] = 1
        self.repo.update_entity(current["id"], current["version"], current["status"], stale)
        # Replay recomputes from the event log rather than copying the stale state.
        self.service.replay_offline(self.actor)
        updated = self.service.get(refuge["id"])
        self.assertEqual(updated["data"]["occupants"], ["w1", "w2"])
        self.assertEqual(updated["data"]["occupancy"], 2)

    def test_replay_is_idempotent(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        refuge = self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        first = self.service.replay_offline(self.actor)
        second = self.service.replay_offline(self.actor)
        self.assertEqual(first["replayed"], 1)
        self.assertEqual(second["replayed"], 0)
        self.assertEqual(second["pending"], 0)
        updated = self.service.get(refuge["id"])
        self.assertEqual(updated["data"]["occupants"], ["w1"])

    def test_write_failure_keeps_unprocessed_records_pending(self):
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "entry", "worker_id": "w2", "location_code": "REFUGE-1"}),
            self.offline("field-a", "3", "2026-09-27T10:03:00Z",
                         {"event": "entry", "worker_id": "w3", "location_code": "REFUGE-1"}),
        ]
        created = self.service.merge_offline(self.actor, records)
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        # Fail on the second record's status update.
        failing = FailingRepository(self.repo, created[1]["id"])
        service = DomainService(failing, RuleEngine())
        result = service.replay_offline(self.actor)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["pending"], 2)
        # Retry with a healthy repository: the unprocessed records are applied.
        result = self.service.replay_offline(self.actor)
        self.assertEqual(result["replayed"], 2)
        self.assertEqual(result["pending"], 0)

    def test_restore_blocked_by_alarm_gas(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        sensor = self.service.list("sensor")[0]
        self.act(sensor, "raise_alarm")
        self.act(vent, "stop")
        with self.assertRaises(ConflictError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z"})
        # Once the gas clears, restore is allowed.
        self.act(sensor, "clear", {})
        restored = self.act(vent, "restore", {"tested_at": "2026-09-27T11:05:00Z"})
        self.assertEqual(restored["status"], "running")

    def test_restore_blocked_by_unevacuated_worker(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "M-01", "team": "A"})
        self.act(vent, "stop")
        with self.assertRaises(ConflictError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z"})
        # Once the worker evacuates, restore is allowed.
        self.act(worker, "mark_missing")
        self.act(worker, "evacuate")
        restored = self.act(vent, "restore", {"tested_at": "2026-09-27T11:05:00Z"})
        self.assertEqual(restored["status"], "running")

    def test_close_blocked_by_over_capacity(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 1})
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "entry", "worker_id": "w2", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        with self.assertRaises(BatchConflictError):
            self.service.replay_offline(self.actor)
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})

    def test_close_blocked_by_unresolved_conflict(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "3", "2026-09-27T10:03:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        with self.assertRaises(BatchConflictError):
            self.service.replay_offline(self.actor)
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})

    def test_resolve_conflict_allows_close(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        records = [
            self.offline("field-a", "1", "2026-09-27T10:01:00Z",
                         {"event": "entry", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "2", "2026-09-27T10:02:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
            self.offline("field-a", "3", "2026-09-27T10:03:00Z",
                         {"event": "exit", "worker_id": "w1", "location_code": "REFUGE-1"}),
        ]
        self.service.merge_offline(self.actor, records)
        self.create("refuge", {"location_code": "REFUGE-1", "capacity": 5})
        with self.assertRaises(BatchConflictError):
            self.service.replay_offline(self.actor)
        conflict_record = [r for r in self.service.list("offline_record")
                           if r["data"]["record_id"] == "3"][0]
        # A manager manually verifies the conflicting record, clearing the block.
        resolved = self.act(conflict_record, "resolve", {})
        self.assertEqual(resolved["status"], "applied")
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
