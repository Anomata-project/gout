import os
import shutil
import subprocess
from unittest import mock

from helpers import duration, ffmpeg, gout_attr, GoutTest, REPO


def frame_at(path, seconds: float, width: int, height: int) -> bytes:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(path),
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, check=True).stdout[:width * height * 3]


def pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    i = 3 * (y * width + x)
    return tuple(frame[i:i + 3])


PROBE = """
from gout.screens import Screen


class Probe(Screen):
    name = "probe"

    def pick(self, ctx, word):
        self.word = word

    def frame(self, ctx, width, height):
        return [(f"{self.word} {ctx.choice_ms:g}", "")]


def register(gout):
    gout.add_screen(Probe())
"""


class VideoTest(GoutTest):
    def setUp(self):
        super().setUp()
        self.more_env["GOUT_VIDEO_GRID"] = "40x12"  # 480 by 288 pixels

    def test_a_cover_with_black_bars_and_the_song(self):
        root = self.project("song", "tone.wav")  # 6 s
        cover = self.tmp / "cover.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=200x100:d=1", "-frames:v", "1", str(cover))  # 2:1, wider than the frame
        out = self.gout("video", str(cover)).stdout
        self.assertIn("rendering master.wav first", out)
        video = root / "master.mp4"
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate",
                                  "-of", "csv=p=0", str(video)], capture_output=True, text=True, encoding="utf-8").stdout.split()
        self.assertEqual(sorted(streams), ["aac,0/0", "h264,480,288,25/1"])  # audio has no frame rate
        self.assertAlmostEqual(duration(video), 6.0, delta=0.1)
        frame = frame_at(video, 1.0, 480, 288)
        self.assertLess(max(pixel(frame, 480, 240, 5)), 20)  # the bar above: 480x240 fits, 24 rows each side
        red, green, blue = pixel(frame, 480, 240, 144)
        self.assertGreater(red, 200)
        self.assertLess(max(green, blue), 40)
        self.gout("vd", str(cover), "-o", str(self.tmp / "other.mp4"))
        self.assertTrue((self.tmp / "other.mp4").exists())
        self.assertFalse(any(p.name.endswith(".part.mp4") for p in root.iterdir()))
        self.assertIn("no such image", self.gout("video", "nothing.png", ok=False).stderr)

    def test_characters_are_drawn_in_the_theme_colours(self):
        Atlas, compose, colours = gout_attr("video", "Atlas"), gout_attr("video", "compose"), gout_attr("video", "class_colours")
        atlas = Atlas()
        self.assertTrue(any(any(row) for row in atlas.glyph("@", (255, 255, 255))))
        self.assertIs(atlas.glyph(" ", (255, 255, 255)), atlas.blank)
        question = atlas.glyph("?", (255, 255, 255))
        for letter in "äÖå–":  # a name with them must not come out as ?
            self.assertNotEqual(atlas.glyph(letter, (255, 255, 255)), question)
        self.assertNotEqual(Atlas(extra="ŁŻ").glyph("Ł", (255, 255, 255)), question)  # the title's own letters
        palette = colours(None)
        self.assertEqual(palette["1"], (0x87, 0xd7, 0x87))  # the second track colour
        frame = compose([("@", "1"), ("", "")], atlas, palette, 2, 2)
        self.assertEqual(len(frame), 2 * 12 * 2 * 24 * 3)
        lit = [frame[i:i + 3] for i in range(0, len(frame), 3) if any(frame[i:i + 3])]
        brightest = max(lit, key=sum)
        self.assertEqual(tuple(brightest), (0x87, 0xd7, 0x87))  # a fully covered pixel is the colour itself
        self.assertEqual(gout_attr("video", "xterm_rgb")(196), (255, 0, 0))

    def beat_project(self):
        """6 s: a 60 Hz thump every half second, louder at 2.9 s and 4.1 s."""
        beat = self.tmp / "beat.wav"
        ffmpeg("-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*60*t)*exp(-25*mod(t,0.5))*(1+3*(between(t,2.85,2.95)+between(t,4.05,4.15)))"
               "+0.02*sin(2*PI*440*t)':s=48000:d=6", "-c:a", "pcm_s16le", str(beat))
        return self.project("song", str(beat))

    def test_the_fractal_moves_with_the_song_and_changes_on_hits(self):
        shutil.copy(REPO / "examples" / "addons" / "fractal.py", self.addons / "fractal.py")
        self.more_env["GOUT_VIDEO_WORKERS"] = "2"
        root = self.beat_project()
        out = self.gout("video", "fractal", "3", "1", "-e", "3s").stdout
        self.assertIn("the fractal screen: 3 1, 1 changes", out)
        self.assertIn("2 processes drawing", out)
        video = root / "master.mp4"
        frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-count_frames", "-show_entries",
                                 "stream=nb_read_frames", "-of", "csv=p=0", str(video)], capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(frames.strip(), "150")
        early, later = frame_at(video, 1.0, 480, 288), frame_at(video, 1.2, 480, 288)
        self.assertNotEqual(early, later)  # it moves
        self.assertGreater(sum(early) / len(early), 2)  # and draws something
        self.assertIn("no such image or screen", self.gout("video", "nothing", ok=False).stderr)
        self.assertIn("fractal ((", self.gout("video", "fractal", "((", ok=False).stderr)  # checked before any work

    def test_a_screen_without_a_choice_shows_what_it_opens_with(self):
        shutil.copy(REPO / "examples" / "addons" / "fractal.py", self.addons / "fractal.py")
        self.more_env["GOUT_VIDEO_WORKERS"] = "2"
        song = self.tmp / "short.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=f=80:d=2", "-ar", "48000", str(song))
        root = self.project("song", str(song))
        (self.tmp / "config" / "gout").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "gout" / "fractal.json").write_text('{"preset": "rings", "zoom_preset": "four"}', encoding="utf-8")
        for screen in ("zoom", "fractal"):
            out = self.gout("video", screen, "-o", str(root / f"{screen}.mp4")).stdout  # no preset named: the remembered one
            self.assertIn(f"the {screen} screen", out)
            self.assertAlmostEqual(duration(root / f"{screen}.mp4"), 2.0, delta=0.1)

    def test_cuts_go_to_the_nearest_hit_and_choices_take_turns(self):
        cut_points, schedule = gout_attr("video", "cut_points"), gout_attr("video", "schedule")
        onset = [0.0] * 1200  # 30 s of 25 ms steps
        onset[10000 // 25 + 20] = 0.9  # a hit half a second after the 10 s mark
        onset[20000 // 25 - 60] = 0.2  # too weak to count, 1.5 s before 20 s
        self.assertEqual(cut_points(onset, 10000, 30000, 25), [10500, 20000])
        tasks = schedule(10, 1, 500, [2000, 5000, 8000], ["a", "b", "c"])  # 1 fps, half a second of head
        self.assertEqual([word for _, word, _ in tasks], ["a", "a", "a", "b", "b", "b", "c", "c", "c", "a"])
        self.assertEqual(tasks[0][2], -500)

    def test_the_fractal_draws_the_same_frame_in_any_order(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fractal_example", REPO / "examples" / "addons" / "fractal.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        project = gout_attr("project", "Project")
        root = self.beat_project()
        ctx_class = gout_attr("screens", "ScreenContext")

        def draw(order, preset, kind=module.Fractal):
            screen, ctx = kind(), ctx_class(project(root))
            ctx.offline, ctx.choice_ms = True, 1000
            screen.pick(ctx, preset)
            frames = {}
            for ms in order:
                ctx.position_ms = ms
                frames[ms] = screen.frame(ctx, 60, 20)
            return frames

        for preset in ("classic", "waves"):
            forwards, backwards = draw([0, 3000, 6000], preset), draw([6000, 3000, 0], preset)
            self.assertEqual(forwards, backwards)
            self.assertEqual(draw([0, 3000, 6000], preset, module.Zoom), draw([6000, 3000, 0], preset, module.Zoom))
        self.assertEqual(module.Fractal().choices()[:2], ["seven", "classic"])

    def test_every_worker_knows_when_its_choice_began(self):
        (self.addons / "probe.py").write_text(PROBE, encoding="utf-8")
        root = self.beat_project()
        ScreenFrames, schedule, Project = gout_attr("video", "ScreenFrames"), gout_attr("video", "schedule"), gout_attr("project", "Project")
        tasks = schedule(10, 1, 500, [2000, 5000], ["a", "b"])[2:]  # 1 fps, half a second of head, a cover over two frames
        with mock.patch.dict(os.environ, self.env()):
            frames = ScreenFrames(Project(root), "probe", tasks, 20, 1, 6000, 3, first=2, cuts=[2000, 5000])
            drawn = [frames.rows(frame)[0][0] for frame, _, _ in tasks]
            frames.stop()
        self.assertEqual(drawn, ["a 1500", "b 2000", "b 2000", "b 2000", "a 5000", "a 5000", "a 5000", "a 5000"])

    def test_addons_examples_update_keeps_the_old_one(self):
        (self.addons / "fractal.py").write_text("# an older copy\n", encoding="utf-8")
        (self.addons / "tremolo.py").write_bytes((REPO / "examples" / "addons" / "tremolo.py").read_bytes())
        out = self.gout("addons", "examples").stdout
        self.assertIn("--update replaces it", out)
        out = self.gout("addons", "examples", "--update").stdout
        self.assertIn("fractal.py               updated", out)
        self.assertEqual((self.addons / "fractal.py.bak").read_text(encoding="utf-8"), "# an older copy\n")
        self.assertFalse((self.addons / "tremolo.py.bak").exists())  # the same file stays as it is

    def test_the_title_over_the_cover_then_the_fractal(self):
        shutil.copy(REPO / "examples" / "addons" / "fractal.py", self.addons / "fractal.py")
        self.more_env["GOUT_VIDEO_WORKERS"] = "2"
        root = self.beat_project()
        cover = self.tmp / "cover.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=480x288:d=1", "-frames:v", "1", str(cover))
        out = self.gout("video", "fractal", "classic", "-c", str(cover)).stdout
        self.assertIn("no title: gout set title", out)
        self.assertIn("after cover.png", out)
        video = root / "master.mp4"
        self.assertGreater(pixel(frame_at(video, 1.0, 480, 288), 480, 240, 144)[0], 200)  # the cover first
        self.assertLess(pixel(frame_at(video, 5.5, 480, 288), 480, 240, 144)[0], 200)  # then the fractal
        self.gout("set", "title", "Hey – a subtitle")
        self.gout("set", "artist", "Somebody")
        out = self.gout("video", str(cover)).stdout
        self.assertNotIn("no title", out)
        with_title, after = frame_at(video, 2.0, 480, 288), frame_at(video, 4.0, 480, 288)  # a 6 s song: gone at 3 s
        middle = [pixel(with_title, 480, x, y) for x in range(100, 380, 4) for y in range(110, 180, 4)]
        self.assertTrue(any(max(p) < 30 for p in middle))  # the black box
        self.assertTrue(any(p[0] > 150 and p[1] > 100 and p[2] < 120 for p in middle))  # gold letters on it
        self.assertEqual(pixel(after, 480, 240, 144)[1] < 40, True)  # gone again: the red cover
        self.gout("video", str(cover), "-T")  # no title

    def test_the_title_is_big_with_what_follows_the_dash_under_it(self):
        split, Title = gout_attr("video", "split_title"), gout_attr("video", "Title")
        self.assertEqual(split("Northern Road – slow dance"), ("Northern Road", "slow dance"))
        self.assertEqual(split("Plain"), ("Plain", ""))
        title = Title("Hää – slow", "Me", 80, 24)
        left, top, right, bottom = title.box
        rows = [("-" * 80, "1" * 80)] * 24
        self.assertIs(title.over(rows, 0.2), rows)  # not yet
        full = title.over(rows, 3.0)
        big = "".join(text for text, _ in full[top:bottom + 1])
        self.assertTrue(any(ch in big for ch in "#%@"))  # drawn in the fractal's characters
        self.assertIn("slow", big)
        self.assertIn("Me", big)
        self.assertEqual(full[0], rows[0])  # rows outside the box stay
        wiping = title.over(rows, 0.8)  # half way in: the right part of the box not yet
        self.assertEqual(wiping[top][0][right], "-")
        self.assertIs(title.over(rows, 6.6), rows)  # and gone

    def test_progress_shows_a_bar_and_the_time_left(self):
        bar, fmt_clock, Progress = gout_attr("core", "bar"), gout_attr("core", "fmt_clock"), gout_attr("video", "Progress")
        self.assertEqual(bar(0.5, 4), "▕██  ▏")
        self.assertEqual(bar(1 / 16, 2), "▕▏ ▏")  # an eighth of the two characters
        self.assertEqual((fmt_clock(452), fmt_clock(3723)), ("7:32", "1:02:03"))
        progress = Progress(100)
        self.assertIn("working out the time", progress.text(1, 100.0))  # the first frame: startup, not speed
        line = progress.text(51, 110.0, clock=0.0)  # 50 frames in 10 s: 49 more take 9.8 s
        self.assertIn(" 51%  0:10 left, ready at ", line)
        self.assertTrue(line.startswith("▕██████████████"))

    def test_the_ui_shows_the_progress_and_esc_stops_the_video(self):
        from test_ui import FakeScreen
        import os
        root = self.project("song", "tone.wav")
        cover = self.tmp / "cover.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1", str(cover))
        for key, value in {**self.more_env, "GOUT_ADDONS": str(self.addons), "GOUT_PLAYER": "null"}.items():
            os.environ[key] = value
        try:
            screen = FakeScreen(30, 120)
            ui = gout_attr("tui", "Tui")(gout_attr("project", "Project")(root), screen)
            seen = []
            show = ui.show_progress

            def watch(what, fraction, status):
                screen.queue.append("\x1b")  # esc, pressed while it renders
                stop = show(what, fraction, status)
                seen.append(next(screen.row(y) for y in range(30) if "video ▕" in screen.row(y)))
                return stop

            ui.show_progress = watch
            ui.input = f"video {cover}"
            ui.submit()
        finally:
            os.environ.pop("GOUT_VIDEO_GRID", None)
        self.assertIn("esc stops", seen[0])
        fit = gout_attr("tui", "progress_text")
        for room in (120, 60, 40, 24, 12):
            text = fit("video", 0.42, "7:32 left, ready at 19:08", room)
            self.assertLessEqual(len(text), room - 1 if room > 12 else room)
            self.assertIn("42%", text)
        self.assertIn("ready at 19:08  esc stops", fit("video", 0.42, "7:32 left, ready at 19:08", 120))
        self.assertIn("7:32 left  esc", fit("video", 0.42, "7:32 left, ready at 19:08", 40))
        self.assertTrue(any("video stopped" in line for line in ui.log))
        self.assertFalse(any(p.suffix == ".mp4" for p in root.iterdir()))  # neither the video nor a part file
        self.assertFalse(ui.busy)
