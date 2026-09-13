from helpers import GoutTest, duration, stream_info


class CutTest(GoutTest):
    def test_legacy_form_cuts_a_wav_exactly(self):
        out = self.tmp / "out.wav"
        self.gout(str(self.fx / "click.wav"), "-st", "1s", "-el", "2s", "-o", str(out))
        self.assertAlmostEqual(duration(out), 2.0, delta=0.002)
        self.assertEqual(stream_info(out)["codec_name"], "pcm_s16le")

    def test_cut_command_by_absolute_end(self):
        out = self.tmp / "out.mp3"
        self.gout("cut", str(self.fx / "tone.mp3"), "-st", "00:00:01", "-et", "00:00:04", "-o", str(out))
        self.assertAlmostEqual(duration(out), 3.0, delta=0.06)

    def test_file_size_target_is_never_exceeded(self):
        out = self.tmp / "small.mp3"
        self.gout("cut", str(self.fx / "tone.mp3"), "-fs", "40kB", "-o", str(out))
        size = out.stat().st_size
        self.assertLessEqual(size, 40_000)
        self.assertGreater(size, 30_000)

    def test_dry_run_writes_nothing(self):
        out = self.tmp / "nothing.mp3"
        self.gout("cut", str(self.fx / "tone.mp3"), "-st", "1s", "-n", "-o", str(out))
        self.assertFalse(out.exists())

    def test_refuses_to_overwrite_without_force(self):
        out = self.tmp / "twice.wav"
        self.gout("cut", str(self.fx / "click.wav"), "-el", "1s", "-o", str(out))
        self.gout("cut", str(self.fx / "click.wav"), "-el", "1s", "-o", str(out), ok=False)
        self.gout("cut", str(self.fx / "click.wav"), "-el", "1s", "-o", str(out), "-f")
