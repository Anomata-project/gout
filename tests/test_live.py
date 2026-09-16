import os
import sqlite3
import time

from helpers import GoutTest, duration, gout_attr, hits, loudness
from test_ui import FakeScreen


class LiveTest(GoutTest):
    def test_changes_are_instant_by_default(self):
        self.gout("new", "song")
        root = self.tmp / "song"
        self.cwd = root
        self.assertIn("autorender  idle", self.gout("set").stdout)
        started = time.monotonic()
        self.gout("add", str(self.fx / "tone.wav"))
        self.gout("reverb", "1", "hall")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse((root / "master.wav").exists())
        self.gout("set", "autorender", "on")
        self.gout("gain", "1", "-3")
        self.assertTrue((root / "master.wav").exists())  # on: after every change, as before
        self.gout("set", "autorender", "idle")
        self.gout("gain", "1", "-4")
        self.assertIn("out of date", self.gout("ls").stdout)

    def test_projects_with_the_old_default_move_to_idle(self):
        root = self.project("song")
        db = sqlite3.connect(root / "gout.db")
        db.execute("UPDATE project SET value = 'on' WHERE key = 'autorender'")
        db.execute("DELETE FROM project WHERE key = 'settings_version'")
        db.commit()
        db.close()
        self.assertIn("autorender  idle", self.gout("set").stdout)
        self.gout("set", "autorender", "on")
        self.assertIn("autorender  on", self.gout("set").stdout)  # a choice made now stays

    def test_live_playback_sounds_like_the_render(self):
        root = self.project("song", "click.wav")  # click at 2.000 s
        self.gout("set", "bpm", "120")
        self.gout("delay", "1", "1/8", "w50", "f50", "n3")
        self.gout("gain", "1", "-3")
        live = self.tmp / "live.wav"
        env_backup = dict(os.environ)
        try:
            out = self.run_player(f"file:{live}", "1.5s")
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
        self.assertIn("live", out)
        self.assertFalse((root / "master.wav").exists())
        found = hits(live)
        self.assertEqual(len(found), 4)
        for (t, level), (want_t, want_level) in zip(found, [(0.5, 1.0), (0.75, 0.5), (1.0, 0.25), (1.25, 0.125)]):
            self.assertAlmostEqual(t, want_t, delta=0.002)
            self.assertAlmostEqual(level, want_level, delta=0.02)
        self.assertAlmostEqual(duration(live), 2.25 + 1.0, delta=0.02)  # the delay's tail plays out

    def test_live_playback_applies_the_last_loudness_gain(self):
        root = self.project("song", "noise.wav")
        self.gout("set", "lufs", "-14")
        self.gout("mix")
        self.gout("set", "title", "changed")  # out of date, sounds the same
        live = self.tmp / "live.wav"
        out = self.run_player(f"file:{live}")
        self.assertIn("live", out)
        self.assertAlmostEqual(loudness(live), -14, delta=0.5)

    def run_player(self, backend, *args):
        env = self.env()
        env["GOUT_PLAYER"] = backend
        import subprocess
        from helpers import gout_cmd
        result = subprocess.run([*gout_cmd(), "play", *args], cwd=self.cwd, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def open_ui(self, root):
        os.environ["GOUT_ADDONS"] = str(self.addons)
        os.environ["GOUT_PLAYER"] = "null"
        project = gout_attr("project", "Project")(root)
        return project, gout_attr("tui", "Tui")(project, FakeScreen(30, 120))

    def settle(self, ui, seconds=1.6):
        """Pretend the ui has been idle for a while."""
        now = time.monotonic()
        ui.background_render(now)
        ui.background_render(now + seconds)

    def wait_render(self, ui, timeout=30):
        end = time.monotonic() + timeout
        while ui.render_proc is not None and ui.render_proc.poll() is None and time.monotonic() < end:
            time.sleep(0.1)
        ui.background_render()

    def test_the_ui_renders_in_the_background_when_idle(self):
        root = self.project("song", "tone.wav")
        self.gout("set", "autorender", "idle")
        project, ui = self.open_ui(root)
        ui.input = "gain 1 -3"
        ui.submit()
        self.assertFalse(project.master.exists())  # the change itself did not render
        ui.background_render(time.monotonic())
        self.assertIsNone(ui.render_proc)  # not idle long enough yet
        self.settle(ui)
        self.assertIsNotNone(ui.render_proc)
        ui.draw()
        self.wait_render(ui)
        self.assertIsNone(ui.render_proc)
        self.assertTrue(project.master_is_current())
        self.assertTrue(any(line.startswith("mix   master.wav") for line in ui.log))
        rows = gout_attr("render", "render_timeline")(project, 120, styled=True)
        master_classes = [classes for label, cells, kind, classes, role in rows if kind == "wave"][0]
        self.assertIn("m", master_classes)  # current: drawn in the master colour
        ui.input = "gain 1 -6"
        ui.submit()
        rows = gout_attr("render", "render_timeline")(project, 120, styled=True)
        self.assertIn("out of date", "".join(label for label, *_ in rows))
        self.settle(ui)
        self.assertIsNotNone(ui.render_proc)
        ui.input = "gain 1 -9"
        ui.submit()
        ui.background_render()  # a change while rendering cancels it
        self.assertIsNone(ui.render_proc)
        self.assertIn("starting over", ui.log[-1])
        self.assertFalse((root / "master.raw.part.wav").exists())
        ui.input = "space"
        ui.input = ""
        ui.handle(" ")  # playing holds back background renders
        self.assertTrue(ui.player.live)
        self.settle(ui)
        self.assertIsNone(ui.render_proc)
        ui.handle(" ")
        ui.input = "quit"
        ui.submit()

    def test_nothing_audible_is_not_retried_forever(self):
        root = self.project("song", "tone.wav")
        self.gout("set", "autorender", "idle")
        self.gout("mute", "1")
        project, ui = self.open_ui(root)
        self.settle(ui)
        self.wait_render(ui)
        self.settle(ui, 3)
        self.assertIsNone(ui.render_proc)
