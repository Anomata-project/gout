import curses
import importlib.util
import json
import os
import pty
import select
import shutil
import struct
import sys
import time

from helpers import REPO, GoutTest, ffmpeg, gout_attr, gout_cmd
from test_ui import FakeScreen

FRACTAL = REPO / "examples" / "addons" / "fractal.py"


def load_fractal():
    spec = importlib.util.spec_from_file_location("gout_test_fractal", FRACTAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScreenTest(GoutTest):
    def song(self):
        """4 s: a 60 Hz bass for the first second, silence, hiss above 5 kHz in the third, silence."""
        path = self.tmp / "bands.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=f=60:d=4", "-f", "lavfi", "-i", "anoisesrc=d=4:a=0.5",
               "-filter_complex",
               "[0:a]volume='if(lt(t,1),1,0)':eval=frame[b];"
               "[1:a]highpass=f=7000,highpass=f=7000,volume='if(between(t,2,3),1,0)':eval=frame[h];"
               "[b][h]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo[o]",
               "-map", "[o]", "-ar", "48000", "-c:a", "pcm_s16le", str(path))
        return path

    def features(self, root):
        Project = gout_attr("project", "Project")
        project_features = gout_attr("analysis", "project_features")
        return project_features(Project(root))

    def assert_bands_follow_the_song(self, f, start_s):
        at = lambda name, s: f.at(name, (start_s + s) * 1000)
        self.assertGreater(at("low", 0.5), 0.8)
        self.assertLess(at("high", 0.5), 0.2)
        self.assertGreater(at("high", 2.5), 0.8)
        self.assertLess(at("low", 2.5), 0.2)
        self.assertLess(at("level", 1.5), 0.05)       # the silence between
        self.assertGreater(f.mean("onset", (start_s + 0.1) * 1000, 150), 0.1)  # the bass coming in

    def test_analysis_follows_the_bands_in_project_time(self):
        self.project("song")
        self.gout("add", str(self.song()), "-a", "1s")
        root = self.cwd
        tracks = self.features(root)  # master.wav not rendered: the track as the timeline places it
        self.assert_bands_follow_the_song(tracks, 1.0)
        self.assertEqual(tracks.at("level", 500), 0.0)  # before the track starts
        self.assertLess(tracks.total("low", 1000), 0.05)
        self.assertGreater(tracks.total("low", 2000), 0.8)  # a second of bass has gone by

        self.gout("set", "head", "500ms")
        self.gout("mix")
        master = self.features(root)  # now from master.wav, its head skipped
        self.assert_bands_follow_the_song(master, 1.0)
        self.assertTrue(any((root / ".gout" / "analysis").glob("*.bands")))

    def test_screens_register_like_effects_and_refuse_taken_names_and_keys(self):
        Screen = gout_attr("screens", "Screen")
        register_screen = gout_attr("screens", "register_screen")

        def screen(name, key):
            return type("S", (Screen,), {"name": name, "key": key})()

        with self.assertRaisesRegex(ValueError, "'eq' is already taken"):
            register_screen(screen("eq", ""))
        with self.assertRaisesRegex(ValueError, "not free for screens"):
            register_screen(screen("mine", "ctrl-t"))
        with self.assertRaisesRegex(ValueError, "'quit' is already taken"):
            register_screen(screen("quit", ""))

        shutil.copy(FRACTAL, self.addons / "fractal.py")
        self.project("song", "tone.wav")
        listed = self.gout("addons").stdout
        self.assertIn("fractal (screen, ctrl-space)", listed)
        self.assertIn("zoom (screen)", listed)  # the second screen in the same file
        cheat = " ".join(self.gout("cheat", "-w", "100").stdout.split())
        self.assertIn("SCREENS", cheat)
        self.assertIn("ctrl-space or fractal full-screen play", cheat)

    def test_fractal_moves_with_the_music(self):
        self.project("song")
        self.gout("add", str(self.song()))
        Project = gout_attr("project", "Project")
        ScreenContext = gout_attr("screens", "ScreenContext")
        project = Project(self.cwd)
        ctx = ScreenContext(project)
        ctx.features = gout_attr("analysis", "project_features")(project)
        ctx.offline = True  # no resolution that adapts to a busy machine: the same moment, the same picture
        fractal = load_fractal().Fractal()
        fractal.command(ctx, [])

        def frame(seconds):
            ctx.position_ms = seconds * 1000
            rows = fractal.frame(ctx, 80, 24)
            self.assertEqual(len(rows), 24)
            self.assertTrue(all(len(text) == 80 and len(classes) == 80 for text, classes in rows))
            return [text for text, _ in rows]

        bass, silence, hiss = frame(0.6), frame(1.6), frame(2.6)
        self.assertNotEqual(bass, silence)
        self.assertNotEqual(hiss, silence)
        self.assertEqual(frame(1.6), silence)  # the same moment draws the same picture
        self.assertEqual(fractal.status(ctx), "1 seven  w = z³ + 7")
        colours = set("".join(classes for _, classes in fractal.frame(ctx, 80, 24))) - {" "}
        self.assertEqual(len(colours), 3)  # three roots, three colours
        for key in "1234567890":  # every preset draws, with a colour per root it shows
            fractal.key_pressed(ctx, key)
            rows = fractal.frame(ctx, 60, 20)
            self.assertGreaterEqual(len(set("".join(c for _, c in rows)) - {" "}), 2, fractal.status(ctx))
        self.assertEqual(fractal.status(ctx), "0 ladder  w = cosh(z) - 2")

    def test_zoom_dives_in_for_ever_pushed_by_the_music(self):
        self.project("song")
        self.gout("add", str(self.song()))
        Project = gout_attr("project", "Project")
        ScreenContext = gout_attr("screens", "ScreenContext")
        project = Project(self.cwd)
        ctx = ScreenContext(project)
        ctx.features = gout_attr("analysis", "project_features")(project)
        ctx.offline = True
        module = load_fractal()
        zoom = module.Zoom()
        zoom.command(ctx, [])

        def frame(seconds):
            ctx.position_ms = seconds * 1000
            rows = zoom.frame(ctx, 80, 24)
            self.assertEqual(len(rows), 24)
            self.assertTrue(all(len(text) == 80 and len(classes) == 80 for text, classes in rows))
            return rows, zoom.depth

        _, start = frame(0)
        _, bass = frame(1)
        _, silence = frame(2)
        self.assertEqual(start, 0)
        self.assertGreater(bass - start, 1.5 * (silence - bass))  # the bass pushes; silence only drifts
        self.assertTrue(zoom.status(ctx).startswith("1 seven  w = z³ + 7  ×"))
        ctx.choice_ms = 1500  # a video changed to this choice at 1.5 s: the dive starts there
        self.assertLess(frame(2)[1], silence)

        for key in "1234567890":  # every preset keeps drawing ten to the forty times deeper
            zoom.key_pressed(ctx, key)
            self.assertTrue(zoom.loop_for(zoom.preset()), zoom.status(ctx))  # a point to loop around
            zoom.zoom_factor = 1e-40
            rows, depth = frame(0.5)
            text = "".join(t for t, _ in rows)
            self.assertGreater(depth, 90)
            self.assertLess(text.count(" ") / len(text), 0.5, zoom.status(ctx))
            self.assertGreaterEqual(len(set("".join(c for _, c in rows)) - {" "}), 2, zoom.status(ctx))

        zoom.key_pressed(ctx, "1")
        zoom.zoom_factor = 1e-6  # past the loop depth: drawn a loop further up, turned by the loop's angle
        jumped = "".join(t for t, _ in frame(0.5)[0])
        self.addCleanup(setattr, module, "LOOP_DEPTH", module.LOOP_DEPTH)
        module.LOOP_DEPTH = 1e-12
        zoom.last = None
        straight = "".join(t for t, _ in frame(0.5)[0])  # the same moment at its real depth
        self.assertGreater(sum(a == b for a, b in zip(jumped, straight)) / len(jumped), 0.95)
        self.assertEqual(json.loads((self.tmp / "config" / "gout" / "fractal.json").read_text())["zoom_preset"], "seven")

    def test_fractal_presets_live_in_a_file_and_change_by_key_or_word(self):
        Fractal = load_fractal().Fractal
        path = self.tmp / "config" / "gout" / "fractal.json"
        fractal = Fractal()
        lines, go = fractal.command(None, [])
        self.assertTrue(go)
        self.assertTrue(path.exists())
        self.assertIn("wrote the presets", lines[0])
        self.assertEqual(len(json.loads(path.read_text())["presets"]), 10)
        self.assertEqual(fractal.status(None), "1 seven  w = z³ + 7")

        fractal.key_pressed(None, "5")
        self.assertEqual(fractal.status(None), "5 rings  w = z⁸ + 15z⁴ - 16")
        self.assertEqual(json.loads(path.read_text())["preset"], "rings")  # remembered
        again = Fractal()
        again.command(None, [])
        self.assertEqual(again.status(None), "5 rings  w = z⁸ + 15z⁴ - 16")
        again.key_pressed(None, "down")
        self.assertEqual(again.status(None), "6 islands  w = z³ - 2z + 2")

        lines, go = again.command(None, ["presets"])  # a list in the log, and it stays closed
        self.assertFalse(go)
        self.assertIn("cosh(z) - 2", "\n".join(lines))
        self.assertTrue(again.command(None, ["classic"])[1])
        self.assertEqual(again.status(None), "2 classic  w = z³ - 1")
        self.assertTrue(again.command(None, ["9"])[1])
        self.assertEqual(again.status(None), "9 waves  w = sin(z)")
        lines, go = again.command(None, ["z^5", "-", "3z", "+", "1"])
        self.assertTrue(go)
        self.assertEqual(again.status(None), "custom  w = z⁵ - 3z + 1")
        with self.assertRaisesRegex(ValueError, "unknown name 'x'"):
            again.command(None, ["x^2"])

        doc = json.loads(path.read_text())
        doc["presets"] += [{"name": "mine", "formula": "z^4 - 3i"}, {"name": "bad", "formula": "z^^2"}]
        doc["status_seconds"] = 0
        path.write_text(json.dumps(doc))
        lines, _ = again.command(None, ["mine"])
        self.assertIn("bad", "\n".join(lines))
        self.assertEqual(again.status(None), "mine  w = z⁴ - 3i")
        self.assertEqual(again.status_seconds, 0)
        self.assertEqual(len(again.frame(type("C", (), {"position_ms": 0, "band": lambda *a: 0.0,
                                                        "travel": lambda *a: 0.0})(), 40, 12)), 12)

        path.write_text("{ not json")
        lines, _ = Fractal().command(None, [])
        self.assertIn("unreadable", "\n".join(lines))

    def test_fullscreen_finds_the_window_gout_runs_in_and_puts_it_back(self):
        TerminalWindow = gout_attr("window", "TerminalWindow")

        class Bus:
            def __init__(self, ours, already=()):
                self.ours, self.full, self.calls = ours, set(already), []

            def run(self, args):
                if args[0] == "introspect":
                    return "node /org/gnome/Terminal/window {\n  node 1 {\n  };\n  node 2 {\n  };\n};\n"
                path, method = args[args.index("--object-path") + 1], args[args.index("--method") + 1]
                if method.endswith("Describe"):
                    return f"((true, signature '', [<{'true' if path in self.full else 'false'}>]),)\n"
                action = args[args.index("--method") + 2]
                self.calls.append((path.rsplit("/", 1)[1], action))
                (self.full.add if action == "enter-fullscreen" else self.full.discard)(path)
                return "()\n"

            def size(self):
                return (200, 60) if f"/org/gnome/Terminal/window/{self.ours}" in self.full else (100, 30)

        def window(bus):
            return TerminalWindow({"GNOME_TERMINAL_SERVICE": ":1.9"}, run=bus.run, size=bus.size,
                                  sleep=lambda s: None, wait_s=0.1)

        bus = Bus(ours=2)  # the newest window is tried first
        w = window(bus)
        self.assertEqual(w.enter(), "")
        self.assertEqual(bus.calls, [("2", "enter-fullscreen")])
        w.leave()
        self.assertEqual(bus.calls[-1], ("2", "leave-fullscreen"))

        bus = Bus(ours=1)  # not the newest: that one is put back, then the next one tried
        w = window(bus)
        self.assertEqual(w.enter(), "")
        self.assertEqual(bus.calls, [("2", "enter-fullscreen"), ("2", "leave-fullscreen"), ("1", "enter-fullscreen")])
        w.leave()
        bus.calls.clear()
        w.enter()  # found once, remembered
        self.assertEqual(bus.calls, [("1", "enter-fullscreen")])

        bus = Bus(ours=1, already={"/org/gnome/Terminal/window/2"})  # a fullscreen window is left alone
        w = window(bus)
        self.assertEqual(w.enter(), "")
        self.assertEqual(bus.calls, [])
        w.leave()
        self.assertEqual(bus.calls, [])

        self.assertFalse(TerminalWindow({}).available())  # other terminals: nothing to ask

    def test_ctrl_space_opens_the_screen_and_esc_goes_back(self):
        register_screen = gout_attr("screens", "register_screen")
        registry = gout_attr("screens", "screens")()
        registry.pop("fractal", None)
        register_screen(load_fractal().Fractal(), "fractal.py")
        self.addCleanup(registry.pop, "fractal", None)
        saved = os.environ.get("GOUT_PLAYER")
        os.environ["GOUT_PLAYER"] = "null"
        self.addCleanup(lambda: os.environ.pop("GOUT_PLAYER") if saved is None else os.environ.update(GOUT_PLAYER=saved))

        self.project("song")
        self.gout("add", str(self.song()))
        Project, Tui = gout_attr("project", "Project"), gout_attr("tui", "Tui")
        screen = FakeScreen(30, 100)
        ui = Tui(Project(self.cwd), screen)
        ui.input = "gain 1"
        ui.handle("\x00")  # ctrl-space
        self.assertEqual(ui.mode, "screen")
        self.assertIsNotNone(ui.player)  # full-screen play
        for _ in range(100):
            if ui.screen_ctx.features is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(ui.screen_ctx.features)
        ui.draw()
        status = screen.row(29)
        self.assertIn("1 seven  w = z³ + 7", status)
        self.assertIn("esc back to gout", status)
        self.assertEqual(len(screen.row(0)), 100)
        ui.handle(curses.KEY_DOWN)
        ui.draw()
        self.assertIn("2 classic  w = z³ - 1", screen.row(29))
        ui.handle(" ")  # space stops, as in the daw
        self.assertIsNone(ui.player)
        ui.handle("\x1b")  # esc: back
        self.assertEqual(ui.mode, "prompt")
        self.assertEqual(ui.input, "gain 1")  # the half-typed line is still there
        ui.draw()
        self.assertIn("timeline", screen.row(0))

        ui.input = "fz"  # the short name at the prompt opens it too
        ui.submit()
        self.assertEqual(ui.mode, "screen")
        ui.handle("\x00")  # and its key closes it
        self.assertEqual(ui.mode, "prompt")
        ui.stop_playing(keep=False)

    def test_the_status_line_hides_while_playing_and_a_key_brings_it_back(self):
        register_screen = gout_attr("screens", "register_screen")
        registry = gout_attr("screens", "screens")()
        registry.pop("fractal", None)
        register_screen(load_fractal().Fractal(), "fractal.py")
        self.addCleanup(registry.pop, "fractal", None)
        saved = os.environ.get("GOUT_PLAYER")
        os.environ["GOUT_PLAYER"] = "null"
        self.addCleanup(lambda: os.environ.pop("GOUT_PLAYER") if saved is None else os.environ.update(GOUT_PLAYER=saved))
        config = self.tmp / "config" / "gout" / "fractal.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"preset": "star", "status_seconds": 0.2,
                                      "presets": [{"name": "star", "formula": "z^5 - 1"}]}))

        root = self.project("song", "tone.wav")
        Project, Tui = gout_attr("project", "Project"), gout_attr("tui", "Tui")
        screen = FakeScreen(20, 80)
        ui = Tui(Project(root), screen)
        ui.input = "fractal presets"  # a list, not the screen
        ui.submit()
        self.assertEqual(ui.mode, "prompt")
        self.assertIn("z⁵ - 1", "\n".join(ui.log))
        ui.input = "fractal w^2"
        ui.submit()
        self.assertEqual(ui.mode, "prompt")
        self.assertIn("fractal: unknown name 'w'", ui.log[-1])
        ui.input = "fractal star"
        ui.submit()
        self.assertEqual(ui.mode, "screen")
        ui.draw()
        self.assertIn("star  w = z⁵ - 1", screen.row(19))
        time.sleep(0.3)
        ui.draw()
        self.assertNotIn("esc back to gout", screen.row(19))  # only the picture
        ui.handle("c")
        ui.draw()
        self.assertIn("esc back to gout", screen.row(19))  # a key brings it back
        time.sleep(0.3)
        ui.handle(" ")  # paused: it stays
        ui.draw()
        self.assertIn("space plays", screen.row(19))
        ui.handle("\x1b")
        self.assertEqual(ui.mode, "prompt")

    def test_a_screen_that_fails_closes_and_says_why(self):
        Screen = gout_attr("screens", "Screen")
        register_screen = gout_attr("screens", "register_screen")
        registry = gout_attr("screens", "screens")()

        class Broken(Screen):
            name, key, play_on_open = "broken", "ctrl-b", False

            def frame(self, ctx, width, height):
                raise RuntimeError("no picture")

        register_screen(Broken(), "broken.py")
        self.addCleanup(registry.pop, "broken", None)
        root = self.project("song", "tone.wav")
        Project, Tui = gout_attr("project", "Project"), gout_attr("tui", "Tui")
        ui = Tui(Project(root), FakeScreen())
        ui.handle("\x02")
        ui.draw()
        self.assertEqual(ui.mode, "prompt")
        self.assertIn("broken screen: RuntimeError: no picture", "\n".join(ui.log))

    def test_real_terminal_ctrl_space(self):
        shutil.copy(FRACTAL, self.addons / "fractal.py")
        root = self.project("song", "tone.wav")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(root)
            env = self.env()
            env["TERM"] = "xterm-256color"
            command = gout_cmd()  # python and the launcher, or a built gout
            os.execve(command[0], command, env)
        import fcntl
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        output = bytearray()

        def drain(seconds):
            end = time.time() + seconds
            while time.time() < end:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    output.extend(chunk)

        drain(1.5)
        os.write(fd, b"\x00")
        drain(1.5)
        seen = output.decode("utf-8", "replace")
        os.write(fd, b"\x1b")
        drain(1.0)
        os.write(fd, b"quit\n")
        drain(1.0)
        _, status = os.waitpid(pid, 0)
        text = output.decode("utf-8", "replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, text[-2000:])
        self.assertIn("z³ + 7", seen)
        self.assertIn("esc back to gout", seen)
        self.assertNotIn("Traceback", text)
