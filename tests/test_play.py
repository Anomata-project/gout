import os
import time

from helpers import GoutTest, gout_attr
from test_ui import FakeScreen


class PlayTest(GoutTest):
    def test_play_streams_a_stale_project_live_and_takes_real_time(self):
        root = self.project("song", "click.wav")  # 4 s
        started = time.monotonic()
        out = self.gout("play", "2.5s").stdout
        self.assertIn("live", out)
        self.assertIn("finished", out)
        self.assertGreater(time.monotonic() - started, 1.4)  # 1.5 s of audio
        self.assertFalse((root / "master.wav").exists())  # nothing rendered
        out = self.gout("play", "3s", "-r").stdout  # -r renders first, then plays the file
        self.assertIn("rendering", out)
        self.assertNotIn("live", out)
        self.assertTrue((root / "master.wav").exists())
        self.assertNotIn("live", self.gout("play", "3.5s").stdout)  # current: the file
        self.gout("gain", "1", "-3")
        self.assertIn("live", self.gout("play", "3.5s").stdout)  # a change: live again
        self.gout("play", "10s", ok=False)
        self.gout("stop", ok=False)  # only means something in the ui

    def test_space_plays_and_stops_with_a_playhead(self):
        root = self.project("song", "click.wav")
        os.environ["GOUT_PLAYER"] = "null"
        os.environ["GOUT_ADDONS"] = str(self.addons)
        project = gout_attr("project", "Project")(root)
        screen = FakeScreen(30, 120)
        ui = gout_attr("tui", "Tui")(project, screen)
        ui.handle(" ")  # empty line: play, live since nothing is rendered
        self.assertIsNotNone(ui.player)
        self.assertTrue(ui.player.live)
        self.assertFalse(project.master.exists())
        time.sleep(0.6)
        ui.draw()
        header = next(screen.row(y) for y in range(30) if "timeline" in screen.row(y))
        self.assertIn("▶", header)
        ui.handle(" ")  # stop, keeping the playhead
        self.assertIsNone(ui.player)
        self.assertGreater(ui.playhead_ms, 400)
        stopped = ui.playhead_ms
        ui.handle(" ")
        self.assertGreaterEqual(ui.play_position_ms(), stopped)  # carries on from there
        ui.input = "stop"
        ui.submit()
        ui.input = "play 3s"
        ui.submit()
        self.assertGreaterEqual(ui.play_position_ms(), 3000)
        import curses
        ui.handle(curses.KEY_LEFT)  # empty line while playing: back 5 s (not before 0)
        self.assertLess(ui.play_position_ms(), 1000)
        ui.input = "play 3.8s"
        ui.submit()
        time.sleep(0.6)
        ui.check_player()  # it played to the end
        self.assertIsNone(ui.player)
        self.assertEqual(ui.playhead_ms, 0)
        ui.handle("x")
        ui.handle(" ")  # a space on a line with text is just a space
        self.assertEqual(ui.input, "x ")
        self.assertIsNone(ui.player)
        ui.input = ""
        ui.handle(" ")
        ui.input = "quit"
        ui.submit()  # quitting stops playback
        self.assertIsNone(ui.player)
        self.assertFalse(ui.running)

    def test_a_held_arrow_is_one_jump_and_one_restart(self):
        # a held arrow repeats every 30 ms or so, and starting playback again takes about 0.2 s: one
        # restart per press left gout busy for 20 s after 4 s of holding left. The presses already
        # waiting are one jump now, from the terminal (both ways arrows arrive) and from the window.
        import curses
        import queue
        from unittest import mock
        root = self.project("song", "tone.wav")
        self.gout("move", "1", "20s")  # 26 s long
        os.environ["GOUT_PLAYER"] = "null"
        os.environ["GOUT_ADDONS"] = str(self.addons)
        project = gout_attr("project", "Project")(root)
        screen = FakeScreen(30, 120)
        ui = gout_attr("tui", "Tui")(project, screen)
        ui.input = "play 15s"
        ui.submit()
        with mock.patch.object(ui, "start_playing", wraps=ui.start_playing) as restarts:
            screen.queue = [curses.KEY_LEFT] * 20 + ["\x1b", "[", "D"] * 5 + ["x", curses.KEY_LEFT]
            ui.handle(curses.KEY_LEFT)
            self.assertEqual(restarts.call_count, 1)
            self.assertLess(ui.play_position_ms(), 500)  # back to the start, and not before it
            self.assertEqual(ui.pending, ["x", curses.KEY_LEFT])  # what came after waits its turn
            ui.pending.clear()

            ui.seek(15000)
            restarts.reset_mock()
            screen.queue = ["[", "D", "\x1b", "[", "D"]  # a terminal that sends the plain form
            ui.handle("\x1b")
            self.assertEqual(restarts.call_count, 1)
            self.assertGreaterEqual(ui.play_position_ms(), 5000)  # two presses: 10 s back
            self.assertLess(ui.play_position_ms(), 5500)
            self.assertEqual(screen.queue + ui.pending, [])

            class Window:  # what tell_viewer needs of the viewer
                keys = queue.Queue()
                def publish_state(self, *args): pass
                def publish_play(self, *args): pass
            ui.viewer = Window()
            ui.seek(-10000)
            restarts.reset_mock()
            for key in ("right", "right", "right", "left"):  # a held right, then left: 10 s on
                ui.viewer.keys.put({"key": key})
            ui.tell_viewer()
            self.assertEqual(restarts.call_count, 1)
            self.assertGreaterEqual(ui.play_position_ms(), 10000)
            self.assertLess(ui.play_position_ms(), 10500)
        ui.viewer = None
        ui.stop_playing(keep=False)

    def test_a_change_while_playing_is_heard_at_once(self):
        root = self.project("song", "tone.wav")  # 6 s
        self.gout("set", "head", "1s")
        self.gout("mix")
        os.environ["GOUT_PLAYER"] = "null"
        os.environ["GOUT_ADDONS"] = str(self.addons)
        project = gout_attr("project", "Project")(root)
        ui = gout_attr("tui", "Tui")(project, FakeScreen(30, 120))
        ui.input = "play 1s"
        ui.submit()
        self.assertFalse(ui.player.live)  # master.wav is current
        time.sleep(0.6)
        first = ui.player
        ui.input = "ls"  # nothing changes: the same playback goes on
        ui.submit()
        self.assertIs(ui.player, first)
        ui.input = "eq 1 hs7k:+4"
        ui.submit()
        self.assertIsNot(ui.player, first)  # started again, with the eq
        self.assertTrue(ui.player.live)
        self.assertIn("again from", ui.log[-1])
        self.assertGreaterEqual(ui.play_position_ms(), 1500)  # where it was, not the head padding off
        self.assertLess(ui.play_position_ms(), 2500)
        playing = ui.player
        ui.handle("\x15")  # ctrl-u: undo is a change too
        self.assertIsNot(ui.player, playing)
        ui.input = "quit"
        ui.submit()
