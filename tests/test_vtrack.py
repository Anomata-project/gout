"""Video tracks: a picture with a position and soft trims, silent in the mix."""
import hashlib
import json
import shutil

from helpers import ffmpeg, GoutTest


def digest(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


class VideoTrackTest(GoutTest):
    def video(self, root, at: str = "0"):
        self.gout("add", str(self.fx / "clip.mp4"), "-a", at)
        return next(t for t in self.dump()["tracks"] if t["kind"] == "video")

    def test_add_probes_and_lists_it(self):
        root = self.project("song", "tone.wav")
        out = self.gout("add", str(self.fx / "clip.mp4"), "-a", "1s").stdout
        self.assertIn("64x36", out)
        self.assertIn("12.000 fps", out)
        self.assertIn("48 frames", out)
        self.assertIn("its sound is not used", out)
        self.assertTrue((root / "master" / "clip.mp4").is_file())
        ls = self.gout("ls").stdout
        self.assertIn("video", ls)
        self.assertIn("64x36 12 fps", ls)
        video = self.dump()["tracks"][1]
        self.assertEqual((video["kind"], video["offset_ms"], video["length_ms"]), ("video", 1000, 4000))
        self.assertEqual(video["video"]["frames"], 48)
        self.assertNotIn("fx", video)
        self.assertNotIn("gain_db", video)

    def test_a_still_image_is_not_a_video(self):
        self.project("song")
        image = self.tmp / "still.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=32x32:d=1", "-frames:v", "1", str(image))
        self.assertIn("no audio stream", self.gout("add", str(image), ok=False).stderr)

    def test_a_picture_changes_nothing_you_hear(self):
        root = self.project("song", "tone.wav")
        self.gout("mix")
        self.assertIn("master.wav", self.gout("ls").stdout)
        self.assertNotIn("out of date", self.gout("ls").stdout)
        self.gout("add", str(self.fx / "clip.mp4"))
        self.gout("move", "2", "+2s")
        self.gout("trim", "2", "-st", "1s")
        self.assertNotIn("out of date", self.gout("ls").stdout)  # the fingerprint leaves pictures out
        self.gout("mix")  # and the mix goes on hearing one track

    def test_a_project_of_only_a_picture_has_nothing_to_hear(self):
        self.project("song")
        self.gout("add", str(self.fx / "clip.mp4"))
        self.assertIn("nothing audible", self.gout("mix").stdout)

    def test_move_trim_rm_and_undo(self):
        root = self.project("song", "tone.wav")
        self.video(root, "1s")
        source = digest(root / "master" / "clip.mp4")
        self.gout("move", "2", "+500ms")
        self.gout("trim", "2", "-st", "1s", "-et", "3s")
        video = self.dump()["tracks"][1]
        self.assertEqual((video["offset_ms"], video["in_ms"], video["out_ms"]), (1500, 1000, 3000))
        ls = self.gout("ls").stdout
        self.assertIn("00:00:02.500", ls)  # heard from the trim: 1.5 s + 1 s
        self.gout("move", "all", "+1s")
        self.assertEqual([t["offset_ms"] for t in self.dump()["tracks"]], [1000, 2500])  # the picture moves with the rest
        self.gout("undo")
        self.gout("undo")
        self.gout("undo")
        video = self.dump()["tracks"][1]
        self.assertEqual((video["offset_ms"], video["in_ms"], video["out_ms"]), (1000, 0, None))
        self.gout("rm", "2")
        self.assertEqual(len(self.dump()["tracks"]), 1)
        self.assertTrue((root / "master" / "clip.mp4").is_file())  # rm keeps the file
        self.gout("undo")
        self.assertEqual(self.dump()["tracks"][1]["video"]["frames"], 48)
        self.assertEqual(digest(root / "master" / "clip.mp4"), source)  # never touched

    def test_undoing_an_add_takes_the_copy_away(self):
        root = self.project("song", "tone.wav")
        self.video(root)
        self.gout("undo")
        self.assertFalse((root / "master" / "clip.mp4").exists())
        self.assertEqual(len(self.dump()["tracks"]), 1)

    def test_what_is_for_sound_says_so(self):
        root = self.project("song", "tone.wav")
        self.video(root)
        for words in (("gain", "2", "-3"), ("pan", "2", "L30"), ("mute", "2"), ("solo", "2"), ("part", "2", "1s"),
                      ("fx", "2", "add", "eq"), ("eq", "2", "hp80"), ("duplicate", "2", "0", "1s")):
            self.assertIn("is a video track", self.gout(*words, ok=False).stderr, words)
        self.assertIn("never rewrites a picture", self.gout("trim", "2", "-H", "-st", "1s", ok=False).stderr)
        self.gout("mute", "all", "on")  # all means the sound
        self.assertEqual(self.dump()["tracks"][1].get("mute"), None)

    def test_dump_import_and_rebuild(self):
        root = self.project("song", "tone.wav")
        self.video(root, "1s")
        doc = self.dump()
        doc["tracks"][1]["video"].update(mode="flow", slow=3, opacity=0.5, fade_in_ms=0, tail="loop",
                                         warp=[[0, 0], [2000, 1000], [4000, 3000]], want_ms=[0, 4000])
        doc["tracks"][1].update(offset_ms=2000, in_ms=500)
        path = self.tmp / "doc.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.gout("import", str(path))
        video = self.dump()["tracks"][1]
        self.assertEqual((video["offset_ms"], video["in_ms"]), (2000, 500))
        self.assertEqual((video["video"]["mode"], video["video"]["slow"], video["video"]["opacity"]), ("flow", 3, 0.5))
        self.assertEqual(video["video"]["warp"], [[0, 0], [2000, 1000], [4000, 3000]])
        self.gout("undo")
        self.assertEqual(self.dump()["tracks"][1]["offset_ms"], 1000)
        self.gout("import", str(path))
        before = self.dump()
        self.gout("rebuild", "-f")
        after = self.dump()
        self.assertEqual(after["tracks"], before["tracks"])  # from the files and gout.json alone

    def test_bad_video_settings_in_a_document_are_skipped_with_a_warning(self):
        root = self.project("song")
        self.video(root)
        doc = self.dump()
        doc["tracks"][0]["video"].update(mode="sparkle")
        path = self.tmp / "doc.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = self.gout("import", str(path)).stdout
        self.assertIn("bad video settings", out)
        self.assertEqual(self.dump()["tracks"][0]["video"]["mode"], "blend")

    def test_scan_finds_a_picture_put_in_master_and_ignores_sound_only_files(self):
        root = self.project("song", "tone.wav")
        shutil.copy(self.fx / "clip.mp4", root / "master" / "dropped.mp4")
        ffmpeg("-i", str(self.fx / "tone.wav"), "-c:a", "aac", str(root / "master" / "sound.mp4"))
        out = self.gout("scan").stdout
        self.assertIn("dropped", out)
        self.assertNotIn("sound.mp4", out)
        self.assertEqual([t["kind"] for t in self.dump()["tracks"]], ["wav", "video"])
        self.assertIn("nothing new", self.gout("scan").stdout)

    def test_the_timeline_shows_a_bar(self):
        root = self.project("song", "tone.wav")
        self.video(root, "1s")
        view = self.gout("view", "-w", "80").stdout
        bar = next(line for line in view.splitlines() if "clip" in line)
        self.assertIn("▶", bar)
        self.assertIn("█", bar)
        start = bar.index("█") - 16  # the label is 16 wide; 6 s over the rest: 1 s in
        self.assertGreater(start, 5)
        self.gout("trim", "2", "-st", "2s")
        bar = next(line for line in self.gout("view", "-w", "80").stdout.splitlines() if "clip" in line)
        self.assertGreater(bar.index("█") - 16, start)
