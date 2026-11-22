"""Editor saves must survive retries, competing edits and source deletion."""
import concurrent.futures
import os
import json
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_data = tempfile.TemporaryDirectory(prefix="mclipper-editor-test-")
os.environ.update(MCLIPPER_DATA=_data.name, DATABASE_URL="", MCLIPPER_ACCOUNTS="0",
                  MCLIPPER_FETCH_MODEL="0", MCLIPPER_UPDATE_CHECK="0",
                  MCLIPPER_PUBLIC_URL="http://127.0.0.1:8765")

from fastapi.testclient import TestClient
from studio import auth, config, ctx, db, server
from studio.editor import compile, exports, firstcut, model, recommendations, restoration, store


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        cls.client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        cls.owner = auth.local_user().id

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        db.engine.dispose()
        _data.cleanup()

    def setUp(self):
        with db.engine.begin() as connection:
            connection.execute(firstcut.passes.delete())
            connection.execute(exports.outputs.delete())
            connection.execute(recommendations.suggestions.delete())
            connection.execute(store.history.delete())
            connection.execute(store.projects.delete())
        self.document = model.import_clip(
            {"start": 606, "end": 622, "hook": "Wait", "segments": [
                {"start": 606, "end": 610}, {"start": 618, "end": 622}]},
            [{"s": 619, "e": 620, "w": "landing"}], 45, 600)
        with ctx.as_user(self.owner):
            self.project = store.create(model.identifier(), "Draft", self.document, {})
        self.path = "/api/editor/projects/" + self.project["id"]

    def edit(self, body):
        return self.client.post(self.path + "/commands", json=body,
                                headers={"Origin": "http://127.0.0.1:8765"})

    def batch(self, **extra):
        return {"base_revision": 0, "op_id": model.identifier(), **extra}

    def test_live_offset_and_caption_mapping(self):
        pictures = [i for i in self.document["items"] if i["kind"] == "video"]
        self.assertEqual([(i["source_in"], i["at_frame"], i["frames"]) for i in pictures],
                         [(180, 0, 120), (540, 120, 120)])
        self.assertEqual(next(i for i in self.document["items"] if i["kind"] == "caption")["at_frame"], 150)

    def test_retries_conflicts_and_restore(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        body = self.batch(ops=[{"type": "item.text", "item_id": title["id"], "text": "New hook"}])
        response = self.edit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.edit(body).json(), response.json())
        conflict = self.edit({**body, "op_id": model.identifier()})
        self.assertEqual(conflict.status_code, 409)
        restored = self.edit({"base_revision": 1, "op_id": model.identifier(), "restore_revision": 0})
        self.assertEqual(restored.json()["document"], self.document)
        self.assertEqual(restored.json()["revision"], 2)
        self.assertEqual(self.edit(body).json(), response.json())
        self.assertEqual(self.client.get(self.path).json()["revision"], 2)
        self.assertEqual(len(self.client.get(self.path + "/history").json()), 3)

    def test_invalid_batch_rolls_back(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        body = self.batch(ops=[{"type": "item.remove", "item_id": title["id"]},
                               {"type": "item.remove", "item_id": "missing"}])
        self.assertEqual(self.edit(body).status_code, 422)
        self.assertEqual(self.client.get(self.path).json()["document"], self.document)

    def test_track_locks_and_linked_cues(self):
        document = model.apply(self.document, [{"type": "track.lock", "track_id": "captions", "locked": True}])
        second = [i for i in document["items"] if i["kind"] == "video"][1]
        with self.assertRaises(ValueError):
            model.apply(document, [{"type": "item.move", "item_id": second["id"], "at_frame": 180}])
        moved = model.apply(self.document, [{"type": "item.move", "item_id": second["id"], "at_frame": 180}])
        self.assertEqual(next(i for i in moved["items"] if i["kind"] == "caption")["at_frame"], 210)

    def test_overlap_and_bad_frame_rejected(self):
        item = self.document["items"][0]
        for frame in (True, -1, 1.5, 90):
            with self.assertRaises(ValueError):
                model.apply(self.document, [{"type": "item.move", "item_id": item["id"], "at_frame": frame}])

    def test_other_owner_cannot_access_or_edit(self):
        with ctx.as_user("other_owner"):
            with db.engine.connect() as connection:
                with self.assertRaises(Exception) as caught:
                    store.get(connection, self.project["id"])
                self.assertEqual(caught.exception.status_code, 404)
            with self.assertRaises(Exception) as caught:
                store.change(self.project["id"], self.batch(restore_revision=0))
            self.assertEqual(caught.exception.status_code, 404)

    def test_competing_saves_have_one_winner(self):
        def save(_):
            with ctx.as_user(self.owner):
                try:
                    return store.change(self.project["id"], self.batch(restore_revision=0))["revision"]
                except Exception as error:
                    return error.status_code
        with concurrent.futures.ThreadPoolExecutor(2) as workers:
            results = list(workers.map(save, range(2)))
        self.assertEqual(sorted(results), [1, 409])

    def test_database_reopen_keeps_document(self):
        db.engine.dispose()
        self.assertEqual(self.client.get(self.path).json()["document"], self.document)

    def test_split_keeps_cues_and_protection(self):
        first = self.document["items"][0]
        edited = model.apply(self.document, [
            {"type": "item.protect", "item_id": first["id"], "protected": True},
            {"type": "item.split", "item_id": first["id"], "offset_frames": 60, "new_id": "split-id-123"}])
        pictures = sorted((i for i in edited["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"])
        self.assertEqual([(i["at_frame"], i["source_in"], i["frames"]) for i in pictures],
                         [(0, 180, 60), (60, 240, 60), (120, 540, 120)])
        self.assertIn("split-id-123", edited["protected"])
        second = pictures[-1]
        with self.assertRaises(ValueError):
            model.apply(edited, [{"type": "item.split", "item_id": second["id"],
                                 "offset_frames": 40, "new_id": "bad-split-id"}])

    def test_recommendations_respect_protection_and_revision(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        cue = next(i for i in self.document["items"] if i["kind"] == "caption")
        with ctx.as_user(self.owner):
            store.change(self.project["id"], self.batch(ops=[{"type": "item.protect", "item_id": title["id"], "protected": True}]))
            answer = [{"item_id": title["id"], "text": "Unwanted", "reason": "Ignored"},
                      {"item_id": cue["id"], "text": "Landing", "reason": "Capitalization"},
                      {"item_id": "hallucinated", "text": "Ignored", "reason": "Unknown item"}]
            with patch("studio.editor.recommendations.brain.editor_text_suggestions", return_value=answer):
                proposals = recommendations.generate(self.project["id"], {"revision": 1})
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["item_id"], cue["id"])
            result = recommendations.decide(self.project["id"], proposals[0]["id"], "apply")
            self.assertEqual(result["status"], "applied")
            self.assertEqual(recommendations.decide(self.project["id"], proposals[0]["id"], "dismiss")["status"], "applied")
            with db.engine.connect() as connection:
                saved = store.public(store.get(connection, self.project["id"]))
            self.assertEqual(saved["revision"], 2)
            self.assertIn(cue["id"], saved["document"]["protected"])

    def test_stale_recommendation_cannot_change_new_draft(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        with ctx.as_user(self.owner), patch("studio.editor.recommendations.brain.editor_text_suggestions",
                return_value=[{"item_id": title["id"], "text": "Suggested", "reason": "Clearer"}]):
            proposal = recommendations.generate(self.project["id"], {"revision": 0})[0]
            store.change(self.project["id"], self.batch(ops=[{"type": "item.text", "item_id": title["id"], "text": "My correction"}]))
            with self.assertRaises(Exception) as caught:
                recommendations.decide(self.project["id"], proposal["id"], "apply")
            self.assertEqual(caught.exception.status_code, 409)
            forged = {"base_revision": 1, "op_id": model.identifier(), "suggestion_id": proposal["id"],
                      "ops": [{"type": "item.text", "item_id": title["id"], "text": "Suggested"}]}
            with self.assertRaises(Exception) as caught:
                store.change(self.project["id"], forged)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(recommendations.decide(self.project["id"], proposal["id"], "dismiss")["status"], "dismissed")

    def test_dismissed_recommendation_stays_dismissed(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        with ctx.as_user(self.owner), patch("studio.editor.recommendations.brain.editor_text_suggestions",
                return_value=[{"item_id": title["id"], "text": "Suggested", "reason": "Clearer"}]):
            proposal = recommendations.generate(self.project["id"], {"revision": 0})[0]
            recommendations.decide(self.project["id"], proposal["id"], "dismiss")
            self.assertEqual(recommendations.generate(self.project["id"], {"revision": 0}), [])

    def test_failed_render_recovers_without_claiming_success(self):
        with ctx.as_user(self.owner):
            directory = exports.folder(self.project["id"])
            directory.mkdir(parents=True)
            (directory / "source.media").write_bytes(b"test source")
            with patch("studio.editor.exports.jobs.submit_heavy"):
                output = exports.submit(self.project["id"], 0)
            self.assertEqual(self.client.delete(self.path).status_code, 409)
            with patch("studio.editor.exports.compile.render_sequence", side_effect=RuntimeError("Encoder failed")):
                exports.run(output["id"])
            with db.engine.connect() as connection:
                row = exports.get(connection, self.project["id"], output["id"])
            self.assertEqual(row["status"], "failed")
            with patch("studio.editor.exports.jobs.submit_heavy") as queue:
                retry = exports.submit(self.project["id"], 0)
                self.assertNotEqual(retry["id"], output["id"])
                exports.recover()
                self.assertEqual(queue.call_count, 2)
            with db.engine.begin() as connection:
                connection.execute(exports.outputs.update().where(exports.outputs.c.id == retry["id"]).values(status="running"))
            exports.recover()
            with db.engine.connect() as connection:
                self.assertEqual(exports.get(connection, self.project["id"], retry["id"])["status"], "failed")

    def test_add_text_and_restore_removed_title(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        changed = model.apply(self.document, [{"type": "item.remove", "item_id": title["id"]},
            {"type": "text.add", "new_id": "manual-title-1", "track_id": "titles", "at_frame": 40, "frames": 60, "text": "Manual title"}])
        text = compile.captions(changed)
        self.assertIn("Manual title", text)
        self.assertNotIn("Wait", text)

    def test_selective_restore_preserves_other_corrections(self):
        title = next(i for i in self.document["items"] if i["kind"] == "title")
        cue = next(i for i in self.document["items"] if i["kind"] == "caption")
        changed = model.apply(self.document, [{"type": "item.remove", "item_id": title["id"]},
                                             {"type": "item.text", "item_id": cue["id"], "text": "My correction"}])
        restored = model.restore_item(changed, self.document, title["id"])
        self.assertEqual(next(i for i in restored["items"] if i["id"] == cue["id"])["text"], "My correction")
        self.assertEqual(next(i for i in restored["items"] if i["id"] == title["id"])["text"], "Wait")
        self.assertIn(title["id"], restored["protected"])
        with ctx.as_user(self.owner):
            store.change(self.project["id"], self.batch(ops=[{"type": "item.remove", "item_id": title["id"]}]))
            saved = store.change(self.project["id"], {"base_revision": 1, "op_id": model.identifier(),
                                 "restore_revision": 0, "restore_item_id": title["id"]})
        self.assertEqual(saved["revision"], 2)
        self.assertIn(title["id"], saved["document"]["protected"])

    def test_trim_retimes_captions_and_checks_source(self):
        second = [i for i in self.document["items"] if i["kind"] == "video"][1]
        command = {"type": "item.trim", "item_id": second["id"], "source_in": 570, "frames": 90}
        edited = model.apply(self.document, [command])
        self.assertEqual(next(i for i in edited["items"] if i["kind"] == "caption")["at_frame"], 120)
        for changes in ({"source_in": 585}, {"source_in": 1340, "frames": 60}):
            with self.assertRaises(ValueError):
                model.apply(self.document, [{**command, **changes}])

    def pacing_document(self, seconds=30, words=None):
        document = model.import_clip({"start": 0, "end": seconds}, words or [
            {"s": 1, "e": 2, "w": "Setup."}, {"s": 3, "e": 4, "w": "Again."},
            {"s": 7, "e": 8, "w": "Payoff."}], seconds)
        with ctx.as_user(self.owner):
            project = store.create(model.identifier(), "Pacing", document, {})
        return project

    def queue_pacing(self, project, **extra):
        with patch("studio.editor.firstcut.jobs.submit_heavy"):
            return firstcut.submit(project["id"], {"revision": project["revision"], "op_id": model.identifier(), **extra})

    @staticmethod
    def pacing_answer(phrases, context, instruction):
        phrase = next((p for p in phrases if "Again" in p["text"] and p["eligible"]), None)
        return {"summary": "Keep setup and payoff.", "cuts": [
            {"phrase_id": phrase["id"], "reason": "Repeated start."}] if phrase else []}

    def test_extract_splits_picture_and_retimes_linked_captions(self):
        project = self.pacing_document()
        original = project["document"]
        command = {"type": "sequence.extract", "start_frame": 90, "end_frame": 120, "cut_id": "test-cut-123"}
        changed = model.apply(original, [command])
        pictures = sorted((i for i in changed["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"])
        self.assertEqual([(p["at_frame"], p["source_in"], p["frames"]) for p in pictures], [(0, 0, 90), (90, 120, 780)])
        cues = [i for i in changed["items"] if i["kind"] == "caption"]
        self.assertEqual([(c["text"], c["at_frame"]) for c in cues], [("Setup.", 30), ("Payoff.", 180)])
        self.assertEqual(cues[1]["parent_id"], pictures[1]["id"])
        self.assertEqual(changed, model.apply(original, [command]))
        self.assertEqual(len(original["items"]), 4)

    def test_extract_preserves_protected_words_and_rejects_locked_shifts(self):
        original = self.pacing_document()["document"]
        cue = next(i for i in original["items"] if i.get("text") == "Payoff.")
        protected = model.apply(original, [{"type": "item.text", "item_id": cue["id"], "text": "Corrected name."}])
        operation = {"type": "sequence.extract", "start_frame": 90, "end_frame": 120, "cut_id": "test-cut-123"}
        changed = model.apply(protected, [operation])
        self.assertEqual(next(i for i in changed["items"] if i["id"] == cue["id"])["text"], "Corrected name.")
        self.assertIn(cue["id"], changed["protected"])
        with self.assertRaises(ValueError):
            model.apply(protected, [{**operation, "start_frame": 210, "end_frame": 240}])
        with self.assertRaises(ValueError):
            model.apply(original, [{**operation, "start_frame": 100}])
        locked = model.apply(original, [{"type": "track.lock", "track_id": "captions", "locked": True}])
        with self.assertRaises(ValueError):
            model.apply(locked, [operation])

    def test_pacing_proposal_applies_atomically_and_restores(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer):
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.get(connection, project["id"], proposal["id"])
                self.assertEqual(store.get(connection, project["id"])["revision"], 0)
            proposal = firstcut.public(row)
            self.assertEqual(proposal["status"], "ready")
            cut = proposal["windows"][0]["cuts"][0]
            body = self.batch(first_cut_id=proposal["id"], cut_ids=[cut["id"]])
            result = store.change(project["id"], body)
            self.assertEqual(result["revision"], 1)
            self.assertEqual(compile.duration(result["document"]), 870)
            self.assertEqual(store.change(project["id"], body), result)
            restored = store.change(project["id"], {"base_revision": 1, "op_id": model.identifier(), "restore_revision": 0})
            self.assertEqual(restored["document"], project["document"])
            with db.engine.connect() as connection:
                self.assertEqual(firstcut.get(connection, project["id"], proposal["id"])["status"], "applied")

    def test_pacing_covers_beginning_middle_and_end_without_cutting_unknown_footage(self):
        words = [{"s": second, "e": second + 1, "w": "Again."} for second in (3, 610, 1190)]
        project = self.pacing_document(1200, words)
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer) as ai:
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                saved = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual(ai.call_count, 3)
            self.assertEqual([w["status"] for w in saved["windows"]], ["reviewed", "retained", "reviewed", "reviewed"])
            cuts = [c for w in saved["windows"] for c in w["cuts"]]
            self.assertEqual([c["start_frame"] for c in cuts], [90, 18300, 35700])
            result = store.change(project["id"], self.batch(first_cut_id=proposal["id"], cut_ids=[c["id"] for c in cuts]))
            self.assertEqual(compile.duration(result["document"]), 35910)
            self.assertEqual(sum(i["frames"] for i in result["document"]["items"] if i["kind"] == "video"), 35910)

    def test_pacing_partial_retry_only_reviews_unfinished_windows(self):
        project = self.pacing_document(600, [{"s": s, "e": s + 1, "w": "Again."} for s in (3, 303)])
        def answer(phrases, context, instruction):
            if phrases[0]["start_frame"] >= firstcut.WINDOW:
                raise RuntimeError("Provider unavailable")
            return self.pacing_answer(phrases, context, instruction)
        with ctx.as_user(self.owner):
            proposal = self.queue_pacing(project)
            with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=answer):
                firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["windows"][1]["cuts"], [])
            with patch("studio.editor.firstcut.jobs.submit_heavy"):
                firstcut.control(project["id"], proposal["id"], "retry")
            with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer) as ai:
                firstcut.run(proposal["id"])
            self.assertEqual(ai.call_count, 1)
            with db.engine.connect() as connection:
                self.assertEqual(firstcut.get(connection, project["id"], proposal["id"])["status"], "ready")

    def test_pacing_rejects_fabricated_evidence_and_stale_acceptance(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner):
            proposal = self.queue_pacing(project)
            with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", return_value={"summary": "Bad ID", "cuts": [
                    {"phrase_id": "invented", "reason": "No evidence"}]}):
                firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["windows"][0]["cuts"], [])
            with patch("studio.editor.firstcut.jobs.submit_heavy"):
                firstcut.control(project["id"], proposal["id"], "retry")
            with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer):
                firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            cut_id = row["windows"][0]["cuts"][0]["id"]
            cue = next(i for i in project["document"]["items"] if i.get("text") == "Again.")
            store.change(project["id"], self.batch(ops=[{"type": "item.text", "item_id": cue["id"], "text": "My correction"}]))
            for base in (0, 1):
                with self.assertRaises(Exception) as caught:
                    store.change(project["id"], {"base_revision": base, "op_id": model.identifier(),
                                 "first_cut_id": proposal["id"], "cut_ids": [cut_id]})
                self.assertEqual(caught.exception.status_code, 409)
            with db.engine.connect() as connection:
                self.assertEqual(firstcut.get(connection, project["id"], proposal["id"])["status"], "ready")

    def test_pacing_request_retry_ownership_and_cancel_in_flight(self):
        project = self.pacing_document()
        body = {"revision": 0, "op_id": model.identifier()}
        path = "/api/editor/projects/" + project["id"]
        with patch("studio.editor.firstcut.jobs.submit_heavy") as queue:
            response = self.client.post(path + "/first-cuts", json=body)
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(self.client.post(path + "/first-cuts", json=body).json(), response.json())
            self.assertEqual(queue.call_count, 1)
            self.assertEqual(self.client.post(path + "/first-cuts", json={**body, "instruction": "Changed"}).status_code, 409)
            self.assertEqual(self.client.post(path + "/first-cuts", json={**body, "op_id": model.identifier()}).status_code, 429)
        proposal = response.json()
        self.assertEqual(self.client.delete(path).status_code, 409)
        with ctx.as_user("other_owner"), db.engine.connect() as connection:
            with self.assertRaises(Exception) as caught:
                firstcut.get(connection, project["id"], proposal["id"])
            self.assertEqual(caught.exception.status_code, 404)
        def cancelled(phrases, context, instruction):
            firstcut.control(project["id"], proposal["id"], "cancel")
            return self.pacing_answer(phrases, context, instruction)
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=cancelled):
            firstcut.run(proposal["id"])
        status = self.client.get(path + "/first-cuts/" + proposal["id"]).json()
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["windows"][0]["cuts"], [])
        self.assertEqual(self.client.delete(path).status_code, 200)

    def test_pacing_restart_preserves_completed_evidence(self):
        project = self.pacing_document(600, [{"s": s, "e": s + 1, "w": "Again."} for s in (3, 303)])
        with ctx.as_user(self.owner):
            proposal = self.queue_pacing(project)
            windows = proposal["windows"]
            windows[0].update(status="reviewed", summary="Already reviewed")
            with db.engine.begin() as connection:
                connection.execute(firstcut.passes.update().where(firstcut.passes.c.id == proposal["id"])
                                   .values(status="running", windows=store.encode(windows)))
            firstcut.recover()
            with patch("studio.editor.firstcut.jobs.submit_heavy"):
                firstcut.control(project["id"], proposal["id"], "retry")
            with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer) as ai:
                firstcut.run(proposal["id"])
            self.assertEqual(ai.call_count, 1)
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual(row["windows"][0]["summary"], "Already reviewed")
            self.assertEqual(row["status"], "ready")

    def test_pacing_keeps_untranscribed_and_protected_sections_without_ai_calls(self):
        document = model.import_clip({"start": 0, "end": 30}, [], 30)
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions") as ai:
            for current in (document, model.apply(self.document, [
                    {"type": "track.lock", "track_id": "picture", "locked": True}])):
                project = store.create(model.identifier(), "Retained", current, {})
                proposal = self.queue_pacing(project)
                firstcut.run(proposal["id"])
                with db.engine.connect() as connection:
                    row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
                self.assertEqual(row["status"], "ready")
                self.assertEqual(row["windows"][0]["status"], "retained")
            ai.assert_not_called()

    def test_pacing_selection_is_validated_and_recorded(self):
        project = self.pacing_document(30, [{"s": s, "e": s + 1, "w": "Again."} for s in (3, 7)])
        def answer(phrases, context, instruction):
            return {"summary": "Two repetitions", "cuts": [{"phrase_id": p["id"], "reason": "Repeated."} for p in phrases]}
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=answer):
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            cuts = row["windows"][0]["cuts"]
            with self.assertRaises(ValueError):
                store.change(project["id"], self.batch(first_cut_id=proposal["id"], cut_ids=["unknown-cut"]))
            with self.assertRaises(Exception) as caught:
                store.change(project["id"], self.batch(first_cut_id=proposal["id"], cut_ids=[cuts[0]["id"]],
                             ops=[{"type": "item.remove", "item_id": project["document"]["items"][0]["id"]}]))
            self.assertEqual(caught.exception.status_code, 409)
            result = store.change(project["id"], self.batch(first_cut_id=proposal["id"], cut_ids=[cuts[0]["id"]]))
            self.assertEqual(len([i for i in result["document"]["items"] if i["kind"] == "caption"]), 1)
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual([c["applied"] for c in row["windows"][0]["cuts"]], [True, False])

    def test_pacing_excessive_removal_is_retained(self):
        project = self.pacing_document(10, [{"s": 1, "e": 5, "w": "Again."}])
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer):
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["windows"][0]["cuts"], [])

    def applied_pacing(self, project):
        def answer(phrases, context, instruction):
            return {"summary": "Repeated starts", "cuts": [{"phrase_id": p["id"], "reason": "Repeated."}
                    for p in phrases if "Again" in p["text"] and p["eligible"]]}
        with patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=answer):
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
        with db.engine.connect() as connection:
            proposal = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
        cuts = [c for w in proposal["windows"] for c in w["cuts"]]
        result = store.change(project["id"], {"base_revision": project["revision"], "op_id": model.identifier(),
                              "first_cut_id": proposal["id"], "cut_ids": [c["id"] for c in cuts]})
        return proposal, result, cuts

    def test_restore_one_cut_keeps_corrections_and_removed_title(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner):
            result = store.change(project["id"], self.batch(ops=[{"type": "text.add", "new_id": "test-title-123",
                "track_id": "titles", "at_frame": 0, "frames": 60, "text": "Hook"}]))
            project.update(result)
            proposal, applied, cuts = self.applied_pacing(project)
            payoff = next(i for i in applied["document"]["items"] if i.get("text") == "Payoff.")
            changed = store.change(project["id"], {"base_revision": 2, "op_id": model.identifier(), "ops": [
                {"type": "item.text", "item_id": payoff["id"], "text": "Corrected name."},
                {"type": "item.remove", "item_id": "test-title-123"}]})
            body = {"base_revision": changed["revision"], "op_id": model.identifier(),
                    "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}}
            result = store.change(project["id"], body)
            self.assertEqual(store.change(project["id"], body), result)
            self.assertEqual(compile.duration(result["document"]), 900)
            self.assertFalse(any(i["kind"] == "title" for i in result["document"]["items"]))
            correction = next(i for i in result["document"]["items"] if i["id"] == payoff["id"])
            self.assertEqual((correction["text"], correction["at_frame"]), ("Corrected name.", 210))
            restored = next(i for i in result["document"]["items"] if i.get("text") == "Again.")
            self.assertIn(restored["id"], result["document"]["protected"])
            self.assertIn(restored["parent_id"], result["document"]["protected"])
            self.assertEqual(restored["at_frame"], 90)
            with self.assertRaises(Exception) as caught:
                store.change(project["id"], {**body, "base_revision": result["revision"], "op_id": model.identifier()})
            self.assertEqual(caught.exception.status_code, 409)
            subsequent = self.queue_pacing({**project, **result})
            phrase = next(p for w in subsequent["windows"] for p in w["phrases"] if "Again" in p["text"])
            self.assertFalse(phrase["eligible"])

    def test_restore_adjacent_cuts_in_either_order(self):
        # Both removals collapse to the same timeline boundary. Restores must recover their source order.
        for order in ((0, 1), (1, 0)):
            project = self.pacing_document(30, [{"s": 3, "e": 4, "w": "Again one."},
                {"s": 4, "e": 5, "w": "Again two."}, {"s": 8, "e": 9, "w": "Payoff."}])
            with ctx.as_user(self.owner):
                proposal, result, cuts = self.applied_pacing(project)
                self.assertEqual(len(cuts), 2)
                for index in order:
                    result = store.change(project["id"], {"base_revision": result["revision"], "op_id": model.identifier(),
                        "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[index]["id"]}})
                self.assertEqual(compile.duration(result["document"]), 900)
                cues = sorted((i for i in result["document"]["items"] if i["kind"] == "caption"), key=lambda i: i["at_frame"])
                self.assertEqual([(c["text"], c["at_frame"]) for c in cues],
                                 [("Again one.", 90), ("Again two.", 120), ("Payoff.", 240)])
                pictures = sorted((i for i in result["document"]["items"] if i["kind"] == "video"), key=lambda i: i["at_frame"])
                self.assertTrue(all(i["at_frame"] == i["source_in"] for i in pictures))

    def test_restore_disjoint_cuts_keeps_other_removal_and_survives_reopen(self):
        project = self.pacing_document(30, [{"s": 3, "e": 4, "w": "Again one."},
            {"s": 7, "e": 8, "w": "Again two."}, {"s": 12, "e": 13, "w": "Payoff."}])
        with ctx.as_user(self.owner):
            proposal, result, cuts = self.applied_pacing(project)
            result = store.change(project["id"], {"base_revision": 1, "op_id": model.identifier(),
                "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[1]["id"]}})
            self.assertEqual(compile.duration(result["document"]), 870)
            self.assertNotIn("Again one.", compile.captions(result["document"]))
            self.assertIn("Again two.", compile.captions(result["document"]))
            db.engine.dispose()
            with db.engine.connect() as connection:
                reopened = store.public(store.get(connection, project["id"]))
            self.assertEqual(reopened["document"], result["document"])
            result = store.change(project["id"], {"base_revision": 2, "op_id": model.identifier(),
                "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}})
            self.assertEqual(compile.duration(result["document"]), 900)

    def test_restore_cut_conflicts_do_not_mutate_the_draft(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner):
            proposal, applied, cuts = self.applied_pacing(project)
            body = {"base_revision": 1, "op_id": model.identifier(),
                    "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}}
            locked = store.change(project["id"], {"base_revision": 1, "op_id": model.identifier(),
                "ops": [{"type": "track.lock", "track_id": "captions", "locked": True}]})
            with self.assertRaises(Exception) as caught:
                store.change(project["id"], {**body, "base_revision": 2})
            self.assertEqual(caught.exception.status_code, 409)
            with db.engine.connect() as connection:
                self.assertEqual(store.public(store.get(connection, project["id"])), {**project, **locked})
            store.change(project["id"], {"base_revision": 2, "op_id": model.identifier(), "restore_revision": 1})
            right = next(i for i in applied["document"]["items"] if i["kind"] == "video" and i["at_frame"] == 90)
            changed = store.change(project["id"], {"base_revision": 3, "op_id": model.identifier(), "ops": [
                {"type": "item.move", "item_id": right["id"], "at_frame": 120}]})
            with self.assertRaises(Exception) as caught:
                store.change(project["id"], {**body, "base_revision": 4})
            self.assertEqual(caught.exception.status_code, 409)
            with db.engine.connect() as connection:
                self.assertEqual(store.get(connection, project["id"])["revision"], changed["revision"])

    def test_restore_cut_revision_undo_and_owner_checks(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner):
            proposal, applied, cuts = self.applied_pacing(project)
            body = {"base_revision": 1, "op_id": model.identifier(),
                    "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}}
            restored = store.change(project["id"], body)
            undone = store.change(project["id"], {"base_revision": 2, "op_id": model.identifier(), "restore_revision": 1})
            self.assertEqual(undone["document"], applied["document"])
            repeated = store.change(project["id"], {**body, "op_id": model.identifier(), "base_revision": 3})
            self.assertEqual(repeated["document"], restored["document"])
            with self.assertRaises(Exception) as caught:
                store.change(project["id"], {**body, "op_id": model.identifier()})
            self.assertEqual(caught.exception.status_code, 409)
        with ctx.as_user("other_owner"):
            other = store.create(model.identifier(), "Other draft", project["document"], {})
            with self.assertRaises(Exception) as caught:
                store.change(other["id"], {**body, "base_revision": 0})
            self.assertEqual(caught.exception.status_code, 404)

    def test_restore_cut_api_rejects_forged_requests(self):
        project = self.pacing_document()
        with ctx.as_user(self.owner):
            proposal, applied, cuts = self.applied_pacing(project)
        path = "/api/editor/projects/" + project["id"] + "/commands"
        body = {"base_revision": 1, "op_id": model.identifier(),
                "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}}
        for invalid in ({"ops": []}, {"first_cut_id": proposal["id"]}, {"restore_revision": 0},
                        {"restore_cut": {"pass_id": [], "cut_id": cuts[0]["id"]}}):
            response = self.client.post(path, json={**body, **invalid})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post(path, json=body).json(), response.json())

    def test_restore_head_tail_and_complete_middle_source_section(self):
        for start, end in ((0, 1), (9, 10)):
            project = self.pacing_document(10, [{"s": start, "e": end, "w": "Again."}])
            with ctx.as_user(self.owner):
                proposal, applied, cuts = self.applied_pacing(project)
                result = store.change(project["id"], {"base_revision": 1, "op_id": model.identifier(),
                    "restore_cut": {"pass_id": proposal["id"], "cut_id": cuts[0]["id"]}})
            self.assertEqual(compile.duration(result["document"]), 300)
            cue = next(i for i in result["document"]["items"] if i["kind"] == "caption")
            self.assertEqual(cue["at_frame"], start * 30)
        original = model.import_clip({"segments": [{"start": 0, "end": 4}, {"start": 10, "end": 11},
                                                  {"start": 20, "end": 25}]}, [], 25)
        cuts = [{"id": "middle-cut-123", "start_frame": 120, "end_frame": 150}]
        changed = model.apply(original, [{"type": "sequence.extract", "cut_id": cuts[0]["id"],
                                        "start_frame": 120, "end_frame": 150}])
        result = restoration.restore(changed, original, cuts, cuts[0]["id"], "test-pass")
        pictures = sorted(result["items"], key=lambda i: i["at_frame"])
        self.assertEqual([(i["source_in"], i["at_frame"], i["frames"]) for i in pictures],
                         [(0, 0, 120), (300, 120, 30), (600, 150, 150)])

    def test_pacing_cut_renders_exact_frames_and_keeps_corrected_caption(self):
        project = self.pacing_document(4, [{"s": .5, "e": 1, "w": "Again."}, {"s": 2, "e": 3, "w": "Payoff."}])
        cue = next(i for i in project["document"]["items"] if i.get("text") == "Payoff.")
        with ctx.as_user(self.owner), patch("studio.editor.firstcut.brain.editor_pacing_suggestions", side_effect=self.pacing_answer):
            result = store.change(project["id"], self.batch(ops=[{"type": "item.text", "item_id": cue["id"], "text": "Corrected name."}]))
            project.update(result)
            proposal = self.queue_pacing(project)
            firstcut.run(proposal["id"])
            with db.engine.connect() as connection:
                row = firstcut.public(firstcut.get(connection, project["id"], proposal["id"]))
            cut = row["windows"][0]["cuts"][0]
            result = store.change(project["id"], {"base_revision": 1, "op_id": model.identifier(),
                                 "first_cut_id": proposal["id"], "cut_ids": [cut["id"]]})
            document = result["document"]
            document["canvas"] = {"width": 320, "height": 180}
            source, destination = Path(_data.name) / "pacing-source.mp4", Path(_data.name) / "pacing-final.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4",
                            "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
            compile.render_sequence(source, document, destination, lambda value: None)
            info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                                                       "-of", "json", str(destination)]))
            picture = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual(int(picture["nb_read_frames"]), 105)
            self.assertIn("Corrected name.", compile.captions(document))
            self.assertNotIn("Again.", compile.captions(document))
            self.assertEqual(next(i for i in document["items"] if i["id"] == cue["id"])["at_frame"], 45)
            restored = store.change(project["id"], {"base_revision": 2, "op_id": model.identifier(),
                "restore_cut": {"pass_id": proposal["id"], "cut_id": cut["id"]}})["document"]
            restored["canvas"] = {"width": 320, "height": 180}
            compile.render_sequence(source, restored, destination, lambda value: None)
            info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                                                       "-of", "json", str(destination)]))
            picture = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual(int(picture["nb_read_frames"]), 120)
            self.assertIn("Corrected name.", compile.captions(restored))
            self.assertIn("Again.", compile.captions(restored))
            self.assertEqual(next(i for i in restored["items"] if i["id"] == cue["id"])["at_frame"], 60)

    def test_render_matches_revision_and_retained_text(self):
        source = Path(_data.name) / "render-source.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=320x180:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
        document = model.import_clip({"start": 0, "end": 2, "hook": "Saved title"},
                                     [{"s": .5, "e": 1, "w": "Saved caption"}], 4)
        document["canvas"] = {"width": 320, "height": 180}
        # Leave a deliberate silent gap before the picture and its captions.
        document = model.apply(document, [{"type": "item.move", "item_id": document["items"][0]["id"], "at_frame": 30}])
        with ctx.as_user(self.owner):
            project = store.create(model.identifier(), "Render check", document, {})
            directory = exports.folder(project["id"])
            directory.mkdir(parents=True)
            shutil.copyfile(source, directory / "source.media")
            with patch("studio.editor.exports.jobs.submit_heavy") as queue:
                output = exports.submit(project["id"], 0)
                self.assertEqual(exports.submit(project["id"], 0), output)
                self.assertEqual(queue.call_count, 1)
            title = next(i for i in document["items"] if i["kind"] == "title")
            store.change(project["id"], self.batch(ops=[{"type": "item.text", "item_id": title["id"], "text": "Later title"}]))
            exports.run(output["id"])
            with db.engine.connect() as connection:
                saved = exports.get(connection, project["id"], output["id"])
            self.assertEqual(saved["status"], "done", saved["error"])
            self.assertIn("Saved title", compile.captions(json.loads(saved["document"])))
            self.assertNotIn("Later title", compile.captions(json.loads(saved["document"])))
            self.assertEqual(saved["revision"], 0)
            path = directory / (output["id"] + ".mp4")
            info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                                                       "-of", "json", str(path)]))
            picture = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual(int(picture["nb_read_frames"]), 90)
            self.assertEqual((picture["width"], picture["height"]), (320, 180))
            pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(path),
                                             "-vf", "crop=2:2:0:160", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            self.assertLess(max(pixel), 8)
            uri = "/api/editor/projects/" + project["id"] + "/exports/" + output["id"] + "/media"
            response = self.client.get(uri, headers={"Range": "bytes=0-31"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.content), 32)
        with ctx.as_user("other_owner"):
            with db.engine.connect() as connection:
                with self.assertRaises(Exception) as caught:
                    exports.get(connection, project["id"], output["id"])
                self.assertEqual(caught.exception.status_code, 404)

    def test_import_retains_source_and_retry_is_idempotent(self):
        source = Path(_data.name) / "original.mp4"
        source.write_bytes(b"owned test source")
        job = {"id": "source-job", "status": "done", "title": "Original", "source_path": str(source),
               "dir": _data.name, "clips": [{"id": "clip1", "status": "done", "start": 0, "end": 5}]}
        body = {"job_id": job["id"], "clip_id": "clip1", "op_id": model.identifier()}
        with patch("studio.editor.routes.jobs.get", return_value=job), patch("studio.editor.routes.media.probe",
                return_value={"duration": 10, "width": 1920, "height": 1080}):
            response = self.client.post("/api/editor/projects", json=body)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(self.client.post("/api/editor/projects", json=body).json(), response.json())
        source.unlink()
        path = "/api/editor/projects/" + response.json()["id"]
        self.assertEqual(self.client.get(path + "/source").content, b"owned test source")
        self.assertEqual(self.client.delete(path).status_code, 200)
        self.assertEqual(self.client.get(path + "/source").status_code, 404)


if __name__ == "__main__":
    unittest.main()
