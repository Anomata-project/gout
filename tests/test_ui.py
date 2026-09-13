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
        for keys in (b"ls\n", b"\x15", b"\x15", b"quit\n"):
            os.write(fd, keys)
            drain(0.8)
        _, status = os.waitpid(pid, 0)
        text = output.decode("utf-8", "replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, text[-2000:])
        self.assertIn("timeline", text)
        self.assertNotIn("Traceback", text)
