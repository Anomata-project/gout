import curses
import os
import pty
import select
import struct
import subprocess
import sys
import time

from helpers import GoutTest, gout_attr, gout_cmd


class FakeScreen:
    """Enough of a curses window for the ui to draw into and be read back."""

    def __init__(self, h: int = 36, w: int = 120):
        self.h, self.w = h, w
        self.cells: dict = {}
        self.cursor = (0, 0)
        self.queue: list = []

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.cells.clear()

    def addstr(self, y, x, text, attr=0):
        for i, ch in enumerate(text):
            if x + i >= self.w:
                raise curses.error("off screen")
            self.cells[(y, x + i)] = ch

    def move(self, y, x):
        self.cursor = (y, x)

    def refresh(self):
        pass

    def nodelay(self, flag):
        pass

    def timeout(self, ms):
        pass

    def get_wch(self):
        if not self.queue:
            raise curses.error("no input")
        return self.queue.pop(0)

    def row(self, y):
        return "".join(self.cells.get((y, x), " ") for x in range(self.w)).rstrip()


class UiTest(GoutTest):
    def open_ui(self, root):
        os.environ["GOUT_ADDONS"] = str(self.addons)
        Project = gout_attr("project", "Project")
        Tui = gout_attr("tui", "Tui")
        project = Project(root)
        screen = FakeScreen()
        return project, screen, Tui(project, screen)

    def type_on_row(self, ui, row_id: str, text: str):
        ui.sheet_rows = ui.sheet_build()
        ui.sheet_cur = next(i for i, r in enumerate(ui.sheet_rows) if r["id"] == row_id)
        for ch in text:
            ui.handle(ch)

    def test_prompt_runs_commands_and_follows_the_output(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)
        ui.draw()
        first = screen.cursor[0]
        ui.input = "gain 1 -3"
        ui.submit()
        ui.draw()
        self.assertGreater(screen.cursor[0], first)
        self.assertEqual(project.tracks()[0]["gain_db"], -3.0)

    def test_sheet_edits_apply_with_ctrl_s(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)
        ui.handle("\x05")
        self.assertEqual(ui.mode, "sheet")
        self.type_on_row(ui, "set:lufs", "-16")
        self.type_on_row(ui, "t1:gain", "-4")
        self.type_on_row(ui, "t1:pan", "xx")
        ui.handle("\x13")
        self.assertEqual(project.get("lufs"), "-16")
        self.assertEqual(project.tracks()[0]["gain_db"], -4.0)
        self.assertIn("t1:pan", ui.sheet_errors)
        self.assertEqual(ui.mode, "sheet")
        ui.edits.clear()
        ui.sheet_errors.clear()
        self.type_on_row(ui, "t1:pan", "L30")
        ui.handle("\x18")
        self.assertEqual(ui.mode, "prompt")
        self.assertAlmostEqual(project.tracks()[0]["pan"], -0.3)

    def keys(self, ui, *keys):
        for key in keys:
            if isinstance(key, str) and len(key) > 1:
                for ch in key:
                    ui.handle(ch)
            else:
                ui.handle(key)

    def test_editing_the_command_line_and_history(self):
        root = self.project("song", "bass.wav", "click.wav")
        project, screen, ui = self.open_ui(root)
        self.keys(ui, "gan 1 -3", curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_RIGHT, "i", "\n")
        self.assertEqual(project.tracks()[0]["gain_db"], -3.0)
        # recall it, change the track number in the middle of the line, run it again
        self.keys(ui, curses.KEY_UP)
        self.assertEqual(ui.input, "gain 1 -3")
        self.keys(ui, curses.KEY_LEFT, curses.KEY_LEFT, curses.KEY_LEFT, curses.KEY_BACKSPACE, "2", "\n")
        self.assertEqual(project.tracks()[1]["gain_db"], -3.0)
        self.keys(ui, "abc", curses.KEY_LEFT, curses.KEY_DC)
        self.assertEqual(ui.input, "ab")
        self.keys(ui, "\x01", "x")  # ctrl-a, then type at the start
        self.assertEqual(ui.input, "xab")
        self.keys(ui, "\x17")  # ctrl-w deletes the word before the cursor
        self.assertEqual(ui.input, "ab")
        _, _, again = self.open_ui(root)  # the history is kept per project
        self.assertEqual(again.history[-2:], ["gain 1 -3", "gain 2 -3"])

    def test_tab_completes_commands_tracks_presets_and_file_names_with_spaces(self):
        root = self.project("song", "bass.wav")
        takes = self.tmp / "takes"
        takes.mkdir()
        import shutil
        shutil.copy(self.fx / "tone.wav", takes / "Sandi piano .wav")
        project, screen, ui = self.open_ui(root)
        self.keys(ui, f"add {self.tmp}/ta", "\t")
        self.assertEqual(ui.input, f"add {self.tmp}/takes/")
        self.keys(ui, "\t")
        self.assertEqual(ui.input, f"add {self.tmp}/takes/Sandi\\ piano\\ .wav ")
        self.keys(ui, "\n")
        self.assertEqual([t["name"] for t in project.tracks()], ["bass", "Sandi-piano"])
        for typed, completed in (("reve", "reverb "), ("mute ba", "mute bass "), ("reverb 1 ha", "reverb 1 hall "),
                                 ("fx 1 add co", "fx 1 add comp ")):
            ui.input = typed
            self.keys(ui, "\t")
            self.assertEqual(ui.input, completed, typed)
        ui.input = "m"
        self.keys(ui, "\t")  # several commands: they are listed, the line stays
        self.assertIn("mix", ui.log[-1])
        self.assertIn("move", ui.log[-1])
        ui.input = ""
        page = ui.cheat_scroll
        ui.sheet_h, ui.sheet_len = 5, 40
        self.keys(ui, "\t")  # on an empty line tab still flips the cheat sheet
        self.assertNotEqual(ui.cheat_scroll, page)

    def test_cheat_sheet_lays_itself_out_for_the_width(self):
        counts = {}
        for width in (40, 60, 90, 150):
            text = self.gout("cheat", "-w", str(width)).stdout
            lines = text.rstrip("\n").splitlines()
            counts[width] = len(lines)
            self.assertTrue(all(len(line) <= width for line in lines), width)
            self.assertNotIn("..", text.replace("[-st ..]", "").replace("FILE...", ""), width)  # wrapped, never cut
            words = " ".join(text.split())
            for preset in ("voice", "mud", "flat", "cathedral", "limit"):  # every preset of every effect
                self.assertIn(preset, words, width)
        self.assertLess(counts[150], counts[90])
        self.assertLess(counts[90], counts[60])
        self.assertLess(counts[60], counts[40])
        self.assertIn("presets: voice podcast warm air bright mud clean phone bass kick guitar flat",
                      self.gout("cheat", "-w", "130").stdout)
        wide = self.gout("cheat", "-w", "150").stdout
        self.assertTrue(any(line.startswith("TRACKS") and line.rstrip().endswith("PROJECT")
                            for line in wide.splitlines()), "two columns side by side")

        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)
        screen.w = 200
        ui.input = "split 70"
        ui.submit()
        ui.draw()
        narrow = ui.sheet_len
        ui.input = "split 25"  # the panel gets wider: the sheet takes fewer lines
        ui.submit()
        ui.draw()
        self.assertLess(ui.sheet_len, narrow)

    def test_ctrl_g_hides_the_effect_pictures_but_keeps_the_name_line(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)

        def panel():
            ui.draw()
            rows = [screen.row(y) for y in range(screen.h)]
            head = next(y for y, row in enumerate(rows) if "1 bass  eq" in row)
            sheet = next(y for y, row in enumerate(rows) if y > head and "cheat sheet  " in row)
            return rows[head], sheet - head - 1  # the name line, and how many picture rows under it

        ui.input = "eq 1 hp80"
        ui.submit()
        head, pictures = panel()
        self.assertIn("eq hp80", head)
        self.assertIn("ctrl-g hides", head)
        self.assertGreater(pictures, 3)

        ui.handle("\x07")
        head, pictures = panel()
        self.assertIn("eq hp80", head)
        self.assertIn("ctrl-g shows", head)
        self.assertEqual(pictures, 0)
        self.assertEqual(project.get("ui_fx_pictures"), "off")

        ui.input = "eq 1 hp120"  # changing the effect updates the name line, the pictures stay hidden
        ui.submit()
        head, pictures = panel()
        self.assertIn("eq hp120", head)
        self.assertEqual(pictures, 0)

        Tui = gout_attr("tui", "Tui")
        self.assertFalse(Tui(project, FakeScreen()).show_panel)  # remembered per project

        ui.handle("\x07")
        head, pictures = panel()
        self.assertIn("ctrl-g hides", head)
        self.assertGreater(pictures, 3)

    def test_effect_panel_lets_go_of_an_effect_that_was_removed(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)

        def line_for(command):
            ui.input = command
            ui.submit()
            ui.draw()
            return next((screen.row(y).split("│", 1)[1] for y in range(screen.h)
                         if "│ 1 bass  " in screen.row(y) and "ctrl-g" in screen.row(y)), None)

        self.assertIn("reverb 0.8s", line_for("reverb 1 room"))
        self.assertIsNone(line_for("fx 1 clear"))  # the chain is empty: no panel line
        self.assertEqual(project.tracks()[0]["fx"], [])

        line_for("eq 1 hp80")
        self.assertIn("comp", line_for("comp 1 vocal"))
        self.assertIn("eq hp80", line_for("fx 1 2 rm"))  # the comp went: the eq that is left
        self.assertIn("comp none", line_for("comp 1"))  # a look at an effect the track never had
        self.assertIsNone(line_for("eq 1 clear"))

        line_for("delay 1 slap")
        self.assertIsNone(line_for("undo"))  # undo takes it away too
        self.assertEqual(project.tracks()[0]["fx"], [])

    def test_ctrl_u_undoes_and_ctrl_t_toggles_the_timeline(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)
        for line in ("gain 1 -3", "pan 1 L30"):
            ui.input = line
            ui.submit()
        ui.input = "mute 1"  # half typed: ctrl-u leaves the line alone
        ui.handle("\x15")
        self.assertEqual(ui.input, "mute 1")
        self.assertIn("undo  pan", "\n".join(ui.log[-3:]))
        track = project.tracks()[0]
        self.assertEqual((track["gain_db"], track["pan"]), (-3.0, 0))
        ui.handle("\x15")  # again: the change before
        self.assertEqual(project.tracks()[0]["gain_db"], 0.0)
        ui.handle("\x15")  # and the add itself
        self.assertEqual(project.tracks(), [])
        for _ in range(10):  # the fixture's own setup steps, then the start of the history
            ui.handle("\x15")
            if "nothing to undo" in ui.log[-1]:
                break
        self.assertIn("nothing to undo", ui.log[-1])

        shown = ui.show_timeline
        ui.handle("\x14")
        self.assertEqual(ui.show_timeline, not shown)
        self.assertEqual(project.get("ui_timeline"), "on" if not shown else "off")
        ui.handle("\x14")
        self.assertEqual(ui.show_timeline, shown)

    def test_grey_suggestion_is_taken_with_the_right_arrow(self):
        root = self.project("song", "bass.wav")
        project, screen, ui = self.open_ui(root)
        ui.input = "gain 1 -3"
        ui.submit()
        self.keys(ui, "ga")
        ui.draw()
        prompt_row = screen.cursor[0]
        self.assertIn("gain 1 -3", screen.row(prompt_row))  # typed part plus the dim rest
        self.assertEqual(ui.input, "ga")
        self.keys(ui, curses.KEY_RIGHT)
        self.assertEqual(ui.input, "gain 1 -3")

    def test_real_terminal_session(self):
        root = self.project("song", "bass.wav")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(root)
            env = self.env()
            env["TERM"] = "xterm-256color"
            os.execve(sys.executable, [sys.executable, *gout_cmd()[1:]], env)
        import fcntl
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
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
        for keys in (b"ls\n", b"\x14", b"\x14", b"gan 1 -2", b"\x1b[H", b"\x1b[C", b"\x1b[C", b"i\n", b"quit\n"):
            os.write(fd, keys)
            drain(0.8)
        _, status = os.waitpid(pid, 0)
        text = output.decode("utf-8", "replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, text[-2000:])
        self.assertIn("timeline", text)
        self.assertNotIn("Traceback", text)
        self.assertEqual(self.dump()["tracks"][0]["gain_db"], -2.0)  # home and right arrow edited the line
