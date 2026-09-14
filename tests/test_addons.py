import re
import shutil
import subprocess

from helpers import (REPO, GoutTest, dc_offset, duration, ffmpeg, gout_attr, harmonic_db, loudness, samples,
                     stereo_correlation, thd_db, peak_db)

EXAMPLES = REPO / "examples" / "addons"
TREMOLO = EXAMPLES / "tremolo.py"


class AddonTest(GoutTest):
    def install(self, name: str = "tremolo.py", text: str | None = None):
        target = self.addons / name
        if text is None:
            shutil.copy(EXAMPLES / name, target)
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


class ExampleAddonTest(GoutTest):
    def install(self, *names: str) -> None:
        for name in names:
            shutil.copy(EXAMPLES / name, self.addons / name)

    def test_chorus_widens_a_mono_track_and_keeps_its_level(self):
        self.install("chorus.py")
        root = self.project("song", "tone.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("chorus", "1", "classic")
        self.gout("mix")
        self.assertLess(stereo_correlation(root / "master.wav"), 0.9)
        self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=3)
        self.assertGreater(duration(root / "master.wav"), 6.04)  # the voices ring on a little
        self.gout("chorus", "1", "v2", "w0")
        self.gout("mix")
        self.assertGreater(stereo_correlation(root / "master.wav"), 0.999)
        self.assertIn("voices at", self.gout("chorus", "1").stdout)
        self.assertIn("wide", self.gout("ch", "presets").stdout)
        self.gout("chorus", "1", "v9", ok=False)

    def test_saturation_bends_loud_peaks_and_leaves_quiet_ones(self):
        self.install("saturation.py")
        root = self.project("song", "tone.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.assertLess(thd_db(root / "master.wav", 330), -60)
        self.gout("sat", "1", "tanh", "d3")
        self.gout("mix")
        self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=0.5)  # oauto: quiet stays put
        self.assertLess(thd_db(root / "master.wav", 330), -40)
        self.gout("sat", "1", "hard", "d24")
        self.gout("mix")
        clipped = thd_db(root / "master.wav", 330)
        self.assertGreater(clipped, -20)
        self.gout("sat", "1", "hard", "d24", "m50")
        self.gout("mix")
        self.assertLess(thd_db(root / "master.wav", 330), clipped - 3)  # the clean half dilutes it
        out = self.gout("saturation", "1").stdout
        self.assertIn("comes out at", out)
        self.assertIn("hard d24 m50", out)
        self.assertIn("fuzz", self.gout("sat", "presets").stdout)
        self.gout("sat", "1", "d50", ok=False)

    def test_saturation_blend_lines_up_at_high_frequencies(self):
        self.install("saturation.py")
        high = self.tmp / "high.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=15000:duration=4", "-c:a", "pcm_s16le", str(high))
        self.project("song")
        root = self.cwd
        self.gout("add", str(high))
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("sat", "1", "tanh", "d0", "m50")  # a clean blend: any slip between the paths cancels
        self.gout("mix")
        self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=0.3)

    def test_distortion_harmonics_asymmetry_and_silence(self):
        self.install("distortion.py")
        root = self.project("song", "tone.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("dist", "1", "hard", "d36")
        self.gout("mix")
        self.assertGreater(thd_db(root / "master.wav", 330), -15)
        self.assertLess(harmonic_db(root / "master.wav", 330, 2), -60)  # symmetric clipping: odd harmonics only
        self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=1)  # oauto
        self.gout("dist", "1", "hard", "d36", "a30")
        self.gout("mix")
        self.assertGreater(harmonic_db(root / "master.wav", 330, 2), -45)  # asymmetry: even harmonics
        self.assertLess(abs(dc_offset(root / "master.wav")), 0.001)
        self.assertIn("measured on this track", self.gout("dist", "1").stdout)

        self.cwd = self.tmp
        root = self.project("click", "click.wav")
        self.gout("dist", "1", "fuzz")
        self.gout("mix")
        (mono,) = samples(root / "master.wav", 1)
        self.assertEqual(peak_db(mono[:int(1.9 * 48000)]), float("-inf"))  # silence stays silent

    def test_distortion_matches_the_level_of_noisy_material(self):
        self.install("distortion.py")
        root = self.project("song", "noise.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        for preset in ("overdrive", "highgain", "broken"):
            self.gout("dist", "1", preset)
            self.gout("mix")
            self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=1, msg=preset)

    def test_distortion_crush_manual_output_master_and_errors(self):
        self.install("distortion.py")
        root = self.project("song", "tone.wav")
        self.gout("dist", "1", "crush", "d0", "b4")
        self.gout("mix")
        self.assertGreater(thd_db(root / "master.wav", 330), -20)
        self.gout("dist", "1", "hard", "d36")
        self.gout("mix")
        matched = loudness(root / "master.wav")
        self.gout("dist", "1", "hard", "d36", "o-12")
        self.gout("mix")
        self.assertGreater(loudness(root / "master.wav"), matched + 3)
        self.gout("fx", "1", "clear")
        self.gout("dist", "master", "hard", "d30")
        self.gout("mix")
        self.assertIn("a -12 dBFS source", self.gout("dist", "master").stdout)
        for bad in (["crush", "a20"], ["soft", "b4"], ["d80"], ["h10"]):
            self.gout("dist", "1", *bad, ok=False)
        self.assertIn("overdrive", self.gout("dist", "presets").stdout)

    def test_all_examples_load_together(self):
        self.install("chorus.py", "distortion.py", "saturation.py", "tremolo.py")
        report = self.gout("addons").stdout
        for name in ("chorus", "distortion", "saturation", "tremolo"):
            self.assertIn(name, report)
        self.assertNotIn("not loaded", report)

    def test_the_guide_builds_a_phaser_that_keeps_its_level(self):
        guide = (REPO / "docs" / "addons.md").read_text()
        blocks = re.findall(r"```python\n(.*?)```", guide, re.S)
        (phaser,) = [b for b in blocks if "class Phaser" in b]
        (bars,) = [b for b in blocks if "class Bars" in b]
        (self.addons / "phaser.py").write_text(phaser)
        (self.addons / "bars.py").write_text(bars)
        report = self.gout("addons").stdout
        self.assertIn("phaser.py                phaser", report)
        self.assertIn("bars (screen, ctrl-b)", report)
        root = self.project("song", "noise.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        for line in (["swirl"], ["jet"], ["0.5hz", "d90", "t3"], ["2hz", "d0", "t5"]):
            self.gout("phaser", "1", *line)
            self.gout("mix")
            self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=1, msg=" ".join(line))
        self.assertIn("phaser 2hz d0 t5", self.gout("fx", "1").stdout)
        self.assertIn("the delay is 1.5 to 5 ms", self.gout("ph", "1", "t1", ok=False).stderr)
        self.assertIn("fast and deep", self.gout("phaser", "presets").stdout)

    def test_an_addon_says_which_api_it_needs_and_examples_copy_in(self):
        (self.addons / "future.py").write_text("def register(gout):\n    gout.requires(gout.version + 1)\n")
        (self.addons / "now.py").write_text("from gout.fx import Effect\n\nclass Now(Effect):\n    name = 'now'\n"
                                            "    def parse(self, text):\n        return {}\n    def format(self, params):\n"
                                            "        return ''\n\ndef register(gout):\n    gout.requires(1)\n"
                                            "    gout.add_effect(Now())\n")
        report = self.gout("addons").stdout
        self.assertIn("future.py                not loaded: it needs gout's addon API 2, and this gout has 1: "
                      "update gout", report)
        self.assertIn("now.py                   now", report)

        (self.addons / "tremolo.py").write_text("# my own tremolo\n")
        copied = self.gout("addons", "examples").stdout
        self.assertIn("tremolo.py               already there, kept as it is", copied)
        self.assertIn("fractal.py               copied", copied)
        self.assertEqual((self.addons / "tremolo.py").read_text(), "# my own tremolo\n")
        self.assertEqual((self.addons / "chorus.py").read_text(), (EXAMPLES / "chorus.py").read_text())
        self.gout("addons", "nonsense", ok=False)

    def test_gout_runs_itself_again(self):
        gout_command = gout_attr("core", "gout_command")
        result = subprocess.run([*gout_command(), "version"], capture_output=True, text=True, env=self.env(),
                                cwd=self.tmp)
        self.assertEqual(result.stdout.splitlines()[0], f"gout {gout_attr('core', '__version__')}")  # then warnings
        arrows = gout_attr("tui", "ARROW_NAMES")
        self.assertEqual((arrows[b"kLFT5"], arrows[b"kRIT3"], arrows[b"CTL_LEFT"], arrows[b"ALT_RIGHT"]),
                         ("left", "right", "left", "right"))
