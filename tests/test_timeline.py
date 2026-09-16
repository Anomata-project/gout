import json
import struct
import wave

from helpers import GoutTest, gout_attr
from test_ui import FakeScreen

BRAILLE = range(0x2800, 0x2900)


def write_positive_pulses(path, seconds=4.0, rate=44100):
    """Short positive-only bumps every half second: audio that only goes up from zero."""
    import math
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            phase = (i % (rate // 2)) / rate
            v = int(28000 * math.sin(math.pi * phase / 0.05)) if phase < 0.05 else 0
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


class TimelineTest(GoutTest):
    def rows(self, root, width=120, **kw):
        project = gout_attr("project", "Project")(root)
        return gout_attr("render", "render_timeline")(project, width, **kw)

    def test_master_comes_first_then_tracks_with_gaps_and_braille_waves(self):
        root = self.project("song", "tone.wav", "click.wav")
        self.gout("mix")
        rows = self.rows(root)
        labels = [label.strip() for label, cells, kind, classes, role in rows if kind == "wave" and label.strip()]
        self.assertEqual(labels[0], "master")
        self.assertTrue(labels[2].startswith("1 tone"))
        self.assertTrue(any(l.startswith("2 click") for l in labels))
        kinds = [kind for _, _, kind, _, _ in rows]
        self.assertEqual(kinds[:2], ["axis", "ruler"])
        self.assertEqual(kinds.count("gap"), 2)  # after the master, between the two tracks
        waves = "".join(cells for _, cells, kind, _, _ in rows if kind == "wave")
        self.assertTrue(any(ord(ch) in BRAILLE for ch in waves))
        self.assertEqual({role for _, _, kind, _, role in rows if kind == "wave" and _.strip()} - {""},
                         {"master_label", "track_label", "ruler_labels"})

    def test_the_wave_is_where_the_sound_is(self):
        root = self.project("song", "click.wav")  # silence with one click at 2.000 s of 4
        rows = self.rows(root, width=16 + 100)
        track = [(cells, classes) for label, cells, kind, classes, _ in rows if kind == "wave"]
        loud = sorted({i for cells, classes in track for i, cl in enumerate(classes) if cl == "0"})
        self.assertTrue(loud)
        self.assertTrue(all(45 <= i <= 55 for i in loud), loud)  # 2 s of 4 across 100 columns
        quiet = {cl for cells, classes in track for cl in classes if cl not in " 0"}
        self.assertEqual(quiet, {"c"})  # the rest is the silent centre line

    def test_positive_only_audio_draws_above_the_line(self):
        root = self.project("song")
        write_positive_pulses(self.tmp / "up.wav")
        self.gout("add", str(self.tmp / "up.wav"))
        rows = self.rows(root)
        upper, lower = [cells for label, cells, kind, _, _ in rows if kind == "wave"][-2:]
        full_upper = sum(1 for ch in upper if ord(ch) in BRAILLE and bin(ord(ch) - 0x2800).count("1") >= 6)
        self.assertGreater(full_upper, 3)
        self.assertTrue(all(ch in " ⠁⠈⠉" for ch in lower), lower)  # below the line only the line itself

    def test_layout_shrinks_to_fit(self):
        fit = gout_attr("render", "fit_layout")
        defaults = dict(gout_attr("theme", "DEFAULTS"))
        self.assertEqual(fit(defaults, 3, None), (2, 2, 1, 3))
        self.assertEqual(fit(defaults, 3, 13), (2, 2, 1, 3))        # 2 + 2 + 1 + 3*2 + 2 = 13
        self.assertEqual(fit(defaults, 3, 12), (2, 1, 1, 3))        # tracks to one row
        self.assertEqual(fit(defaults, 3, 9), (1, 1, 1, 3))         # then the master
        self.assertEqual(fit(defaults, 3, 6), (1, 1, 0, 3))         # then no gaps
        self.assertEqual(fit(defaults, 10, 8), (1, 1, 0, 4))        # then fewer tracks, one row for the note
        root = self.project("song", "tone.wav", "click.wav", "bass.wav")  # master not rendered: one row
        self.assertEqual(len(self.rows(root, max_rows=6)), 6)  # 2 + 1 + three one-row tracks, no gaps
        rows = self.rows(root, max_rows=5)
        self.assertEqual(len(rows), 5)
        self.assertIn("+2 more tracks", rows[-1][1])

    def test_color_json_layout_scale_and_problems(self):
        root = self.project("song", "tone.wav")
        (root / "color.json").write_text(json.dumps({
            "track_height": 3, "gap_rows": 0, "wave_style": "blocks", "wave_scale": "db",
            "master_wave": "#zz", "bogus": 1, "_comment": "ignored"}), encoding="utf-8")
        theme, problems, path = gout_attr("theme", "load_theme")(root)
        self.assertEqual(path, root / "color.json")
        self.assertEqual((theme["track_height"], theme["gap_rows"], theme["wave_style"]), (3, 0, "blocks"))
        self.assertEqual(len(problems), 2)
        rows = self.rows(root)
        waves = [cells for _, cells, kind, _, _ in rows if kind == "wave"]
        self.assertEqual(len(waves), 3)  # no master yet (not rendered), one track three rows tall
        self.assertTrue(all(ord(ch) not in BRAILLE for ch in "".join(waves)))
        listing = self.gout("colors").stdout
        self.assertIn("problem: master_wave", listing)
        self.assertIn("*wave_scale", listing)

    def test_db_scale_makes_quiet_material_taller(self):
        root = self.project("song", "tone.wav")  # a sine at -18 dBFS
        def dots(scale):
            (root / "color.json").write_text(json.dumps({"wave_scale": scale}), encoding="utf-8")
            theme = gout_attr("theme", "load_theme")(root)[0]
            rows = self.rows(root, theme=theme)
            return sum(bin(ord(ch) - 0x2800).count("1") for _, cells, kind, _, _ in rows if kind == "wave"
                       for ch in cells if ord(ch) in BRAILLE)
        self.assertGreater(dots("db"), dots("linear") * 2)

    def test_colors_init_writes_every_setting_with_help(self):
        self.gout("colors", "--init")
        path = self.tmp / "config" / "gout" / "color.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        settings = [k for k in data if not k.startswith("_")]
        self.assertEqual(len(settings), 25)
        self.assertEqual(set(settings), set(data["_help"]))
        self.gout("colors", "--init", ok=False)  # will not overwrite without -f
        self.assertIn(str(path), self.gout("colors").stdout)

    def test_palette_maps_colours_and_falls_back_without_them(self):
        theme_mod = __import__("gout.theme", fromlist=["x"])
        self.assertEqual(theme_mod.nearest_256((255, 0, 0)), 196)
        self.assertEqual(theme_mod.nearest_256((128, 128, 128)), 244)
        self.assertEqual(theme_mod.nearest_basic((250, 10, 10), 8), 1)
        import curses
        palette = theme_mod.Palette(dict(theme_mod.DEFAULTS))
        self.assertEqual(palette.attr("header"), curses.A_REVERSE)  # no colours started
        self.assertEqual(palette.attr("muted_wave"), curses.A_DIM)

    def test_ui_draws_master_first_with_a_playhead(self):
        root = self.project("song", "tone.wav", "click.wav")
        self.gout("mix")
        project = gout_attr("project", "Project")(root)
        screen = FakeScreen(40, 140)
        ui = gout_attr("tui", "Tui")(project, screen)
        ui.playhead_ms = 3000
        ui.draw()
        right = ui.layout()[3]
        text = [screen.row(y)[right:] for y in range(40)]
        master = next(i for i, line in enumerate(text) if line.strip().startswith("master"))
        track = next(i for i, line in enumerate(text) if line.strip().startswith("1 tone"))
        self.assertLess(master, track)
        self.assertIn("■ 00:00:03.000", text[0])
        self.assertTrue(any("│" in line for line in text[master:track]))  # the playhead crosses the gap
