from helpers import GoutTest, duration, loudness, peak_time, samples, peak_db, stream_info


class MixTest(GoutTest):
    def test_position_places_the_sound(self):
        root = self.project("song", "click.wav")
        self.gout("move", "1", "1s")
        self.gout("mix")
        self.assertAlmostEqual(peak_time(root / "master.wav"), 3.0, delta=0.001)
        info = stream_info(root / "master.wav")
        self.assertEqual((info["channels"], info["sample_rate"], info["codec_name"]), (2, "48000", "pcm_f32le"))

    def test_soft_trim_keeps_the_sound_in_place(self):
        root = self.project("song", "click.wav")
        self.gout("move", "1", "1s")
        self.gout("trim", "1", "-st", "1s", "-et", "3s")
        self.gout("mix")
        self.assertAlmostEqual(peak_time(root / "master.wav"), 3.0, delta=0.001)
        self.assertAlmostEqual(duration(root / "master.wav"), 4.0, delta=0.01)

    def test_hard_trim_wav_is_exact_and_mp3_within_a_millisecond(self):
        for fixture in ("click.wav", "click.mp3"):
            with self.subTest(fixture=fixture):
                self.cwd = self.tmp
                root = self.project(fixture.replace(".", "-"), fixture)
                self.gout("trim", "1", "-st", "1.013s", "-et", "3s")
                self.gout("trim", "1", "--hard")
                self.gout("mix")
                self.assertAlmostEqual(peak_time(root / "master.wav"), 2.0,
                                       delta=0.0001 if fixture.endswith("wav") else 0.001)

    def test_a_sum_louder_than_0_lufs_still_reaches_the_target(self):
        root = self.project("song", "tone.wav")  # 6 s at -18 dBFS
        self.gout("gain", "1", "12")
        self.gout("set", "gain", "24")  # about +14 LUFS before the loudness step; loudnorm takes 0 at most
        self.gout("set", "lufs", "-14")
        out = self.gout("mix").stdout
        self.assertIn("linear", out)
        self.assertAlmostEqual(loudness(root / "master.wav"), -14.0, delta=0.5)

    def test_mute_leaves_nothing_audible(self):
        root = self.project("song", "click.wav")
        self.gout("mute", "1")
        out = self.gout("mix").stdout
        self.assertIn("nothing audible", out)
        self.assertFalse((root / "master.wav").exists())

    def test_pan_hard_left_silences_the_right_channel(self):
        root = self.project("song", "tone.wav")
        self.gout("pan", "1", "L100")
        self.gout("mix")
        left, right = samples(root / "master.wav", 2)
        self.assertGreater(peak_db(left), -24)  # the fixture sine sits at -18 dB
        self.assertEqual(peak_db(right), float("-inf"))

    def test_gain_lowers_loudness(self):
        root = self.project("song", "noise.wav")
        self.gout("mix")
        before = loudness(root / "master.wav")
        self.gout("gain", "1", "-6")
        self.gout("mix")
        self.assertAlmostEqual(loudness(root / "master.wav"), before - 6, delta=0.3)

    def test_loudness_target(self):
        root = self.project("song", "noise.wav")
        self.gout("set", "lufs", "-14")
        out = self.gout("mix").stdout
        self.assertIn("LUFS", out)
        self.assertAlmostEqual(loudness(root / "master.wav"), -14, delta=0.5)

    def test_master_format_and_tags(self):
        root = self.project("song", "tone.wav")
        self.gout("set", "bits", "16")
        self.gout("set", "title", "Night Bulb")
        self.gout("mix", "--mp3")
        info = stream_info(root / "master.wav")
        self.assertEqual(info["codec_name"], "pcm_s16le")
        self.assertEqual(info["tags"].get("title"), "Night Bulb")
        self.assertEqual(stream_info(root / "master.mp3")["tags"].get("title"), "Night Bulb")

    def test_fades_head_and_tail_change_the_length(self):
        root = self.project("song", "tone.wav")
        self.gout("set", "head", "500ms")
        self.gout("set", "tail", "1s")
        self.gout("set", "fadeout", "1s")
        self.gout("mix")
        self.assertAlmostEqual(duration(root / "master.wav"), 7.5, delta=0.01)
        (mono,) = samples(root / "master.wav", 1)
        self.assertEqual(peak_db(mono[:int(0.49 * 48000)]), float("-inf"))
