"""The MCP workflow must not queue unverified sources or export a different file from the one reviewed."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from mclipper_mcp.core import Bridge, youtube_url

URL = "https://www.youtube.com/watch?v=abcdefghijk"
CHECKS = {k: True for k in ("authentic_gameplay", "truthful_hook", "standalone_context", "payoff_visible", "natural_ending",
                           "captions_checked", "framing_checked", "audio_checked", "watched_full_video")}
NOTES = "Inspected actual player gameplay and confirmed the game using primary official evidence."


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "NICHE.md").write_text("GTA VI gameplay only, English, verified footage.", encoding="utf-8")
        self.requests = []
        self.media = b"first reviewed render"
        self.clip = {"id": "clip01", "file": "clip01.mp4", "version": 1, "status": "done"}
        self.editing = {"version":1}
        def handler(req):
            self.requests.append(req)
            if req.url.path == "/api/status":
                return httpx.Response(200, json={"editing":self.editing})
            if req.url.path == "/api/jobs" and req.method == "POST":
                return httpx.Response(200, json={"id": "job-123", "clips": []})
            if req.url.path == "/api/jobs/job-123":
                return httpx.Response(200, json={"id": "job-123", "clips": [self.clip], "source": {"url": URL}})
            if req.url.path.startswith("/media/"):
                return httpx.Response(200, content=self.media)
            if req.url.path == "/api/jobs/job-123/clips/clip01/render" and req.method == "POST":
                return httpx.Response(200, json={"queued":True})
            return httpx.Response(404)
        self.bridge = Bridge(self.root, self.root / "data", httpx.MockTransport(handler))

    def tearDown(self):
        self.bridge.http.close()
        self.tmp.cleanup()

    def samples(self):
        for i, t in enumerate((1, 40)):
            self.bridge.put("sample:" + str(i), {"id": str(i), "url": URL, "at": t, "brief": self.bridge.brief()["version"],
                            "image_sha256": str(i), "created": time.time()})

    def approve(self, **changes):
        self.samples()
        args = dict(url=URL, game="Grand Theft Auto VI", footage_type="player_gameplay", authenticity="verified", language="English",
                    start=0, end=90, sample_ids=["0", "1"], evidence_urls=["https://www.rockstargames.com/VI"], notes=NOTES)
        args.update(changes)
        return self.bridge.review_source(**args)

    def inspection(self):
        self.approve()
        self.bridge.queue(URL)
        self.bridge.put("inspection:job-123:clip01", {"sha256": hashlib.sha256(self.media).hexdigest(), "version": 1,
                        "brief": self.bridge.brief()["version"], "media_passed": True})

    def review(self, **changes):
        args = dict(jid="job-123", cid="clip01", game="Grand Theft Auto VI", footage_type="player_gameplay", language="English",
                    checks=dict(CHECKS), dimensions={k:18 for k in ("hook", "clarity", "payoff", "energy", "shareability")},
                    penalty=2, issues=[], notes=NOTES)
        args.update(changes)
        return self.bridge.review_clip(**args)

    def test_titles_and_popularity_do_not_approve_a_source(self):
        with self.assertRaises(ValueError): self.bridge.queue(URL)
        self.assertEqual(self.requests, [])

    def test_wrong_game_uncertain_trailer_and_language_are_rejected(self):
        for changes in ({"game":"Grand Theft Auto V"}, {"authenticity":"uncertain"}, {"footage_type":"trailer_reaction"}, {"language":"Hindi"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.approve(**changes)

    def test_source_review_requires_separated_actual_samples_and_primary_evidence(self):
        with self.assertRaises(ValueError): self.approve(sample_ids=["0"])
        with self.assertRaises(ValueError): self.approve(evidence_urls=["https://example.com/gta6"])
        self.samples()
        sample=self.bridge.get("sample:1");sample["image_sha256"]="0";self.bridge.put("sample:1",sample)
        with self.assertRaises(ValueError):
            self.bridge.review_source(URL,"Grand Theft Auto VI","player_gameplay","verified","English",0,90,["0","1"],
                                      ["https://www.rockstargames.com/VI"],NOTES)

    def test_queue_uses_exact_reviewed_source_and_range(self):
        self.approve();self.bridge.queue(URL,100)
        body=json.loads(self.requests[-1].content)
        self.assertEqual((body["options"]["range_start"],body["options"]["range_end"]),(0,90))
        self.assertEqual(body["options"]["max_clips"],4)
        with self.assertRaises(ValueError): self.bridge.queue("https://youtu.be/12345678901")

    def test_changed_brief_invalidates_approval(self):
        self.approve();(self.root/"NICHE.md").write_text("New scope",encoding="utf-8")
        with self.assertRaises(ValueError): self.bridge.queue(URL)

    def test_review_requires_current_inspected_render_and_correct_game(self):
        self.approve();self.bridge.queue(URL)
        with self.assertRaises(ValueError): self.review()
        self.inspection();self.clip["version"]=2
        with self.assertRaises(ValueError): self.review()
        self.clip["version"]=1
        with self.assertRaises(ValueError): self.review(game="Minecraft")

    def test_grade_is_calculated_and_missing_audio_or_issues_block_export(self):
        self.inspection()
        result=self.review();self.assertEqual(result["score"],88);self.assertTrue(result["passed"])
        self.assertFalse(self.review(checks={**CHECKS,"audio_checked":False})["passed"])
        self.assertFalse(self.review(issues=["Ending is cut off"])["passed"])
        with self.assertRaises(ValueError): self.bridge.export("job-123","clip01")

    def test_one_weak_dimension_cannot_hide_behind_high_total(self):
        self.inspection()
        self.assertFalse(self.review(dimensions={"hook":20,"clarity":20,"payoff":8,"energy":20,"shareability":20},penalty=0)["passed"])

    def test_changed_file_bytes_require_another_review_even_with_same_version(self):
        self.inspection();self.review();self.media=b"different render"
        with self.assertRaisesRegex(ValueError,"render changed"): self.bridge.export("job-123","clip01")

    def test_pending_render_cannot_reuse_a_previous_approval(self):
        self.inspection();self.review();self.clip["status"]="queued"
        with self.assertRaises(ValueError):self.review()
        with self.assertRaises(ValueError):self.bridge.export("job-123","clip01")

    def test_edit_plan_binds_every_section_and_invalidates_old_review(self):
        self.inspection();self.review()
        with self.assertRaises(ValueError):self.bridge.edit_plan("job-123","clip01",[{"start":89,"end":94}],None,"A TRUE HOOK")
        self.bridge.edit_plan("job-123","clip01",[{"start":1,"end":3},{"start":5,"end":7}],None,"A TRUE HOOK")
        body=json.loads(self.requests[-1].content)
        self.assertEqual(len(body["segments"]),2)
        self.assertIsNone(self.bridge.get("review:job-123:clip01"))
        self.assertIsNone(self.bridge.get("inspection:job-123:clip01"))

    def test_old_server_cannot_silently_ignore_a_montage(self):
        self.inspection();self.editing={};before=len(self.requests)
        with self.assertRaisesRegex(ValueError,"Restart M clipper"):
            self.bridge.edit_plan("job-123","clip01",[{"start":1,"end":5}],None,"A TRUE HOOK")
        self.assertFalse(any(r.method=="POST" for r in self.requests[before:]))

    def test_same_reviewed_file_exports_video_and_evidence(self):
        self.inspection();self.review()
        with patch.object(self.bridge,"run",return_value=b""):
            exported=self.bridge.export("job-123","clip01")
        self.assertEqual(Path(exported["file"]).read_bytes(),self.media)
        self.assertEqual(json.loads((Path(exported["file"]).parent/"review.json").read_text())["review"]["score"],88)

    def test_unsafe_urls_ids_and_remote_server_are_rejected(self):
        for value in ("https://example.com/watch?v=abcdefghijk","https://youtube.com.evil/watch?v=abcdefghijk","http://youtu.be/abcdefghijk"):
            with self.assertRaises(ValueError): youtube_url(value)
        with self.assertRaises(ValueError):self.bridge.clip("../../outside","clip01")
        with patch.dict("os.environ",{"MCLIPPER_MCP_URL":"http://example.com"}):
            with self.assertRaises(ValueError):Bridge(self.root,self.root/"remote")


if __name__ == "__main__":
    unittest.main()
