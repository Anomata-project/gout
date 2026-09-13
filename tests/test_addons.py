import shutil

from helpers import REPO, GoutTest, loudness

TREMOLO = REPO / "examples" / "addons" / "tremolo.py"


class AddonTest(GoutTest):
    def install(self, name: str = "tremolo.py", text: str | None = None):
        target = self.addons / name
        if text is None:
            shutil.copy(TREMOLO, target)
        else:
            target.write_text(text)
        return target

    def test_example_addon_is_a_first_class_effect(self):
        self.install()
        root = self.project("song", "noise.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("trem", "1", "chop")
        self.gout("mix")
        self.assertLess(loudness(root / "master.wav"), dry - 2)
        self.assertIn("tremolo 12hz d100", self.gout("fx", "1").stdout)
        self.assertIn("[addon tremolo.py]", self.gout("fx", "kinds").stdout)
        self.assertIn("slow", self.gout("tremolo", "presets").stdout)
        self.assertIn("gout tremolo", self.gout("help").stdout)
        self.assertIn("tremolo", self.gout("cheat").stdout)
        self.assertIn("tremolo.py               tremolo", self.gout("addons").stdout)
        self.gout("tremolo", "1", "30hz", ok=False)
        self.assertEqual(self.dump()["tracks"][0]["fx"], [{"kind": "tremolo", "params": "12hz d100", "on": True}])

    def test_broken_and_clashing_addons_are_skipped_not_fatal(self):
        self.install("broken.py", "raise RuntimeError('boom')\n")
        self.install("clash.py", "from gout.fx import Effect\n"
                                 "class E(Effect):\n    name = 'eq'\n"
                                 "def register(gout):\n    gout.add_effect(E())\n")
        self.install("nohook.py", "x = 1\n")
        self.project("song", "tone.wav")
        result = self.gout("eq", "1", "hp80")
        self.assertIn("broken.py not loaded: RuntimeError: boom", result.stderr)
        self.assertIn("'eq' is already taken", result.stderr)
        report = self.gout("addons").stdout
        self.assertIn("nohook.py", report)
        self.assertIn("no register(gout) function", report)
        self.assertEqual(self.dump()["tracks"][0]["fx"][0]["kind"], "eq")

    def test_a_project_without_its_addon_says_so(self):
        addon = self.install()
        root = self.project("song", "tone.wav")
        self.gout("tremolo", "1", "slow")
        addon.unlink()
        self.assertIn("tremolo is not installed", self.gout("mix").stdout)
        self.assertIn("not installed: tremolo", self.gout("addons").stdout)
        self.assertIn("(not installed)", self.gout("fx", "1").stdout)
        self.assertTrue((root / "master.wav").exists())
        self.install()
        self.assertNotIn("not installed", self.gout("mix").stdout)

    def test_projects_never_load_code(self):
        root = self.project("song", "tone.wav")
        (root / "addons").mkdir()
        shutil.copy(TREMOLO, root / "addons" / "tremolo.py")
        self.assertNotIn("tremolo", self.gout("fx", "kinds").stdout)
        self.gout("tremolo", "1", "slow", ok=False)
