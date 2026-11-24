import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from library_store import LibraryError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "library.sqlite3")
        self.production = self.store.call("create_production", {"name": "示例剧目"}, "DIT")
        self.session = self.new_session("第三场")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def new_session(self, name):
        return self.store.call("save_session", {"production_id": self.production["id"], "name": name,
                                                "config": {"fps": 25, "library_source_id": "persist-me"},
                                                "segments": [{"start": 0, "end": 100, "custom": {"x": 1}}]}, "DIT")

    def add_text(self, session=None, path="/Volumes/NAS/Day03/PGM.mov", text="你为什么来这里？", start=1, end=4):
        return self.store.call("add_transcript", {"session_id": (session or self.session)["id"], "media_path": path,
                                                 "segments": [{"start_seconds": start, "end_seconds": end, "text": text,
                                                               "speaker": "甲", "custom": "kept"}],
                                                 "metadata": {"fps": 25}}, "ASR")

    def candidate(self):
        self.store.call("put_script", {"production_id": self.production["id"], "title": "台本",
                                        "lines": [{"scene": "第一幕", "speaker": "甲", "text": "你为什么来这里？", "order": 2}]}, "场记")
        line = self.store.call("list_script", {"production_id": self.production["id"]})[0]
        transcript = self.add_text()["segments"][0]
        item = {"script_line_id": line["id"], "transcript_id": transcript["id"], "score": .9, "reason": "台词近似"}
        self.store.call("replace_candidates", {"production_id": self.production["id"], "candidates": [item]}, "自动匹配")
        return self.store.call("list_candidates", {"production_id": self.production["id"]})[0], item

    def test_session_unknown_fields_roundtrip_and_no_name_overwrite(self):
        config = {"fps": 25, "library_source_id": "persist-me", "future": {"source": ["one", {"two": 3}]}}
        segments = [{"start": 20, "end": 40, "camera": "B", "future_extension": True}]
        updated = self.store.call("save_session", {"production_id": self.production["id"], "name": "第三场",
                                                   "session_id": self.session["id"], "expected_revision": 1,
                                                   "config": config, "segments": segments}, "剪辑")
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(self.store.get_session(updated["id"])["config"], config)
        self.assertEqual(self.store.get_session(updated["id"])["segments"], segments)
        other = self.new_session("第三场")
        self.assertNotEqual(other["id"], self.session["id"])
        self.assertEqual(len(self.store.list_sessions(self.production["id"])), 2)

    def test_session_update_requires_revision_and_rejects_stale(self):
        params = {"production_id": self.production["id"], "name": "新版", "config": {}, "session_id": self.session["id"]}
        for missing in (None, 0, True):
            with self.assertRaises(LibraryError) as caught:
                self.store.call("save_session", {**params, "expected_revision": missing})
            self.assertEqual(caught.exception.code, "conflict")
        self.store.call("save_session", {**params, "expected_revision": 1})
        with self.assertRaises(LibraryError) as caught:
            self.store.call("save_session", {**params, "expected_revision": 1})
        self.assertEqual(caught.exception.code, "conflict")

    def test_concurrent_candidate_decisions_one_wins(self):
        candidate, _ = self.candidate()
        barrier = threading.Barrier(2)
        def edit(actor):
            barrier.wait()
            try:
                return self.store.call("update_candidate", {"candidate_id": candidate["id"], "status": "confirmed",
                                                            "note": actor, "expected_revision": 1}, actor)["note"]
            except LibraryError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ["导演", "场记"]))
        self.assertEqual(results.count("conflict"), 1)
        winner = self.store.list_candidates(self.production["id"])[0]
        self.assertEqual(winner["revision"], 2)
        self.assertEqual(winner["note"], winner["updated_by"])
        audits = self.store.audit(self.production["id"])
        decisions = [item for item in audits if item["action"] == "update_candidate"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["actor"], winner["note"])

    def test_concurrent_pgm_registration_has_one_owner(self):
        barrier = threading.Barrier(2)
        def register(name):
            barrier.wait()
            try:
                return self.store.save_session(self.production['id'], name, {
                    'library_record_type': 'recording', 'pgm': '/NAS/Day03/PGM.mov'})['id']
            except LibraryError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(register, ['导演', '场记']))
        self.assertEqual(result.count('duplicate_material'), 1)
        owners = [s for s in self.store.list_sessions(self.production['id'])
                  if s['config'].get('pgm') == '/NAS/Day03/PGM.mov']
        self.assertEqual(len(owners), 1)
        owner = owners[0]
        self.store.save_session(self.production['id'], owner['name'], owner['config'],
            session_id=owner['id'], expected_revision=owner['revision'])

    def test_regenerated_matches_preserve_confirmed_and_rejected(self):
        candidate, item = self.candidate()
        confirmed = self.store.update_candidate(candidate["id"], "confirmed", note="选中表演", tags=["主选"], expected_revision=1, actor="导演")
        result = self.store.replace_candidates(self.production["id"], [{**item, "score": .1, "reason": "新模型"}])
        current = self.store.list_candidates(self.production["id"])[0]
        self.assertEqual(result["preserved_decisions"], 1)
        self.assertEqual(current, confirmed)
        rejected = self.store.update_candidate(candidate["id"], "rejected", note="口型不符", expected_revision=2)
        self.store.replace_candidates(self.production["id"], [{**item, "score": 1}])
        self.assertEqual(self.store.list_candidates(self.production["id"])[0], rejected)
        self.store.replace_candidates(self.production["id"], [])
        self.assertEqual(self.store.list_candidates(self.production["id"])[0]["status"], "rejected")

    def test_script_reimport_same_lines_stable_and_new_version_preserves_decision(self):
        candidate, _ = self.candidate()
        self.store.update_candidate(candidate["id"], "confirmed", expected_revision=1)
        doc = self.store.put_script(self.production["id"], "台本", [{"scene": "第一幕", "speaker": "甲", "text": "你为什么来这里？", "order": 2}])
        self.assertTrue(doc["unchanged"])
        self.assertEqual(self.store.list_script(self.production["id"])[0]["id"], candidate["script_line_id"])
        newer = self.store.put_script(self.production["id"], "台本", [{"scene": "第一幕", "speaker": "甲", "text": "你怎么来了？", "order": 2}])
        self.assertEqual(newer["version"], 2)
        self.assertEqual(self.store.list_script(self.production["id"])[0]["text"], "你怎么来了？")
        old = self.store.list_candidates(self.production["id"])[0]
        self.assertEqual(old["status"], "confirmed")
        self.assertEqual(old["script_active"], 0)
        self.assertEqual(old["script_text"], "你为什么来这里？")

    def test_transcripts_idempotent_and_cross_session_identity(self):
        first = self.add_text()
        again = self.add_text()
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["transcript_ids"], first["transcript_ids"])
        self.assertEqual(first["segments"][0]["data"]["custom"], "kept")
        other = self.add_text(session=self.new_session("第一场"))
        self.assertNotEqual(other["media_id"], first["media_id"])
        different_path = self.add_text(path="/Volumes/NAS/Day01/PGM.mov")
        self.assertNotEqual(different_path["media_id"], first["media_id"])
        changed = self.add_text(text="你怎么来这里了？")
        self.assertEqual(changed["added"], 1)
        self.assertEqual(len(self.store.list_transcripts(self.production["id"])), 4)

    def test_fingerprint_upgrade_idempotent_and_source_replacement_detected(self):
        original = self.add_text()
        params = {"session_id": self.session["id"], "media_path": "/Volumes/NAS/Day03/PGM.mov",
                  "segments": [{"start_seconds": 1, "end_seconds": 4, "text": "你为什么来这里？"}],
                  "metadata": {"sha256": "fingerprint-one"}}
        upgraded = self.store.call("add_transcript", params)
        self.assertEqual(upgraded["transcript_ids"], original["transcript_ids"])
        with self.assertRaises(LibraryError) as caught:
            self.store.call("add_transcript", {**params, "metadata": {"sha256": "fingerprint-two"}})
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.list_transcripts(self.production["id"])[0]["metadata"]["sha256"], "fingerprint-one")

    def test_rerun_transcription_keeps_manual_candidate_history(self):
        candidate, _ = self.candidate()
        self.store.update_candidate(candidate["id"], "confirmed", expected_revision=1)
        self.add_text(text="您为什么来这儿？")
        self.assertEqual(len(self.store.list_transcripts(self.production["id"])), 2)
        preserved = self.store.list_candidates(self.production["id"])[0]
        self.assertEqual(preserved["status"], "confirmed")
        self.assertEqual(preserved["transcript_text"], "你为什么来这里？")

    def test_joined_match_retains_session_and_precise_source(self):
        candidate, _ = self.candidate()
        self.assertEqual(candidate["session_name"], "第三场")
        self.assertEqual(candidate["media_path"], "/Volumes/NAS/Day03/PGM.mov")
        self.assertEqual(candidate["start_seconds"], 1)
        self.assertEqual(candidate["end_seconds"], 4)
        self.assertEqual(candidate["script_scene"], "第一幕")
        self.assertEqual(candidate["metadata"], {"fps": 25})
        self.assertEqual(self.store.list_candidates(self.production["id"], status="confirmed"), [])

    def test_cross_production_association_rejected_and_batch_rollback(self):
        candidate, item = self.candidate()
        other = self.store.create_production("其他话剧")
        doc = self.store.put_script(other["id"], "台本", [{"text": "你好"}])
        foreign = self.store.list_script(other["id"])[0]
        with self.assertRaises(LibraryError):
            self.store.replace_candidates(self.production["id"], [{**item, "score": .2}, {**item, "script_line_id": foreign["id"]}])
        self.assertEqual(self.store.list_candidates(self.production["id"])[0]["score"], .9)
        with self.assertRaises(LibraryError):
            self.store.list_transcripts(other["id"], self.session["id"])

    def test_artifact_manifest_is_lossless(self):
        manifest = {"proxy_original_map": {"/NAS/Day1/A.mov": "/NAS/Day1/RAW/A.mxf"}, "revision": 4}
        artifact = self.store.register_artifact(self.session["id"], "multicam_fcpxml", "/NAS/exports/test.fcpxml", manifest)
        self.assertEqual(self.store.list_artifacts(self.session["id"])[0]["manifest"], manifest)
        self.assertEqual(artifact["session_id"], self.session["id"])

    def test_backup_consistent_and_cannot_overwrite(self):
        candidate, _ = self.candidate()
        path = Path(self.directory.name) / "backup.sqlite3"
        backup = self.store.backup(str(path), actor="DIT")
        self.assertEqual(backup["integrity"], "ok")
        restored = Store(path)
        try:
            self.assertEqual(restored.list_candidates(self.production["id"])[0]["id"], candidate["id"])
        finally:
            restored.close()
        for target in (str(path), self.store.path):
            with self.assertRaises(LibraryError):
                self.store.backup(target)
        self.assertGreater(backup["size_bytes"], 0)

    def test_invalid_input_has_no_partial_write(self):
        for start, end in ((-1, 3), (2, 2), (3, 2), (float("nan"), 3), (1, float("inf"))):
            with self.assertRaises(LibraryError):
                self.add_text(start=start, end=end)
        self.assertEqual(self.store.list_transcripts(self.production["id"]), [])
        with self.assertRaises(LibraryError):
            self.store.call("execute", {"sql": "DROP TABLE sessions"})
        with self.assertRaises(LibraryError):
            self.store.call({"malicious": "method"}, {})
        with self.assertRaises(LibraryError):
            self.store.call("list_productions", {}, actor="")
        with self.assertRaises(LibraryError):
            self.store.call("save_session", {"production_id": self.production["id"], "name": "x", "config": {"nan": float("nan")}})
        with self.assertRaises(LibraryError):
            Store("smb://nas/library.db")

    def test_memory_store_and_file_reopen(self):
        memory = Store(":memory:")
        try:
            value = memory.create_production("测试")
            self.assertEqual(memory.list_productions()[0]["id"], value["id"])
        finally:
            memory.close()
        reopened = Store(self.store.path)
        try:
            self.assertEqual(reopened.get_session(self.session["id"])["name"], "第三场")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
