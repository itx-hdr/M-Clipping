"""Check real montage output, caption timing, source bounds and crop validation without AI or real videos."""
import atexit
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix="mclipper-editing-")
atexit.register(_data.cleanup)
os.environ["MCLIPPER_DATA"] = _data.name
os.environ["DATABASE_URL"] = ""
from studio import editing, render, jobs


class EditingTests(unittest.TestCase):
    def test_bad_sections_are_rejected(self):
        for plan in ([{"start":2,"end":4},{"start":1,"end":3}], [{"start":0,"end":4},{"start":3,"end":6}],
                     [{"start":0,"end":float("inf")}], [{"start":0,"end":1}], [{"start":0,"end":7}]):
            with self.subTest(plan=plan), self.assertRaises(ValueError): editing.cuts(plan,0,6)

    def test_caption_times_skip_removed_words_and_do_not_split_words(self):
        words=[{"w":"First","s":.3,"e":.7},{"w":"REMOVE","s":2.2,"e":2.6},{"w":"Last","s":4,"e":4.4}]
        plan=editing.cuts([{"start":0,"end":1.6},{"start":3.6,"end":5.2}],0,6)
        kept,total=editing.timeline(words,plan)
        self.assertEqual([w["w"] for w in kept],["First","Last"])
        self.assertAlmostEqual(total,3.2);self.assertAlmostEqual(kept[1]["s"],2)
        with self.assertRaises(ValueError):editing.check_speech_boundaries(words,[{"start":.5,"end":4.6}])

    def test_invalid_crop_is_rejected(self):
        for r in ({"x":.9,"y":0,"width":.2,"height":1},{"x":0,"y":0,"width":float("nan"),"height":1}):
            with self.assertRaises(ValueError):editing.framing({"gameplay":r,"reaction":r})

    def test_request_rejects_outside_source_before_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            job={"id":"editing-test","dir":tmp,"duration":6,"clips":[{"id":"clip01","start":0,"end":5,"layout":"blur","style":"clean","status":"done"}]}
            with patch.dict(jobs._jobs,{job["id"]:job}), patch.object(jobs,"save"), patch.object(jobs._q,"put") as queued:
                with self.assertRaises(ValueError):jobs.request_render(job["id"],"clip01",{"segments":[{"start":0,"end":7}]})
                queued.assert_not_called();self.assertEqual(job["clips"][0]["status"],"done")

    def test_editor_metadata_save_keeps_plan_until_times_are_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sections=[{"start":0,"end":2},{"start":4,"end":6}]
            job={"id":"editing-save","dir":tmp,"duration":6,"clips":[{"id":"clip01","start":0,"end":6,"layout":"blur","style":"clean",
                "segments":sections,"framing":{"kept":True},"status":"done"}]}
            with patch.dict(jobs._jobs,{job["id"]:job}),patch.object(jobs,"save"),patch.object(jobs._q,"put"):
                jobs.request_render(job["id"],"clip01",{"start":0,"end":6,"layout":"blur","hook":"NEW HOOK"})
                self.assertEqual(job["clips"][0]["segments"],sections)
                self.assertTrue(job["clips"][0]["framing"]["kept"])
                jobs.request_render(job["id"],"clip01",{"start":1,"end":6})
                self.assertNotIn("segments",job["clips"][0])

    def test_real_render_removes_middle_and_preserves_caption_and_panel_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'source.mp4'
            command=['ffmpeg','-y','-v','error']
            for color in ('red','green','blue'):command+=['-f','lavfi','-i',f'color=c={color}:s=320x180:r=30:d=2']
            command+=['-f','lavfi','-i','sine=frequency=600:duration=6','-filter_complex','[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
                      '-map','[v]','-map','3:a','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(src)]
            subprocess.run(command,check=True,capture_output=True)
            render.install_fonts()
            plan=[{'start':0,'end':1.6},{'start':4,'end':5.6}]
            clip={'start':0,'end':5.6,'segments':plan,'style':'clean','hook':'COLOR TEST',
                  'framing':{'gameplay':{'x':0,'y':0,'width':1,'height':1},'reaction':{'x':.5,'y':0,'width':.5,'height':1}}}
            words=[{'w':'First','s':.3,'e':.7},{'w':'REMOVE','s':2.2,'e':2.6},{'w':'Last','s':4.4,'e':4.8}]
            out=render.render_clip(src,root/'clips','clip01',clip,words,320,180)
            self.assertAlmostEqual(out['duration'],3.2,delta=.15)
            ass=(root/'clips/clip01.ass').read_text();self.assertNotIn('REMOVE',ass);self.assertIn('Last',ass)
            for t,channel in ((.5,0),(2.5,2)):
                for y in (650,1500):
                    rgb=subprocess.check_output(['ffmpeg','-v','error','-ss',str(t),'-i',str(root/'clips/clip01.mp4'),
                         '-frames:v','1','-vf',f'crop=2:2:540:{y},scale=1:1,format=rgb24','-f','rawvideo','-'])
                    self.assertGreater(rgb[channel],200);self.assertLess(rgb[2-channel],50)


if __name__=='__main__':unittest.main()
