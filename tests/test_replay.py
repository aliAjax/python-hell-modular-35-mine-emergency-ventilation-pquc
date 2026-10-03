import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ReplayConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def movement(record_id, recorded_at, refuge_id, worker_id, direction, source_id="field-a", seq=None):
    return {
        "source_id": source_id,
        "record_id": record_id,
        "recorded_at": recorded_at,
        "seq": seq,
        "payload": {
            "type": "refuge_movement",
            "refuge_id": refuge_id,
            "worker_id": worker_id,
            "direction": direction,
        },
    }


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def setup_chamber(self, capacity=2, workers=("w-1", "w-2")):
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": capacity})
        ids = {}
        for worker_id in workers:
            ids[worker_id] = self.create(
                "worker", {"name": "Miner " + worker_id, "location_code": "M-01", "team": "A"}
            )["id"]
        return refuge, ids

    def test_records_replay_in_field_time_order_as_one_batch(self):
        refuge, ids = self.setup_chamber()
        # Deliberately uploaded out of order: exit has a higher record number
        # but happened later in the field.
        records = [
            movement("103", "2026-10-03T10:30:00Z", refuge["id"], ids["w-1"], "exit"),
            movement("101", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("102", "2026-10-03T10:10:00Z", refuge["id"], ids["w-2"], "enter"),
        ]
        result = self.service.merge_offline(self.actor, records)
        chamber = next(item for item in result["occupancy"] if item["refuge_id"] == refuge["id"])
        self.assertEqual(chamber["occupied"], 1)
        self.assertEqual(chamber["worker_ids"], [ids["w-2"]])
        self.assertEqual(
            {record["status"] for record in self.service.list("offline_record")},
            {"applied"},
        )

    def test_contradictory_movement_stops_replay_and_lists_conflicts(self):
        refuge, ids = self.setup_chamber()
        records = [
            movement("1", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("2", "2026-10-03T10:05:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("3", "2026-10-03T10:10:00Z", refuge["id"], ids["w-2"], "enter"),
        ]
        with self.assertRaises(ReplayConflictError) as caught:
            self.service.merge_offline(self.actor, records)
        codes = {entry["code"] for entry in caught.exception.conflicts}
        self.assertIn("movement_contradiction", codes)
        bad = next(
            entry for entry in caught.exception.conflicts if entry["code"] == "movement_contradiction"
        )
        self.assertTrue(bad["related"])
        # Nothing was applied; the contradicting record is flagged, the rest
        # remain pending so they can be retried after review.
        rows = {record["data"]["record_id"]: record for record in self.service.list("offline_record")}
        self.assertEqual(rows["2"]["status"], "conflict")
        self.assertEqual(rows["1"]["status"], "pending")
        self.assertEqual(rows["3"]["status"], "pending")
        chamber = next(item for item in self.service.refuge_occupancy() if item["refuge_id"] == refuge["id"])
        self.assertEqual(chamber["occupied"], 0)

    def test_conflict_can_be_reviewed_and_batch_retried(self):
        refuge, ids = self.setup_chamber()
        records = [
            movement("1", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("2", "2026-10-03T10:05:00Z", refuge["id"], ids["w-1"], "enter"),
        ]
        with self.assertRaises(ReplayConflictError):
            self.service.merge_offline(self.actor, records)
        conflict_record = next(
            record for record in self.service.list("offline_record") if record["status"] == "conflict"
        )
        self.service.review_conflict(self.actor, conflict_record["id"], "duplicate tap, ignore")
        # Retry after review: the duplicate is skipped, the legitimate entry
        # and the later exit post as a fresh batch.
        result = self.service.merge_offline(
            self.actor,
            [
                records[0],
                movement("3", "2026-10-03T10:30:00Z", refuge["id"], ids["w-1"], "exit"),
            ],
        )
        chamber = next(item for item in result["occupancy"] if item["refuge_id"] == refuge["id"])
        self.assertEqual(chamber["occupied"], 0)
        self.assertEqual(len(result["applied"]), 2)

    def test_capacity_overflow_rejects_and_rolls_back_whole_batch(self):
        refuge, ids = self.setup_chamber(capacity=1, workers=("w-1", "w-2"))
        records = [
            movement("1", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("2", "2026-10-03T10:05:00Z", refuge["id"], ids["w-2"], "enter"),
        ]
        with self.assertRaises(ConflictError):
            self.service.merge_offline(self.actor, records)
        # Full rollback: every record stays pending, chamber still empty.
        self.assertTrue(all(record["status"] == "pending" for record in self.service.list("offline_record")))
        chamber = next(item for item in self.service.refuge_occupancy() if item["refuge_id"] == refuge["id"])
        self.assertEqual(chamber["occupied"], 0)
        self.assertEqual(self.service.get(refuge["id"])["status"], "available")

        # After one person leaves in the field, the same batch minus the
        # overflow can post.
        retry = [
            movement("1", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
            movement("3", "2026-10-03T10:20:00Z", refuge["id"], ids["w-1"], "exit"),
        ]
        result = self.service.merge_offline(self.actor, retry)
        chamber = next(item for item in result["occupancy"] if item["refuge_id"] == refuge["id"])
        self.assertEqual(chamber["occupied"], 0)

    def test_stale_action_is_recomputed_not_overwritten(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        version1 = incident["version"]
        # Center moves the incident forward before the offline batch returns.
        incident = self.service.transition(self.actor, incident["id"], "begin_evacuation")
        self.assertGreater(incident["version"], version1)

        def action_record(record_id, action, base_version):
            return {
                "source_id": "field-a",
                "record_id": record_id,
                "recorded_at": "2026-10-03T11:00:00Z",
                "payload": {
                    "type": "entity_action",
                    "kind": "incident",
                    "entity_id": incident["id"],
                    "action": action,
                    "data": {},
                    "base_version": base_version,
                },
            }

        # begin_evacuation from an old version no longer makes sense on the
        # current state, so it must be listed as a stale conflict instead of
        # rolling the incident back to "evacuating".
        with self.assertRaises(ReplayConflictError) as caught:
            self.service.merge_offline(self.actor, [action_record("1", "begin_evacuation", version1)])
        self.assertEqual(caught.exception.conflicts[0]["code"], "stale_action")
        self.assertEqual(self.service.get(incident["id"])["status"], "evacuating")
        # Review the rejected record so the next batch can go through.
        bad = next(
            record for record in self.service.list("offline_record") if record["status"] == "conflict"
        )
        self.service.review_conflict(self.actor, bad["id"], "superseded by center action")

        # A stale-but-still-valid action is recomputed against the current
        # center state and posts (version mismatch is flagged for the audit).
        result = self.service.merge_offline(self.actor, [action_record("2", "search", version1)])
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(self.service.get(incident["id"])["status"], "searching")
        detail = next(
            entry
            for entry in self.service.audit_log(incident["id"])
            if entry["action"] == "search"
        )["detail"]
        self.assertTrue(detail["recomputed"])

    def test_failed_write_keeps_unprocessed_records_for_retry(self):
        refuge, ids = self.setup_chamber()
        records = [
            movement("1", "2026-10-03T10:00:00Z", refuge["id"], ids["w-1"], "enter"),
        ]
        real_apply = self.repository.apply_replay
        calls = {"count": 0}

        def flaky_apply(plan, actor_id):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("simulated disk failure during commit")
            return real_apply(plan, actor_id)

        self.repository.apply_replay = flaky_apply
        with self.assertRaises(OSError):
            self.service.merge_offline(self.actor, records)
        # Raw record survived the failed write and is still pending.
        staged = self.service.list("offline_record")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["status"], "pending")

        self.repository.apply_replay = real_apply
        result = self.service.merge_offline(self.actor, records)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["applied"][0]["status"], "applied")


class VentilationGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def test_restore_blocked_by_alarm_gas(self):
        self.create("sensor", {"location_code": "M-01", "gas_ppm": 200, "threshold_ppm": 80})
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-01", "capacity": 10})
        vent = self.service.transition(self.actor, vent["id"], "stop")
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, vent["id"], "restore", {"tested_at": "2026-10-03T11:00:00Z"})

    def test_restore_blocked_by_worker_left_in_area(self):
        self.create("worker", {"name": "Still Inside", "location_code": "M-01", "team": "A"})
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-01", "capacity": 10})
        vent = self.service.transition(self.actor, vent["id"], "stop")
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, vent["id"], "restore", {"tested_at": "2026-10-03T11:00:00Z"})

    def test_restore_allowed_after_area_is_clear(self):
        worker = self.create("worker", {"name": "Gone Out", "location_code": "M-01", "team": "A"})
        worker = self.service.transition(self.actor, worker["id"], "deactivate")
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-01", "capacity": 10})
        vent = self.service.transition(self.actor, vent["id"], "stop")
        vent = self.service.transition(self.actor, vent["id"], "restore", {"tested_at": "2026-10-03T11:00:00Z"})
        self.assertEqual(vent["status"], "running")

    def test_offline_restore_replay_respects_same_guard(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 200, "threshold_ppm": 80})
        sensor = self.service.transition(self.actor, sensor["id"], "raise_alarm")
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-01", "capacity": 10})
        vent = self.service.transition(self.actor, vent["id"], "stop")
        version = vent["version"]
        record = {
            "source_id": "field-a",
            "record_id": "9",
            "recorded_at": "2026-10-03T11:05:00Z",
            "payload": {
                "type": "entity_action",
                "kind": "ventilation",
                "entity_id": vent["id"],
                "action": "restore",
                "data": {"tested_at": "2026-10-03T11:05:00Z"},
                "base_version": version,
            },
        }
        with self.assertRaises(ReplayConflictError) as caught:
            self.service.merge_offline(self.actor, [record])
        self.assertEqual(caught.exception.conflicts[0]["code"], "invalid_action")
        self.assertEqual(self.service.get(vent["id"])["status"], "stopped")


class CloseGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def ready_incident(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.service.transition(self.actor, incident["id"], action)
        return incident

    def test_close_blocked_when_recomputed_occupancy_nonzero(self):
        incident = self.ready_incident()
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 5})
        worker = self.create("worker", {"name": "Hiding Miner", "location_code": "M-01", "team": "A"})
        self.service.merge_offline(
            self.actor,
            [movement("1", "2026-10-03T10:00:00Z", refuge["id"], worker["id"], "enter")],
        )
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "close", {"summary": "done"})

        # Once the exit replays and the chamber empties, closing succeeds.
        self.service.merge_offline(
            self.actor,
            [movement("2", "2026-10-03T12:00:00Z", refuge["id"], worker["id"], "exit")],
        )
        incident = self.service.transition(self.actor, incident["id"], "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_close_blocked_by_unreviewed_conflict_then_allowed(self):
        incident = self.ready_incident()
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 5})
        with self.assertRaises(ReplayConflictError):
            self.service.merge_offline(
                self.actor,
                [
                    movement("1", "2026-10-03T10:00:00Z", refuge["id"], "w-9-missing", "enter"),
                ],
            )
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "close", {"summary": "done"})
        conflict_record = next(
            record for record in self.service.list("offline_record") if record["status"] == "conflict"
        )
        self.service.review_conflict(self.actor, conflict_record["id"], "bad worker id, discard")
        incident = self.service.transition(self.actor, incident["id"], "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
