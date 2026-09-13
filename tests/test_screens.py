import curses
import importlib.util
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
        self.assertIn("fractal (screen, ctrl-space)", self.gout("addons").stdout)
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
        fractal = load_fractal().Fractal()

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
        self.assertEqual(fractal.status(ctx), "w = z³ + 7")
        fractal.key_pressed(ctx, "up")
        self.assertEqual(fractal.status(ctx), "w = z⁴ + 7")
        self.assertNotEqual(frame(1.6), silence)
        for _ in range(8):
            fractal.key_pressed(ctx, "-")
        self.assertEqual(fractal.status(ctx), "w = z⁴ - 2")  # it steps over 0

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
        self.assertIn("w = z³ + 7", status)
        self.assertIn("esc back to gout", status)
        self.assertEqual(len(screen.row(0)), 100)
        ui.handle(curses.KEY_UP)
        ui.draw()
        self.assertIn("w = z⁴ + 7", screen.row(29))
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
            os.execve(sys.executable, [sys.executable, *gout_cmd()[1:]], env)
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
