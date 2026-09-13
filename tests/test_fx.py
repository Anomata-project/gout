from helpers import GoutTest, difference_peak_db, duration, hits, loudness, samples, peak_db


class EffectsTest(GoutTest):
    def test_eq_high_pass_removes_a_low_tone_and_bypass_brings_it_back(self):
        root = self.project("song", "tone.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("eq", "1", "hp2k/24")
        self.gout("mix")
        self.assertLess(loudness(root / "master.wav"), dry - 30)
        self.assertIn("hp2k/24", self.gout("eq", "1").stdout)
        self.gout("eq", "1", "off")
        self.gout("mix")
        self.assertAlmostEqual(loudness(root / "master.wav"), dry, delta=0.2)

    def test_eq_shortcuts_presets_and_errors(self):
        self.project("song", "tone.wav")
        self.gout("hp", "1", "80")
        self.gout("lp", "1", "12k")
        self.assertIn("hp80 lp12k", self.gout("eq", "1").stdout)
        self.gout("eq", "1", "voice")
        self.assertIn("hs10k:+1", self.gout("eq", "1").stdout)
        self.assertIn("voice", self.gout("eq", "presets").stdout)
        self.gout("eq", "1", "bogus", ok=False)

    def test_compressor_lowers_a_loud_tone(self):
        root = self.project("song", "noise.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("comp", "1", "-30", "8:1", "a1", "r50")
        self.gout("mix")
        self.assertLess(loudness(root / "master.wav"), dry - 3)
        self.assertIn("works", self.gout("comp", "1").stdout)

    def test_delay_note_values_follow_the_tempo(self):
        root = self.project("song", "click.wav")
        self.gout("delay", "1", "1/8", ok=False)  # no bpm yet
        self.gout("set", "bpm", "120")
        self.gout("delay", "1", "1/8", "w50", "f50", "n3")
        self.gout("mix")
        found = hits(root / "master.wav")
        self.assertEqual(len(found), 4)
        for (t, level), (want_t, want_level) in zip(found, [(2.0, 1.0), (2.25, 0.5), (2.5, 0.25), (2.75, 0.125)]):
            self.assertAlmostEqual(t, want_t, delta=0.002)
            self.assertAlmostEqual(level, want_level, delta=0.02)
        self.gout("set", "bpm", "off", ok=False)  # a delay still uses the tempo

    def test_reverb_tail_lengthens_the_master_and_stems_still_sum_to_it(self):
        root = self.project("song", "click.wav", "bass.wav")
        self.gout("reverb", "1", "hall")
        self.gout("eq", "2", "hp80")
        self.gout("comp", "2", "vocal")
        self.gout("mix")
        self.assertAlmostEqual(duration(root / "master.wav"), 4.0 + 2.625, delta=0.01)
        self.gout("stems")
        stems = sorted((root / "stems").iterdir())
        self.assertEqual([s.name for s in stems], ["01-click.wav", "02-bass.wav"])
        self.assertEqual({round(duration(s), 3) for s in stems}, {6.625})
        from helpers import ffmpeg
        summed = self.tmp / "sum.wav"
        ffmpeg("-i", str(stems[0]), "-i", str(stems[1]), "-filter_complex",
               "[0:a][1:a]amix=inputs=2:normalize=0:duration=longest[s]", "-map", "[s]",
               "-c:a", "pcm_f32le", str(summed))
        self.assertLess(difference_peak_db(summed, root / "master.wav"), -90)

    def test_reverb_is_wide(self):
        root = self.project("song", "click.wav")
        self.gout("reverb", "1", "2s", "p0", "d20", "w100")
        self.gout("mix")
        left, right = samples(root / "master.wav", 2)
        start = int(2.05 * 48000)
        l, r = left[start:], right[start:]
        num = sum(a * b for a, b in zip(l, r))
        den = (sum(a * a for a in l) * sum(b * b for b in r)) ** 0.5
        self.assertLess(abs(num / den), 0.1)

    def test_master_effects(self):
        root = self.project("song", "tone.wav")
        self.gout("mix")
        dry = loudness(root / "master.wav")
        self.gout("eq", "master", "hp2k/24")
        self.gout("mix")
        self.assertLess(loudness(root / "master.wav"), dry - 30)
        self.gout("eq", "master", "off")
        self.gout("reverb", "master", "room")
        self.gout("mix")
        self.assertAlmostEqual(duration(root / "master.wav"), 6.0 + 0.805, delta=0.01)

    def test_effects_survive_the_sidecar_round_trip(self):
        root = self.project("song", "tone.wav")
        self.gout("eq", "1", "hp80", "+3@200")
        self.gout("comp", "1", "glue")
        self.gout("reverb", "master", "plate")
        (root / "gout.db").unlink()
        self.gout("rebuild")
        self.assertIn("hp80 +3@200", self.gout("eq", "1").stdout)
        self.assertIn("-16 2:1", self.gout("comp", "1").stdout)
        self.assertIn("1.8s", self.gout("reverb", "master").stdout)
