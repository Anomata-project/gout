"""Fitting a video to the music: the time map, sync and warp."""
import json
import math

from helpers import ffmpeg, gout_attr, GoutTest

CELLS = 4000  # 100 s of music in 25 ms cells


def pulses(n: int, every: int, height: float = 6.0) -> list[float]:
    return [1.0 + (height if i % every == 0 else 0.0) for i in range(n)]


def slopes(src):
    return [b - a for a, b in zip(src, src[1:])]


class TimeMapTest(GoutTest):
    """The pure arithmetic of timemap.py on made-up curves."""

    def setUp(self):
        super().setUp()
        self.fit = gout_attr("timemap", "fit")

    def check_map(self, result, slow=4.0, fast=1.25, tolerance=1e-6):
        steps = slopes(result.src)
        self.assertAlmostEqual(result.src[0], 0.0)
        self.assertAlmostEqual(result.src[-1], result.used, places=5)  # covers the whole stretch
        self.assertEqual(len(result.src), result.covered + 1)
        self.assertTrue(all(s > 0 for s in steps), "monotonic, and never standing still")
        self.assertGreaterEqual(min(steps), 1 / slow - tolerance)  # never slower than allowed
        self.assertLessEqual(max(steps), fast + tolerance)

    def test_a_map_is_monotonic_covers_the_range_and_respects_the_slowdown(self):
        motion = [1.0 + 4 * math.sin(i / 40) ** 2 for i in range(1200)]
        level = [0.3 if (i // 400) % 2 == 0 else 0.9 for i in range(CELLS)]
        onset = [1.0 if i % 80 == 0 and (i // 400) % 2 else 0.0 for i in range(CELLS)]
        for depth in (0.0, 0.6, 1.0):
            for slow in (3.5, 4.0, 8.0):
                result = self.fit(motion, level, onset, 1200, slow=slow, fast=1.25, depth=depth)
                self.assertEqual(result.gap, 0)
                self.check_map(result, slow=slow)
        even = self.fit(motion, level, [0.0] * CELLS, 1200, slow=4.0, fast=1.25, depth=0.0)  # no hits to bend to
        self.assertTrue(all(abs(s - 0.3) < 1e-6 for s in slopes(even.src)))  # depth 0: an even stretch

    def test_a_tight_limit_leaves_only_the_even_stretch(self):
        motion = pulses(1000, 13)
        level = [(i % 200) / 200 for i in range(CELLS)]
        result = self.fit(motion, level, [0.0] * CELLS, 1000, slow=4.0, fast=1.25, depth=1.0)  # exactly 4x: no room
        self.assertEqual(result.gap, 0)
        self.assertTrue(all(abs(s - 0.25) < 1e-6 for s in slopes(result.src)))

    def test_flat_motion_gives_the_music_level_map(self):
        level = [0.2] * 2000 + [1.0] * 2000  # the second half is louder
        result = self.fit([1.0] * 1500, level, [0.0] * CELLS, 1500, slow=4.0, fast=1.25, depth=1.0)
        self.check_map(result)
        first, second = slopes(result.src)[:2000], slopes(result.src)[2000:]
        self.assertLess(sum(first) / len(first), sum(second) / len(second))  # the picture runs faster under loud music

    def test_the_music_shapes_the_map_and_hits_are_met(self):
        motion = pulses(1200, 13)  # a fast moment every 13 cells of the picture
        level = [0.6] * CELLS
        onset = [1.0 if i % 47 == 0 and i else 0.0 for i in range(CELLS)]
        result = self.fit(motion, level, onset, 1200, slow=4.0, fast=1.25, depth=0.3)
        self.check_map(result)
        self.assertGreater(result.hits, 50)
        self.assertGreater(result.hits_after, result.hits_before)  # bent to meet the hits
        self.assertGreater(result.anchored, 0)
        bent = self.fit(motion, level, [0.0] * CELLS, 1200, slow=4.0, fast=1.25, depth=0.3)
        self.assertEqual(bent.anchored, 0)  # no hits, nothing to bend to

    def test_louder_music_against_busier_picture_raises_the_match(self):
        motion = [1.0 + 4 * math.sin(i / 40) ** 2 for i in range(1200)]
        level = [0.3 if (i // 400) % 2 == 0 else 0.9 for i in range(CELLS)]
        result = self.fit(motion, level, [0.0] * CELLS, 1200, slow=4.0, fast=1.25, depth=0.6)
        self.assertGreater(result.match_after, result.match_before + 0.1)

    def test_bar_lines_are_anchors_when_there_is_no_hit(self):
        motion = pulses(1200, 13)
        bars = list(range(80, CELLS, 160))
        result = self.fit(motion, [0.6] * CELLS, [0.0] * CELLS, 1200, slow=4.0, fast=1.25, depth=0.3, bars=bars)
        self.check_map(result)
        self.assertGreater(result.anchored, 0)

    def test_too_short_reports_the_gap_and_chooses_nothing(self):
        result = self.fit(pulses(300, 13), [0.5] * CELLS, [0.0] * CELLS, 300, slow=4.0, fast=1.25, depth=0.6)
        self.assertEqual(result.covered, 1200)  # 300 cells at 4x
        self.assertEqual(result.gap, CELLS - 1200)
        self.assertIn("short", result.notes)
        self.assertTrue(all(abs(s - 0.25) < 1e-9 for s in slopes(result.src)))

    def test_a_tail_fills_the_gap_with_room_to_move(self):
        for tail in ("loop", "pingpong"):
            result = self.fit(pulses(300, 13), [0.3 if (i // 400) % 2 == 0 else 0.9 for i in range(CELLS)], [0.0] * CELLS,
                              300, slow=4.0, fast=1.25, depth=0.6, tail=tail)
            self.assertEqual(result.gap, 0, tail)
            self.check_map(result)
            self.assertGreater(result.used, 300)
            self.assertGreater(max(slopes(result.src)) - min(slopes(result.src)), 0.01)  # not one flat pace

    def test_fold_is_the_loop_and_the_bounce(self):
        fold = gout_attr("timemap", "fold")
        self.assertEqual([fold(x, 10, "loop") for x in (0, 5, 10, 12, 25)], [0, 5, 10, 2, 5])
        self.assertEqual([fold(x, 10, "pingpong") for x in (0, 5, 10, 12, 20, 25)], [0, 5, 10, 8, 0, 5])
        self.assertEqual(fold(12, 10, ""), 12)

    def test_a_long_picture_uses_its_start(self):
        result = self.fit(pulses(3000, 13), [0.5] * 1000, [0.0] * 1000, 3000, slow=4.0, fast=1.25, depth=0.6)
        self.assertEqual(result.used, 1250)  # 1000 cells at the most it may be sped up
        self.assertIn("long", result.notes)
        self.check_map(result)

    def test_points_are_few_close_and_keep_the_ends(self):
        simplify, evaluate = gout_attr("timemap", "simplify"), gout_attr("timemap", "evaluate")
        result = self.fit(pulses(1200, 13), [0.3 if (i // 400) % 2 == 0 else 0.9 for i in range(CELLS)], [0.0] * CELLS,
                          1200, slow=4.0, fast=1.25, depth=0.6)
        points = simplify(result.src, 1000)
        self.assertLess(len(points), 80)
        self.assertEqual(points[0], [0, 1000])
        self.assertEqual(points[-1], [CELLS * 25, 1000 + 1200 * 25])
        self.assertTrue(all(b[0] - a[0] >= 250 and b[1] > a[1] for a, b in zip(points, points[1:])))
        for i in range(0, CELLS, 37):  # the points follow the map within about a frame
            self.assertAlmostEqual(evaluate(points, i * 25), 1000 + result.src[i] * 25, delta=45)


def moving_video(path, seconds: int = 10, fps: int = 12):
    """Picture that speeds up: the stripes' phase goes with t squared."""
    ffmpeg("-f", "lavfi", "-i", f"nullsrc=s=96x54:r={fps}:d={seconds},geq=lum='128+100*sin(X/6+T*T*3)':cb=128:cr=128",
           "-pix_fmt", "yuv420p", str(path))


def music(path, seconds: int = 30):
    ffmpeg("-f", "lavfi", "-i", f"sine=f=220:d={seconds},volume='if(lt(mod(t,10),5),0.2,1)':eval=frame", str(path))


class SyncTest(GoutTest):
    def setUp(self):
        super().setUp()
        self.picture, self.song = self.tmp / "acc.mp4", self.tmp / "music.wav"
        moving_video(self.picture)
        music(self.song)

    def setup_project(self):
        root = self.project("song")
        self.gout("add", str(self.song))
        self.gout("add", str(self.picture))
        return root

    def video(self):
        return next(t for t in self.dump()["tracks"] if t["kind"] == "video")

    def check_points(self, video, slow=4.0, fast=1.25):
        points = video["video"]["warp"]
        self.assertEqual(points[0][0], 0)
        for a, b in zip(points, points[1:]):
            self.assertGreater(b[0], a[0])  # forward in the project
            self.assertGreater(b[1], a[1])  # and in the picture
            ratio = (b[0] - a[0]) / (b[1] - a[1])  # how many times slower
            self.assertLessEqual(ratio, slow * 1.01)
            self.assertGreaterEqual(ratio, 0.99 / fast)

    def test_sync_fits_the_picture_to_the_music(self):
        self.setup_project()
        out = self.gout("sync", "2").stdout
        self.assertIn("30.0 s of music", out)
        self.assertIn("warp points", out)
        self.assertIn("music match", out)
        video = self.video()
        self.assertEqual(video["offset_ms"], 0)
        points = video["video"]["warp"]
        self.assertEqual(points[-1], [30000, 10000])  # the whole picture over the whole song
        self.assertEqual(video["video"]["want_ms"], [0, 30000])
        self.check_points(video)
        self.assertIn("synced", self.gout("ls").stdout)
        self.assertNotIn("SHORT", self.gout("ls").stdout)

    def test_sync_is_undone_in_one_step_and_is_repeatable(self):
        self.setup_project()
        self.gout("sync", "2")
        first = self.video()["video"]["warp"]
        self.gout("sync", "2", "--depth", "0.2")
        self.assertNotEqual(self.video()["video"]["warp"], first)
        self.assertEqual(self.video()["video"]["depth"], 0.2)
        self.gout("undo")
        self.assertEqual(self.video()["video"]["warp"], first)
        self.gout("undo")
        self.assertEqual(self.video()["video"]["warp"], [])
        self.gout("sync", "2")
        self.assertEqual(self.video()["video"]["warp"], first)  # the same every time

    def test_the_limits_and_the_stretch_can_be_set(self):
        self.setup_project()
        self.gout("sync", "2", "--slow", "3.5", "--fast", "1", "--depth", "1", "-st", "2s", "-et", "22s")
        video = self.video()
        self.assertEqual(video["offset_ms"], 2000)
        self.assertEqual(video["video"]["warp"][-1][0], 20000)
        self.check_points(video, slow=3.5, fast=1.0)
        self.assertIn("is not a video track", self.gout("sync", "1", ok=False).stderr)
        self.assertIn("between", self.gout("sync", "2", "--depth", "2", ok=False).stderr)
        self.assertIn("--fast must be more", self.gout("sync", "2", "--slow", "2", "--fast", "0.4", ok=False).stderr)

    def test_a_video_that_is_too_short_reports_the_gap_and_keeps_nothing_made_up(self):
        self.setup_project()
        self.gout("trim", "2", "-et", "4s")  # 4 s of picture at 4x: 16 s of the 30
        out = self.gout("sync", "2").stdout
        self.assertIn("SHORT BY 14.0 s", out)
        self.assertIn("--pingpong", out)
        video = self.video()
        self.assertEqual(video["video"]["warp"][-1][0], 16000)
        self.assertEqual(video["video"]["tail"], "")  # it chose nothing
        self.assertIn("SHORT BY 14.0 s", self.gout("ls").stdout)
        self.assertIn("SHORT BY", self.gout("warp", "2").stdout)
        for flag in ("--loop", "--pingpong"):
            self.gout("sync", "2", flag)
            video = self.video()
            self.assertEqual(video["video"]["tail"], flag[2:])
            self.assertEqual(video["video"]["warp"][-1][0], 30000)  # covers the music now
            self.assertGreater(video["video"]["warp"][-1][1], 4000)  # past the end of the trimmed picture
            self.assertNotIn("SHORT", self.gout("ls").stdout)
        self.gout("sync", "2", "--once")
        self.assertEqual(self.video()["video"]["tail"], "")

    def test_a_long_picture_says_which_part_is_used(self):
        self.setup_project()
        self.gout("sync", "2", "-st", "0", "-et", "4s")  # 4 s of music: the picture can only be sped up 1.25x
        out = self.gout("sync", "2", "-st", "0", "-et", "4s").stdout
        self.assertIn("is longer than the stretch needs", out)
        self.assertEqual(self.video()["video"]["warp"][-1], [4000, 5000])

    def test_warp_lists_and_edits_points_by_hand(self):
        self.setup_project()
        self.gout("sync", "2")
        points = self.video()["video"]["warp"]
        listing = self.gout("warp", "2").stdout
        self.assertIn(f"{len(points)} points", listing)
        self.assertIn("slower", listing)
        shown = round(gout_attr("timemap", "evaluate")(points, 10000)) + 100  # a moment just after where the map is
        self.gout("warp", "2", "add", "10s", f"{shown}ms")
        added = self.video()["video"]["warp"]
        self.assertEqual(len(added), len(points) + 1)
        self.assertIn([10000, shown], added)
        index = added.index([10000, shown]) + 1
        self.gout("warp", "2", "mv", str(index), "+50ms")
        self.assertIn([10050, shown], self.video()["video"]["warp"])
        self.gout("warp", "2", "src", str(index), "-20ms")
        self.assertIn([10050, shown - 20], self.video()["video"]["warp"])
        self.gout("warp", "2", "rm", str(index))
        self.assertEqual(self.video()["video"]["warp"], points)
        self.assertIn("forward in project time", self.gout("warp", "2", "mv", "2", "29s", ok=False).stderr)
        self.assertIn("forward in the picture", self.gout("warp", "2", "src", "2", "9.9s", ok=False).stderr)
        self.assertIn("no point 99", self.gout("warp", "2", "rm", "99", ok=False).stderr)
        out = self.gout("warp", "2", "src", "2", "1s").stdout  # faster than allowed: the user's edit
        self.assertIn("your edit", out)
        self.gout("undo")
        self.gout("warp", "2", "reset")
        self.assertEqual(self.video()["video"]["warp"], points)
        self.gout("warp", "2", "clear")
        self.assertEqual(self.video()["video"]["warp"], [])
        self.assertIn("no map", self.gout("warp", "2").stdout)

    def test_a_point_cannot_show_more_picture_than_there_is(self):
        self.setup_project()
        self.gout("sync", "2")
        last = len(self.video()["video"]["warp"])
        self.assertIn("unless it loops", self.gout("warp", "2", "src", str(last), "11s", ok=False).stderr)

    def test_the_map_moves_with_the_track_and_survives_dump_import_and_rebuild(self):
        self.setup_project()
        self.gout("sync", "2")
        self.gout("move", "2", "+2s")
        video = self.video()
        self.assertEqual(video["offset_ms"], 2000)
        self.assertEqual(video["video"]["warp"][0][0], 0)  # points are after the track's position
        self.assertIn("00:00:02.000", next(l for l in self.gout("ls").stdout.splitlines() if "acc" in l))
        before = self.dump()
        self.gout("rebuild", "-f")
        self.assertEqual(self.dump()["tracks"], before["tracks"])
        path = self.tmp / "doc.json"
        path.write_text(json.dumps(before), encoding="utf-8")
        self.gout("warp", "2", "clear")
        self.gout("import", str(path))
        self.assertEqual(self.video()["video"]["warp"], before["tracks"][1]["video"]["warp"])

    def test_the_bar_lines_of_a_tempo_are_used(self):
        self.setup_project()
        self.gout("set", "bpm", "120")  # a bar is 2 s
        out = self.gout("sync", "2").stdout
        self.assertIn("15 bar lines", out)
        times = [p[0] for p in self.video()["video"]["warp"]]
        self.assertTrue(all(t in times for t in range(2000, 30000, 2000)))  # a point on every bar line

    def test_the_timeline_marks_what_is_short(self):
        self.setup_project()
        self.gout("trim", "2", "-et", "4s")
        self.gout("sync", "2")
        bar = next(line for line in self.gout("view", "-w", "100").stdout.splitlines() if "acc" in line)
        self.assertIn("█", bar)
        self.assertIn("╌", bar)
        self.assertLess(bar.index("█"), bar.index("╌"))

    def test_nothing_to_fit_to_without_sound(self):
        self.project("song")
        self.gout("add", str(self.picture))
        self.assertIn("add a sound first", self.gout("sync", "1", ok=False).stderr)
