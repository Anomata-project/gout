"""The ui's command line: an editable line with history, completion and suggestions.

Kept apart from curses so it can be tested directly. The ui feeds it keys it has already
decoded (insert this text, move left, ...), asks it what to draw, and gives it a function that
lists completions for the word under the cursor.

Words are split the way shlex does it on submit: spaces separate, backslash escapes, quotes
group. Completed file names escape their spaces and quotes with backslashes, so
"Sandi piano .m4a" becomes Sandi\\ piano\\ .m4a and reaches the command as one word.
"""
from __future__ import annotations

import os
from pathlib import Path

SPECIAL = set(" \t\"'\\")


def escape(word: str) -> str:
    return "".join("\\" + ch if ch in SPECIAL else ch for ch in word)


def current_word(text: str, cursor: int) -> tuple[int, str, int]:
    """(start index, the word's unescaped value up to the cursor, its position among words)."""
    start, value, index = 0, "", 0
    in_word, quote, escaped = False, "", False
    for i, ch in enumerate(text[:cursor]):
        if escaped:
            value += ch
            escaped = False
        elif ch == "\\" and quote != "'":
            if not in_word:
                start, value, in_word = i, "", True
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
            else:
                value += ch
        elif ch in "\"'":
            if not in_word:
                start, value, in_word = i, "", True
            quote = ch
        elif ch.isspace():
            if in_word:
                index += 1
            in_word, value = False, ""
            start = i + 1
        else:
            if not in_word:
                start, value, in_word = i, "", True
            value += ch
    if not in_word:
        start = cursor
    return start, value, index


def path_candidates(base: Path, value: str) -> list[str]:
    """Files and folders that complete `value`, relative to base; folders end in /."""
    expanded = os.path.expanduser(value)
    folder, _, prefix = expanded.rpartition("/")
    shown_folder = value[:len(value) - len(prefix)]
    directory = Path(folder or ".") if folder.startswith("/") else base / (folder or ".")
    if value.startswith("~") and "/" not in value:
        return []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
    except OSError:
        return []
    out = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        if entry.name.startswith(".") and not prefix.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        out.append(shown_folder + entry.name + ("/" if is_dir else ""))
    return out


def common_prefix(words: list[str]) -> str:
    if not words:
        return ""
    first, last = min(words), max(words)
    n = 0
    while n < len(first) and n < len(last) and first[n] == last[n]:
        n += 1
    return first[:n]


class LineEditor:
    """Text, a cursor and a history. `completer(words_before, value)` returns candidate words."""

    def __init__(self, completer=None):
        self.text = ""
        self.cursor = 0
        self.history: list[str] = []
        self.hist_i: int | None = None
        self.draft = ""
        self.completer = completer or (lambda before, value: [])

    # ---- editing

    def set(self, text: str) -> None:
        self.text, self.cursor = text, len(text)

    def insert(self, chars: str) -> None:
        self.text = self.text[:self.cursor] + chars + self.text[self.cursor:]
        self.cursor += len(chars)

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        """Move right; at the end of the line, take the suggestion instead."""
        if self.cursor < len(self.text):
            self.cursor += 1
        else:
            self.accept()

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        if self.cursor < len(self.text):
            self.cursor = len(self.text)
        else:
            self.accept()

    def delete_word(self) -> None:
        """ctrl-w: the word before the cursor, and the spaces after it."""
        head = self.text[:self.cursor].rstrip()
        cut = head.rfind(" ") + 1
        self.text = self.text[:cut] + self.text[self.cursor:]
        self.cursor = cut

    def clear(self) -> None:
        self.set("")

    # ---- history

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self.hist_i, self.draft = None, ""

    def up(self) -> None:
        if not self.history:
            return
        if self.hist_i is None:
            self.draft, self.hist_i = self.text, len(self.history) - 1
        else:
            self.hist_i = max(0, self.hist_i - 1)
        self.set(self.history[self.hist_i])

    def down(self) -> None:
        if self.hist_i is None:
            return
        self.hist_i += 1
        if self.hist_i >= len(self.history):
            self.hist_i = None
            self.set(self.draft)
        else:
            self.set(self.history[self.hist_i])

    # ---- completion

    def words_before(self) -> list[str]:
        import shlex
        start, _, _ = current_word(self.text, self.cursor)
        try:
            return shlex.split(self.text[:start])
        except ValueError:
            return self.text[:start].split()

    def candidates(self) -> tuple[int, str, list[str]]:
        start, value, _ = current_word(self.text, self.cursor)
        return start, value, self.completer(self.words_before(), value)

    def replace_word(self, start: int, word: str, final: bool) -> None:
        tail = self.text[self.cursor:]
        done = escape(word) + (" " if final and not word.endswith("/") and not tail.startswith(" ") else "")
        self.text = self.text[:start] + done + tail
        self.cursor = start + len(done)

    def complete(self) -> list[str]:
        """Tab: finish the word as far as it is unambiguous. Returns the choices when several remain."""
        start, value, found = self.candidates()
        if not found:
            return []
        if len(found) == 1:
            self.replace_word(start, found[0], final=True)
            return []
        prefix = common_prefix(found)
        if len(prefix) > len(value):
            self.replace_word(start, prefix, final=False)
        return found

    def suggestion(self) -> str:
        """Grey text to show after the cursor: an earlier command this line starts, or the only
        (or first) completion of the word being typed. Empty unless the cursor is at the end."""
        if self.cursor != len(self.text) or not self.text.strip():
            return ""
        for line in reversed(self.history):
            if line.startswith(self.text) and line != self.text:
                return line[len(self.text):]
        start, value, found = self.candidates()
        if not value or not found:
            return ""
        best = found[0] if len(found) == 1 else common_prefix(found)
        if not best.startswith(value) or len(best) <= len(value):
            return ""
        typed = self.text[start:]
        return escape(best)[len(typed):] if escape(best).startswith(typed) else ""

    def accept(self) -> None:
        extra = self.suggestion()
        if extra:
            self.insert(extra)
