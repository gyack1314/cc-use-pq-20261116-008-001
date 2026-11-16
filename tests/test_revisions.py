from __future__ import annotations

import json
import unittest

from talent_harvest.audit import Auditor
from talent_harvest.cli import main
from talent_harvest.contracts import HarvestError
from talent_harvest.export import Delivery
from talent_harvest.inventory import Inventory
from talent_harvest.ops import Operations
from tests.support import Workspace, manifest, record, source


def titled(column, **overrides):
    """A record whose job-title column uses the partner's current header name."""
    row = record(**overrides)
    row[column] = row.pop("title")
    return row


def register_university(work, title_column):
    return work.harvester.register_source(
        source(key="university", fmt="csv", field_map={"title": title_column}))


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()

    def tearDown(self):
        self.work.close()

    def test_header_switch_binds_each_page_to_its_declaration(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")],
                               [titled("岗位名称", record_id="role-2", title="辅导员")]])
        page_2 = path.parent / "page-2.csv"
        original = page_2.read_bytes()
        page_2.unlink()
        first = self.work.harvester.ingest(path)
        self.assertEqual([page["state"] for page in first["pages"]], ["complete", "failed"])
        self.assertEqual(first["pages"][0]["source_revision"], 1)

        register_university(self.work, "job_title")
        self.assertEqual(self.work.ledger.current_revision("university"), 2)
        new_path = manifest(self.work.path, key="university", batch="batch-2", fmt="csv",
                            pages=[[titled("job_title", record_id="role-3", title="实验员")]])
        second = self.work.harvester.ingest(new_path)
        self.assertEqual(second["state"], "complete")
        self.assertEqual(second["pages"][0]["source_revision"], 2)

        page_2.write_bytes(original)
        recovered = self.work.harvester.resume(path)
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(recovered["pages"][1]["source_revision"], 1)

        observations = {row["record_id"]: row
                        for row in self.work.ledger.observations(source="university")}
        self.assertEqual(observations["role-1"]["source_revision"], 1)
        self.assertEqual(observations["role-2"]["source_revision"], 1)
        self.assertEqual(observations["role-3"]["source_revision"], 2)
        self.assertEqual(observations["role-2"]["record"]["title"], "辅导员")

        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 3)
        self.assertEqual(sum(entity["record"]["quantity"] for entity in current["accepted"]), 12)
        self.assertEqual(Inventory(self.work.ledger).recovery_plan()["retryable"], 0)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_resubmitted_manifest_keeps_original_bindings_and_evidence(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")]])
        self.work.harvester.ingest(path)
        register_university(self.work, "job_title")
        events = len(self.work.ledger.events())

        again = self.work.harvester.ingest(path)
        self.assertEqual(again["state"], "complete")
        self.assertTrue(again["pages"][0]["unchanged"])
        self.assertEqual(again["pages"][0]["source_revision"], 1)

        observations = self.work.ledger.observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["version"], 1)
        self.assertEqual(observations[0]["source_revision"], 1)
        self.assertEqual(observations[0]["record"]["title"], "AI Engineer")
        self.assertEqual(len(self.work.ledger.events()), events)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_unrecognized_layout_fails_retryably_and_enters_nothing(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("job_title")]])
        result = self.work.harvester.ingest(path)
        self.assertEqual(result["state"], "failed")
        page = self.work.ledger.page("university", "batch-1", 1)
        self.assertEqual(page["state"], "failed")
        self.assertEqual(page["error_code"], "layout_mismatch")
        self.assertIn("岗位名称", page["error_detail"])
        self.assertEqual(self.work.ledger.observations(), [])
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 0)

        plan = Inventory(self.work.ledger).recovery_plan()
        self.assertEqual(plan["retryable"], 1)
        self.assertEqual(plan["tasks"][0]["source_revision"], 1)

        register_university(self.work, "job_title")
        corrected = manifest(self.work.path, key="university", batch="batch-2", fmt="csv",
                             pages=[[titled("job_title")]])
        recovered = self.work.harvester.ingest(corrected)
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(recovered["pages"][0]["source_revision"], 2)
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 1)
        self.assertEqual(current["accepted"][0]["record"]["quantity"], 4)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_manifest_can_pin_pages_to_specific_revisions(self):
        register_university(self.work, "岗位名称")
        register_university(self.work, "job_title")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")],
                               [titled("job_title", record_id="role-2", title="实验员")]],
                        page_revisions=[1, None])
        result = self.work.harvester.ingest(path)
        self.assertEqual(result["state"], "complete")
        self.assertEqual([page["source_revision"] for page in result["pages"]], [1, 2])
        observations = {row["record_id"]: row
                        for row in self.work.ledger.observations(source="university")}
        self.assertEqual(observations["role-1"]["source_revision"], 1)
        self.assertEqual(observations["role-2"]["source_revision"], 2)
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 2)

    def test_unknown_declared_revision_is_rejected_before_intake(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")]], page_revisions=[7])
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.ingest(path)
        self.assertEqual(caught.exception.code, "source_revision_unknown")
        self.assertEqual(self.work.ledger.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        self.assertEqual(self.work.ledger.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0)

    def test_json_page_missing_business_key_is_layout_mismatch(self):
        self.work.register()
        broken = record()
        del broken["quantity"]
        path = manifest(self.work.path, pages=[[broken]])
        result = self.work.harvester.ingest(path)
        self.assertEqual(result["pages"][0]["state"], "failed")
        self.assertEqual(self.work.ledger.page("company", "batch-1", 1)["error_code"],
                         "layout_mismatch")
        self.assertEqual(self.work.ledger.observations(), [])

    def test_replay_check_uses_bound_revision_after_header_switch(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")]])
        self.work.harvester.ingest(path)
        register_university(self.work, "job_title")
        report = Operations(self.work.ledger).replay_check(path)
        self.assertTrue(report["consistent"])
        self.assertEqual(report["differences"], [])

    def test_withdrawal_keeps_revision_evidence_consistent(self):
        register_university(self.work, "岗位名称")
        old_path = manifest(self.work.path, key="university", fmt="csv",
                            pages=[[titled("岗位名称")]])
        self.work.harvester.ingest(old_path)
        register_university(self.work, "job_title")
        new_path = manifest(self.work.path, key="university", batch="batch-2", fmt="csv",
                            pages=[[titled("job_title", record_id="role-2", title="实验员")]])
        self.work.harvester.ingest(new_path)

        self.work.harvester.withdraw("university", "合作终止")
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 0)
        history = Delivery(self.work.ledger).history(source="university")["observations"]
        self.assertEqual(len(history), 2)
        self.assertEqual({row["source_revision"] for row in history}, {1, 2})
        listing = self.work.ledger.revisions("university")
        self.assertEqual([entry["revision"] for entry in listing["revisions"]], [1, 2])
        self.assertTrue(listing["withdrawn_at"])
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.ingest(manifest(self.work.path, key="university",
                                                batch="batch-3", fmt="csv",
                                                pages=[[titled("job_title")]]))
        self.assertEqual(caught.exception.code, "source_withdrawn")

    def test_consistency_after_supplement_resubmit_and_withdraw(self):
        register_university(self.work, "岗位名称")
        path = manifest(self.work.path, key="university", fmt="csv",
                        pages=[[titled("岗位名称")],
                               [titled("岗位名称", record_id="role-2", title="辅导员")]])
        page_2 = path.parent / "page-2.csv"
        original = page_2.read_bytes()
        page_2.unlink()
        self.work.harvester.ingest(path)
        register_university(self.work, "job_title")

        resubmitted = self.work.harvester.ingest(path)
        self.assertEqual([page["state"] for page in resubmitted["pages"]],
                         ["complete", "failed"])
        self.assertTrue(resubmitted["pages"][0]["unchanged"])
        self.assertEqual(len(self.work.ledger.observations()), 1)

        page_2.write_bytes(original)
        completed = self.work.harvester.resume(path)
        self.assertEqual(completed["state"], "complete")
        batch = self.work.ledger.batch("university", "batch-1")
        self.assertEqual([page["state"] for page in batch["pages"]], ["complete", "complete"])
        self.assertEqual([page["source_revision"] for page in batch["pages"]], [1, 1])

        history = Delivery(self.work.ledger).history(source="university")["observations"]
        self.assertEqual([row["version"] for row in history], [1, 1])
        summary = Delivery(self.work.ledger).summary()
        self.assertEqual(summary["by_kind"]["position"]["quantity"], 8)

        self.work.harvester.withdraw("university", "批次作废")
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 0)
        self.assertEqual(len(Delivery(self.work.ledger).history(source="university")["observations"]), 2)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_revision_listing_tracks_declaration_history(self):
        self.work.register()
        declaration = source()
        declaration["trust"] = 40
        self.work.harvester.register_source(declaration)
        listing = self.work.ledger.revisions("company")
        self.assertEqual(listing["current_revision"], 2)
        self.assertEqual([entry["revision"] for entry in listing["revisions"]], [1, 2])
        self.assertEqual(listing["revisions"][0]["trust"], 70)
        self.assertEqual(listing["revisions"][1]["trust"], 40)
        self.assertNotEqual(listing["revisions"][0]["declaration_hash"],
                            listing["revisions"][1]["declaration_hash"])
        self.assertFalse(listing["revisions"][0]["current"])
        self.assertTrue(listing["revisions"][1]["current"])

    def test_audit_detects_page_binding_tampering(self):
        self.work.register()
        self.work.ingest()
        self.work.ledger.db.execute("UPDATE pages SET source_revision=9")
        self.work.ledger.db.commit()
        report = Auditor(self.work.ledger).verify_state()
        self.assertFalse(report["ok"])
        self.assertIn("page_revision", [item["code"] for item in report["findings"]])

    def test_audit_detects_observation_revision_tampering(self):
        self.work.register()
        self.work.ingest()
        declaration = source()
        declaration["trust"] = 40
        self.work.harvester.register_source(declaration)
        self.work.ledger.db.execute("UPDATE observations SET source_revision=2")
        self.work.ledger.db.commit()
        report = Auditor(self.work.ledger).verify_state()
        self.assertFalse(report["ok"])
        self.assertIn("observation_revision", [item["code"] for item in report["findings"]])

    def test_cli_revisions_command(self):
        declaration = self.work.path / "source.json"
        declaration.write_text(json.dumps(source(key="campus")), encoding="utf-8")
        database = self.work.path / "cli.sqlite3"
        self.assertEqual(main(["--db", str(database), "register", str(declaration)]), 0)
        self.assertEqual(main(["--db", str(database), "revisions", "campus"]), 0)
