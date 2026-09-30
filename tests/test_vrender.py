"""Rendering video tracks: the time map, the interpolation modes, fades, loops."""
import json
import subprocess

from helpers import duration, ffmpeg, gout_attr, GoutTest


def grays(path, size: int = 1):
    """The mean grey level of every frame of a video (scaled to one pixel), in order."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf",
                          "scale=1:1:flags=area,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return list(out)


def stream(path, kind: str, *fields: str):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries",
                          "stream=" + ",".join(fields), "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return out.strip().split(",")


def ramp(path, seconds: int = 10, fps: int = 12):
    """Each frame one step lighter than the one before: which source frame is on screen can be read back."""
    ffmpeg("-f", "lavfi", "-i", f"nullsrc=s=64x36:r={fps}:d={seconds},geq=lum='16+N*1.8':cb=128:cr=128",
           "-pix_fmt", "yuv420p", str(path))


def flat(path, luma: int, seconds: int = 10, fps: int = 12):
    ffmpeg("-f", "lavfi", "-i", f"nullsrc=s=64x36:r={fps}:d={seconds},geq=lum={luma}:cb=128:cr=128",
           "-pix_fmt", "yuv420p", str(path))


def music(path, seconds: int = 30):
    ffmpeg("-f", "lavfi", "-i", f"sine=f=220:d={seconds},volume='if(lt(mod(t,10),5),0.2,1)':eval=frame", str(path))


class RenderTest(GoutTest):
    def setUp(self):
        super().setUp()
        self.song, self.ramp = self.tmp / "music.wav", self.tmp / "ramp.mp4"
        music(self.song)
        ramp(self.ramp)
        self.table = grays(self.ramp)  # source frame -> grey level as the render reads it back

    def synced(self):
        root = self.project("song")
        self.gout("add", str(self.song))
        self.gout("add", str(self.ramp))
        self.gout("sync", "2")
        return root

    def points(self, n: int = 1):
        return [t for t in self.dump()["tracks"] if t["kind"] == "video"][n - 1]["video"]["warp"]

    def expected(self, seconds: float, points) -> float:
        """The grey level the map says should be on screen at this project time (by the source frame)."""
        evaluate = gout_attr("timemap", "evaluate")
        frame = min(len(self.table) - 1, max(0, int(evaluate(points, seconds * 1000) / 1000 * 12)))
        return self.table[frame]

    def test_the_picture_follows_the_time_map(self):
        root = self.synced()
        points = self.points()
        out = self.tmp / "out.mp4"
        said = self.gout("video", "-m", "nearest", "-o", str(out)).stdout
        self.assertIn("rendering master.wav first", said)
        self.assertEqual(stream(out, "v", "codec_name", "width", "height"), ["h264", "64", "36"])
        self.assertEqual(stream(out, "a", "codec_name"), ["aac"])
        self.assertAlmostEqual(duration(out), 30.0, delta=0.15)
        levels = grays(out)
        self.assertEqual(len(levels), 750)
        for seconds in (1, 4, 9, 13, 18, 22, 27, 29.5):
            self.assertAlmostEqual(levels[int(seconds * 25)], self.expected(seconds, points), delta=6, msg=f"at {seconds} s")
        self.assertEqual((root / "master" / "ramp.mp4").read_bytes(), self.ramp.read_bytes())  # the original is untouched
        self.assertFalse(any(".part" in p.name for p in root.iterdir()))

    def test_interp_is_per_track_and_blend_fills_between(self):
        self.synced()
        self.assertIn("is not a video track", self.gout("interp", "1", "blend", ok=False).stderr)
        self.assertIn("nearest", self.gout("interp", "2").stdout)
        self.gout("interp", "2", "nearest")
        nearest = self.tmp / "nearest.mp4"
        self.gout("video", "-o", str(nearest))
        self.gout("interp", "2", "blend")
        self.assertEqual(self.dump()["tracks"][1]["video"]["mode"], "blend")
        blend = self.tmp / "blend.mp4"
        self.gout("video", "-o", str(blend))
        self.gout("undo")
        self.assertEqual(self.dump()["tracks"][1]["video"]["mode"], "nearest")
        first, second = grays(nearest), grays(blend)
        self.assertEqual(len(first), len(second))
        # slowed 3 times, the nearest frame shows each of the picture's frames for several output frames;
        # blended, the levels in between appear
        self.assertGreater(len(set(second)), 1.5 * len(set(first)))
        self.assertLessEqual(len(set(first)), 122)

    def test_flow_renders_too(self):
        self.synced()
        self.gout("interp", "all", "flow")
        out = self.tmp / "flow.mp4"
        said = self.gout("video", "-o", str(out)).stdout
        self.assertIn("flow is slow", said)
        levels = grays(out)
        self.assertEqual(len(levels), 750)
        points = self.points()
        for seconds in (4, 13, 22):
            self.assertAlmostEqual(levels[int(seconds * 25)], self.expected(seconds, points), delta=8, msg=f"at {seconds} s")

    def test_a_picture_not_synced_plays_as_it_is_where_it_lies(self):
        self.project("song")
        self.gout("add", str(self.song))
        self.gout("add", str(self.ramp), "-a", "5s")
        self.gout("trim", "2", "-st", "2s", "-et", "8s")  # shows 2 s to 8 s of the file, from 7 s on
        self.gout("fade", "all", "0")  # a cut in, a cut out
        out = self.tmp / "out.mp4"
        self.gout("video", "-m", "nearest", "-o", str(out))
        levels = grays(out)
        self.assertLess(max(levels[:170]), 5)  # black until 7 s
        self.assertAlmostEqual(levels[int(7.3 * 25)], self.table[int(2.3 * 12)], delta=6)
        self.assertAlmostEqual(levels[int(10 * 25)], self.table[int(5 * 12)], delta=6)
        self.assertLess(max(levels[int(13.2 * 25):int(14.8 * 25)]), 5)  # and black again after 13 s

    def test_the_head_delays_the_picture_with_the_sound(self):
        self.synced()
        self.gout("set", "head", "1s")
        out = self.tmp / "out.mp4"
        self.gout("video", "-m", "nearest", "-o", str(out))
        levels = grays(out)
        self.assertLess(max(levels[:20]), 5)  # the first second of padding
        points = self.points()
        self.assertAlmostEqual(levels[int(10 * 25)], self.expected(9, points), delta=6)

    def test_a_clip_above_another_dissolves_over_it_and_opacity_lets_the_one_below_through(self):
        dark, bright = self.tmp / "dark.mp4", self.tmp / "bright.mp4"
        flat(dark, 80)  # 10 s
        flat(bright, 200)
        self.project("song")
        self.gout("add", str(self.song))
        self.gout("add", str(dark))
        self.gout("add", str(bright), "-a", "5s")
        self.gout("fade", "all", "2s", "1s")
        self.assertIn("in 00:00:02.000  out 00:00:01.000", self.gout("fade", "3").stdout)
        out = self.tmp / "out.mp4"
        self.gout("video", "-o", str(out))
        levels = grays(out)
        low, high = levels[int(3 * 25)], levels[int(9 * 25)]
        self.assertGreater(high, low + 80)  # dark alone at 3 s (after its own 2 s fade-in), bright alone at 9 s
        mid = levels[int(6 * 25)]  # halfway through the bright one's fade-in
        self.assertGreater(mid, low + 25)
        self.assertLess(mid, high - 25)
        self.assertGreater(levels[int(9.8 * 25)], low + 50)  # dark does not fade out under the bright clip: no dip
        self.assertLess(levels[int(14.8 * 25)], high - 60)  # bright fades out over its last second, over black
        self.assertLess(levels[int(0.1 * 25)], 10)  # and dark fades in from black
        self.gout("opacity", "3", "50%")
        self.gout("fade", "3", "0")
        self.assertEqual(self.dump()["tracks"][2]["video"]["opacity"], 0.5)
        half = self.tmp / "half.mp4"
        self.gout("video", "-o", str(half))
        levels = grays(half)
        self.assertAlmostEqual(levels[int(12 * 25)], high / 2, delta=12)  # alone over black at half
        self.assertAlmostEqual(levels[int(7 * 25)], (low + high) / 2, delta=14)  # over the dark one: a mix

    def test_opacity_fade_and_interp_commands(self):
        flat(self.tmp / "dark.mp4", 80)
        self.project("song")
        self.gout("add", str(self.song))
        self.gout("add", str(self.tmp / "dark.mp4"))
        self.assertIn("is not a video track", self.gout("opacity", "1", "50%", ok=False).stderr)
        self.assertIn("between 0% and 100%", self.gout("opacity", "2", "-5", ok=False).stderr)
        self.assertIn("expected", self.gout("opacity", "2", "lots", ok=False).stderr)
        self.assertIn("bad time", self.gout("fade", "2", "-1s", ok=False).stderr)
        self.assertIn("one of", self.gout("interp", "2", "smooth", ok=False).stderr)
        self.gout("opacity", "2", "0.25")
        self.assertIn("25%", self.gout("opacity", "2").stdout)
        self.gout("opacity", "2", "100")
        self.assertEqual(self.dump()["tracks"][1]["video"]["opacity"], 1.0)
        self.gout("fade", "2", "1s")
        self.assertEqual(self.dump()["tracks"][1]["video"]["fade_in_ms"], 1000)
        self.assertEqual(self.dump()["tracks"][1]["video"]["fade_out_ms"], 1000)
        self.gout("undo")
        self.assertEqual(self.dump()["tracks"][1]["video"]["fade_in_ms"], 500)

    def test_a_short_video_is_not_rendered_and_the_choices_are_named(self):
        self.synced()
        self.gout("trim", "2", "-et", "4s")
        self.gout("sync", "2")
        said = self.gout("video", "-o", str(self.tmp / "out.mp4"), ok=False)
        self.assertIn("short by 14.0 s", said.stdout)
        self.assertIn("--duplicate", said.stdout)
        self.assertIn("until you choose", said.stderr)
        self.assertFalse((self.tmp / "out.mp4").exists())

    def test_a_bounce_turns_round_and_a_loop_jumps_back(self):
        self.synced()
        self.gout("trim", "2", "-et", "4s")  # 48 frames, light to lighter
        self.gout("sync", "2", "--pingpong")
        bounce = self.tmp / "bounce.mp4"
        self.gout("video", "-m", "nearest", "-o", str(bounce))
        levels = grays(bounce)
        self.assertEqual(len(levels), 750)
        steps = [b - a for a, b in zip(levels, levels[1:])]
        self.assertLess(max(abs(s) for s in steps), 8)  # no jump: it turns round
        self.assertGreater(max(levels), 80)
        self.assertLess(levels[-1], max(levels) - 20)  # and came back down at some point
        self.assertTrue(list((self.tmp / "song" / ".gout" / "video").glob("*.pingpong.mp4")))
        self.gout("sync", "2", "--loop")
        looped = self.tmp / "loop.mp4"
        self.gout("video", "-m", "nearest", "-o", str(looped))
        steps = [b - a for a, b in zip(grays(looped), grays(looped)[1:])]
        self.assertGreater(max(steps, key=abs) * -1, 60)  # the jump back to the start

    def test_a_preview_is_nearest_and_small_and_the_screens_keep_their_flags(self):
        self.synced()
        self.gout("interp", "all", "flow")
        said = self.gout("video", "--preview").stdout
        self.assertIn("master-preview.mp4", said)
        self.assertIn("nearest", said)
        self.assertNotIn("flow is slow", said)
        self.assertTrue((self.tmp / "song" / "master-preview.mp4").exists())
        self.assertIn("for video tracks", self.gout("video", str(self.ramp), "-m", "flow", ok=False).stderr)
        self.assertIn("not for video tracks", self.gout("video", "-T", ok=False).stderr)
        self.assertIn("one of", self.gout("video", "-m", "smooth", ok=False).stderr)
        self.assertIn("WIDTHxHEIGHT", self.gout("video", "-s", "big", ok=False).stderr)
        big = self.tmp / "big.mp4"
        self.gout("video", "-m", "nearest", "-s", "128x72", "-r", "10", "-o", str(big))
        self.assertEqual(stream(big, "v", "width", "height", "r_frame_rate"), ["128", "72", "10/1"])

    def test_video_with_no_video_track_says_how_to_use_it(self):
        self.project("song", "tone.wav")
        self.assertIn("usage", self.gout("video", ok=False).stderr)

    def test_the_expression_is_shallow_and_right(self):
        inverse = gout_attr("vrender", "inverse_expression")
        points = [(i * 2.0, i * 1.0) for i in range(200)]  # 200 points, two times slower
        text = inverse(points)
        self.assertEqual(text.count("if("), 198)  # one for each split between 199 straight stretches
        depth = deepest = 0
        for char in text:
            depth += (char == "(") - (char == ")")
            deepest = max(deepest, depth)
        self.assertLess(deepest, 30)  # nested about log2(n) deep, not n
        self.assertEqual(inverse([(0.0, 0.0), (10.0, 5.0)]), "0.000000+(T-0.000000)*2.000000000")
