import json
import shutil

from helpers import GoutTest


class ProjectTest(GoutTest):
    def test_new_project_layout(self):
        root = self.project()
        self.assertTrue((root / "gout.db").is_file())
        self.assertTrue((root / "master").is_dir())
        self.assertTrue((root / "gout.json").is_file())

    def test_add_copies_wav_and_mp3_and_converts_other_formats(self):
        root = self.project("song", "bass.wav", "click.mp3", "vox.flac")
        self.assertEqual(sorted(p.name for p in (root / "master").iterdir()),
                         ["bass.wav", "click.mp3", "vox.wav"])
        names = [t["name"] for t in self.dump()["tracks"]]
        self.assertEqual(names, ["bass", "click", "vox"])

    def test_short_command_names(self):
        self.project()
        self.gout("a", str(self.fx / "bass.wav"), "-a", "1s", "-n", "low")
        track = self.dump()["tracks"][0]
        self.assertEqual((track["name"], track["offset_ms"]), ("low", 1000))

    def test_move_relative_absolute_and_negative(self):
        self.project("song", "bass.wav")
        self.gout("move", "1", "+1.5s")
        self.assertEqual(self.dump()["tracks"][0]["offset_ms"], 1500)
        self.gout("move", "bass", "-500ms")
        self.assertEqual(self.dump()["tracks"][0]["offset_ms"], 1000)
        self.gout("move", "1", "=-2s")
        self.assertEqual(self.dump()["tracks"][0]["offset_ms"], -2000)

    def test_rm_keeps_the_file_and_rm_delete_removes_it(self):
        root = self.project("song", "bass.wav", "click.wav")
        self.gout("rm", "1")
        self.assertTrue((root / "master" / "bass.wav").exists())
        self.gout("rm", "click", "-D")
        self.assertFalse((root / "master" / "click.wav").exists())
        self.gout("undo", ok=False)  # rm -D cannot be undone

    def test_undo_add_removes_the_copy_but_scan_keeps_user_files(self):
        root = self.project()
        self.gout("add", str(self.fx / "bass.wav"))
        self.gout("undo")
        self.assertFalse((root / "master" / "bass.wav").exists())
        shutil.copy(self.fx / "click.wav", root / "master" / "mine.wav")
        self.gout("scan")
        self.assertEqual([t["name"] for t in self.dump()["tracks"]], ["mine"])
        self.gout("undo")
        self.assertTrue((root / "master" / "mine.wav").exists())
        self.assertEqual(self.dump()["tracks"], [])

    def test_sidecar_matches_dump_and_rebuild_restores_from_it(self):
        root = self.project("song", "bass.wav", "click.wav")
        self.gout("move", "2", "1.5s")
        self.gout("trim", "1", "-st", "500ms", "-et", "3s")
        self.gout("gain", "1", "-4")
        self.gout("set", "lufs", "-14")
        self.assertEqual(json.loads((root / "gout.json").read_text()), self.dump())
        before = self.gout("ls").stdout
        (root / "gout.db").unlink()
        self.gout("rebuild")
        self.gout("set", "autorender", "off")
        self.assertEqual(self.gout("ls").stdout.splitlines()[1:], before.splitlines()[1:])
        self.assertIn("-14", self.gout("set").stdout)

    def test_import_settings_only_into_another_project(self):
        self.project("a", "bass.wav")
        self.gout("set", "lufs", "-16")
        self.gout("set", "title", "Template")
        source = self.tmp / "a" / "gout.json"
        self.cwd = self.tmp
        self.project("b", "click.wav")
        self.gout("import", str(source), "-s")
        listing = self.gout("set").stdout
        self.assertIn("-16", listing)
        self.assertIn("Template", listing)
        self.assertEqual([t["name"] for t in self.dump()["tracks"]], ["click"])

    def test_saveas_copies_the_whole_project(self):
        root = self.project("song", "bass.wav")
        self.gout("saveas", "song-v2")
        copy = self.tmp / "song-v2"
        self.assertTrue((copy / "master" / "bass.wav").exists())
        self.assertIn("song-v2", self.gout("set", cwd=copy).stdout)
        self.assertTrue((root / "gout.db").exists())
