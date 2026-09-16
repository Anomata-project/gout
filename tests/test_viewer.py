import json
import os
import shutil
import subprocess
import unittest
import urllib.error
import urllib.request
from array import array
from unittest import mock

from helpers import GoutTest, REPO, ffmpeg, gout_attr
from test_ui import FakeScreen


class ViewerTest(GoutTest):
    def song(self):
        """A 3 s mono track, as a recorded take is: a 0.5 peak for the first second, 0.25 after it; a
        part cut at 2.5 s on the timeline."""
        path = self.tmp / "steps.wav"
        ffmpeg("-f", "lavfi", "-i", "aevalsrc='if(lt(t,1),0.5,0.25)*sin(2*PI*100*t)':s=48000:d=3",
               "-c:a", "pcm_f32le", str(path))
        root = self.project("song")
        self.gout("add", str(path), "-a", "500ms")
        self.gout("part", "1", "2.5s")
        return root

    def serve(self, root):
        Project, Viewer = gout_attr("project", "Project"), gout_attr("viewer", "Viewer")
        project_state, load_theme = gout_attr("viewer", "project_state"), gout_attr("theme", "load_theme")
        project = Project(root)
        viewer = Viewer(project.root, project.rate).start()
        self.addCleanup(viewer.stop)
        viewer.publish_state(*project_state(project, load_theme(project.root)[0]))
        return viewer

    def get(self, viewer, path, host=None):
        request = urllib.request.Request(f"http://127.0.0.1:{viewer.port}{path}",
                                         headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    def test_the_waveform_levels_hold_the_real_peaks_and_are_cached(self):
        root = self.song()
        peak_levels = gout_attr("viewer", "peak_levels")
        cache = root / ".gout" / "peaks"
        levels = peak_levels(root / "master" / "steps.wav", 48000, cache)
        self.assertEqual(len(levels), 5)
        finest = levels[0]
        self.assertEqual(len(finest) // 2, -(-3 * 48000 // 256))  # a point per 256 frames
        first_second = finest[1:2 * (48000 // 256):2]
        self.assertAlmostEqual(max(first_second), 0.5, delta=0.01)
        self.assertAlmostEqual(max(finest[1 + 2 * (48000 // 256 + 2)::2]), 0.25, delta=0.01)
        self.assertTrue(all(lo <= hi for lo, hi in zip(finest[::2], finest[1::2])))
        self.assertAlmostEqual(max(levels[-1][1::2]), 0.5, delta=0.01)  # the coarsest keeps the loudest
        self.assertEqual(len(list(cache.glob("*.peaks"))), 1)
        again = peak_levels(root / "master" / "steps.wav", 48000, cache)  # from the cache
        self.assertEqual([list(a) for a in again], [list(a) for a in levels])

    def test_the_server_shows_the_project_only_with_its_token_and_host(self):
        root = self.song()
        viewer = self.serve(root)
        token = viewer.token
        self.assertEqual(self.get(viewer, "/")[0], 200)  # the page itself holds nothing of the project
        self.assertEqual(self.get(viewer, "/state")[0], 403)
        self.assertEqual(self.get(viewer, f"/state?t={token}x")[0], 403)
        self.assertEqual(self.get(viewer, f"/state?t={token}", host="evil.example")[0], 403)  # rebinding
        status, body = self.get(viewer, f"/state?t={token}")
        state = json.loads(body)
        track = state["tracks"][0]
        self.assertEqual((track["name"], track["offset_ms"], [p["label"] for p in track["parts"]]), ("steps", 500, ["p1", "p2"]))
        self.assertEqual(state["length_ms"], 3500)

        status, body = self.get(viewer, f"/peaks?t={token}&file=steps.wav")
        head = array("I")
        head.frombytes(body[:12])
        self.assertEqual((status, list(head)), (200, [48000, 256, 5]))
        self.assertEqual(self.get(viewer, f"/peaks?t={token}&file=../gout.db")[0], 404)  # only what it was told
        status, body = self.get(viewer, f"/samples?t={token}&file=steps.wav&from=1000&to=1100")
        frames = array("f")
        frames.frombytes(body[8:])
        self.assertEqual(len(frames), 2 * 4800)
        self.assertAlmostEqual(max(frames), 0.25, delta=0.01)

        request = urllib.request.Request(f"http://127.0.0.1:{viewer.port}/key?t={token}", method="POST",
                                         data=json.dumps({"seek": 1500}).encode())
        self.assertEqual(urllib.request.urlopen(request, timeout=10).status, 204)
        self.assertEqual(viewer.keys.get(timeout=5), {"seek": 1500})
        viewer.publish_play(2000, True, {"at": 1000, "peaks": [0.1, 0.2], "label": "rec"})
        with urllib.request.urlopen(f"http://127.0.0.1:{viewer.port}/events?t={token}", timeout=10) as reply:
            event = json.loads(reply.readline().decode().removeprefix("data: "))
        self.assertEqual((event["pos"], event["playing"], event["take"]["peaks"]), (2000, True, [0.1, 0.2]))

    def test_ctrl_o_opens_the_window_and_its_keys_work_the_ui(self):
        root = self.song()
        Project, Tui, Viewer = gout_attr("project", "Project"), gout_attr("tui", "Tui"), gout_attr("viewer", "Viewer")
        with mock.patch.object(Viewer, "open", return_value="a test browser") as opened:
            project = Project(root)
            ui = Tui(project, FakeScreen())
            ui.handle("\x0f")  # ctrl-o
            self.addCleanup(lambda: ui.viewer and ui.viewer.stop())
            self.assertEqual(opened.call_count, 1)
            self.assertIn("window  the timeline in a test browser", ui.log[-1])
            viewer = ui.viewer
            self.assertEqual(json.loads(viewer.state)["tracks"][0]["name"], "steps")
            viewer.keys.put({"seek": 1200})
            ui.tell_viewer()
            self.assertEqual(ui.playhead_ms, 1200)
            ui.input = "gain 1 -3"
            ui.submit()
            ui.viewer_checked = 0
            ui.tell_viewer()  # a change reaches the window
            self.assertEqual(json.loads(viewer.state)["tracks"][0]["gain_db"], -3.0)
            ui.input = "window"
            ui.submit()
            self.assertEqual(opened.call_count, 2)  # the same server, shown again
            self.assertIs(ui.viewer, viewer)

    @unittest.skipUnless(shutil.which("node"), "node checks the page's script")
    def test_the_page_script_parses(self):
        result = subprocess.run(["node", "--check", str(REPO / "gout" / "viewerpage" / "viewer.js")],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_window_asks_the_ui_for_part_duplicate_loop_and_a_stretch_to_play(self):
        root = self.song()
        Project, Tui, Viewer = gout_attr("project", "Project"), gout_attr("tui", "Tui"), gout_attr("viewer", "Viewer")
        with mock.patch.object(Viewer, "open", return_value="a test browser"), \
                mock.patch.dict(os.environ, {"GOUT_PLAYER": "null"}):
            project = Project(root)
            ui = Tui(project, FakeScreen())
            ui.handle("\x0f")
            self.addCleanup(lambda: ui.viewer and ui.viewer.stop())
            ui.viewer.keys.put({"argv": ["loop", "1000ms", "1500ms"]})
            ui.viewer.keys.put({"argv": ["duplicate", "1", "1000ms", "1500ms"]})
            ui.viewer.keys.put({"argv": ["rm", "1"]})  # not something the window may ask for
            ui.viewer.keys.put({"argv": ["move", "1", "p2", "+300ms"]})  # a part dragged by its strip
            ui.tell_viewer()
            self.assertEqual([t["name"] for t in project.tracks()], ["steps", "steps-copy"])
            self.assertEqual([p["shift_ms"] for p in project.tracks()[0]["parts"]], [0, 300])
            self.assertIn("> duplicate 1 1000ms 1500ms  (window)", ui.log)
            ui.tell_viewer()
            self.assertEqual(json.loads(ui.viewer.state)["loop"], {"from": 1000, "to": 1500, "on": True})
            ui.viewer.keys.put({"play": [1000, 1300]})
            ui.tell_viewer()
            self.assertIsNotNone(ui.player)
            self.assertEqual(ui.player.end_s, 1.3)  # a stretch plays to its end, and not round the loop
            self.assertIsNone(ui.player.loop)
            import time
            time.sleep(1.2)
            ui.check_player()
            self.assertIsNone(ui.player)
            self.assertEqual(ui.playhead_ms, 1000)  # back to where the stretch began
