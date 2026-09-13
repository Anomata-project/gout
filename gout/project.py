"""The project store: sqlite schema, tracks, caches, undo history, the gout.json sidecar."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

from .core import __version__, DB_NAME, DEFAULT_RATE, die, GoutError, MASTER_OWNER, MASTER_WAV, SIDECAR, TRACK_DIR
from .media import compute_envelope, compute_spectrum, ENV_RATE, measure_loudness, probe


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    n           INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    file        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    length_ms   INTEGER NOT NULL,
    channels    INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    offset_ms   INTEGER NOT NULL DEFAULT 0,
    in_ms       INTEGER NOT NULL DEFAULT 0,
    out_ms      INTEGER,
    gain_db     REAL NOT NULL DEFAULT 0,
    pan         REAL NOT NULL DEFAULT 0,
    mute        INTEGER NOT NULL DEFAULT 0,
    solo        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fx (
    id      INTEGER PRIMARY KEY,
    owner   TEXT NOT NULL,              -- the track's file name, or @master
    pos     INTEGER NOT NULL,           -- 1, 2, ... in the order the audio goes through them
    kind    TEXT NOT NULL,              -- the effect's name: eq, comp, or an addon's
    params  TEXT NOT NULL DEFAULT '',   -- its canonical settings line
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS envelopes (
    file  TEXT PRIMARY KEY,
    size  INTEGER NOT NULL,
    mtime REAL NOT NULL,
    rate  INTEGER NOT NULL,
    peaks BLOB NOT NULL,
    lufs  REAL,
    tp    REAL,
    lra   REAL,
    spectrum BLOB,
    wave  BLOB
);
CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    command  TEXT NOT NULL,
    undoable INTEGER NOT NULL DEFAULT 1,
    snapshot TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT '[]'
);
"""


TRACK_COLUMNS = ("n", "name", "file", "kind", "length_ms", "channels", "sample_rate",
                 "offset_ms", "in_ms", "out_ms", "gain_db", "pan", "mute", "solo")


# before the effect chain, a track had one column per effect and the master one setting each
LEGACY_FX = ("eq", "comp", "delay", "reverb")


def legacy_chain(item: dict) -> list[dict]:
    """A chain from the old per-effect fields (eq, eq_on, comp, ...) of a track or settings dict."""
    chain = []
    for kind in LEGACY_FX:
        text = item.get(kind)
        if text:
            chain.append({"kind": kind, "params": str(text), "on": bool(int(item.get(f"{kind}_on", 1) or 0))})
    return chain


class Project:
    """A project directory: gout.db, master/ with the track files, master.wav."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.db_path = self.root / DB_NAME
        self.tracks_dir = self.root / TRACK_DIR
        self.master = self.root / MASTER_WAV
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if "created" not in {r[1] for r in self.conn.execute("PRAGMA table_info(history)")}:
            with self.conn:  # databases from before the column existed
                self.conn.execute("ALTER TABLE history ADD COLUMN created TEXT NOT NULL DEFAULT '[]'")
        self.migrate_effects()
        if "lufs" not in {r[1] for r in self.conn.execute("PRAGMA table_info(envelopes)")}:
            with self.conn:
                for col in ("lufs", "tp", "lra"):
                    self.conn.execute(f"ALTER TABLE envelopes ADD COLUMN {col} REAL")
        if "spectrum" not in {r[1] for r in self.conn.execute("PRAGMA table_info(envelopes)")}:
            with self.conn:
                self.conn.execute("ALTER TABLE envelopes ADD COLUMN spectrum BLOB")
        if "wave" not in {r[1] for r in self.conn.execute("PRAGMA table_info(envelopes)")}:
            with self.conn:
                self.conn.execute("ALTER TABLE envelopes ADD COLUMN wave BLOB")
        old = self.get("automix")  # the setting was called automix before 2.0.0 final
        if old is not None:
            if self.get("autorender") is None:
                self.set("autorender", old)
            self.unset("automix")

    def migrate_effects(self) -> None:
        """Move effects from the old per-effect columns and master settings into the chain table."""
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(tracks)")}
        old_keys = [k for k in LEGACY_FX if self.get(k) is not None]
        if not (columns & set(LEGACY_FX)) and not old_keys:
            return
        with self.conn:
            if columns & set(LEGACY_FX):
                for row in self.conn.execute("SELECT * FROM tracks ORDER BY n").fetchall():
                    for pos, item in enumerate(legacy_chain(dict(row)), 1):
                        self.conn.execute("INSERT INTO fx (owner, pos, kind, params, enabled) VALUES (?, ?, ?, ?, ?)",
                                          (row["file"], pos, item["kind"], item["params"], int(item["on"])))
                for col in LEGACY_FX:
                    for name in (col, f"{col}_on"):
                        if name in columns:
                            self.conn.execute(f"ALTER TABLE tracks DROP COLUMN {name}")
            settings = {k: self.conn.execute("SELECT value FROM project WHERE key = ?", (k,)).fetchone()
                        for k in LEGACY_FX}
            start = len(self.chain(MASTER_OWNER))
            for pos, item in enumerate(legacy_chain({k: (v["value"] if v else "") for k, v in settings.items()}),
                                       start + 1):
                self.conn.execute("INSERT INTO fx (owner, pos, kind, params, enabled) VALUES (?, ?, ?, ?, ?)",
                                  (MASTER_OWNER, pos, item["kind"], item["params"], 1))
            for k in LEGACY_FX:
                self.conn.execute("DELETE FROM project WHERE key = ?", (k,))

    # ---- locating

    @classmethod
    def find(cls, start: Path | None = None) -> "Project | None":
        here = (start or Path.cwd()).resolve()
        for candidate in (here, *here.parents):
            if (candidate / DB_NAME).is_file():
                return cls(candidate)
        return None

    @classmethod
    def create(cls, root: Path, rate: int) -> "Project":
        root.mkdir(parents=True, exist_ok=True)
        (root / TRACK_DIR).mkdir(exist_ok=True)
        project = cls(root)
        project.set("name", root.resolve().name)
        project.set("rate", str(rate))
        project.set("autorender", "on")
        project.set("created", dt.datetime.now().isoformat(timespec="seconds"))
        return project

    # ---- settings

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM project WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO project (key, value) VALUES (?, ?)", (key, value))

    def unset(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM project WHERE key = ?", (key,))

    @property
    def rate(self) -> int:
        return int(self.get("rate") or DEFAULT_RATE)

    @property
    def autorender(self) -> bool:
        return (self.get("autorender") or "on") != "off"

    # ---- tracks

    def tracks(self) -> list[dict]:
        """Every track as a dict, with its effect chain under "fx" and its chain owner."""
        chains = self.chains()
        out = []
        for r in self.conn.execute("SELECT * FROM tracks ORDER BY n").fetchall():
            t = dict(r)
            t["owner"] = t["file"]
            t["fx"] = chains.get(t["file"], [])
            out.append(t)
        return out

    def track(self, spec: str) -> dict:
        tracks = self.tracks()
        if not tracks:
            die("the project has no tracks yet — gout add FILE")
        if spec.isdigit():
            for t in tracks:
                if t["n"] == int(spec):
                    return t
            die(f"no track {spec} (there are {len(tracks)})")
        exact = [t for t in tracks if t["name"] == spec]
        if exact:
            return exact[0]
        prefix = [t for t in tracks if t["name"].startswith(spec)]
        if len(prefix) == 1:
            return prefix[0]
        if prefix:
            die(f"'{spec}' matches several tracks: " + ", ".join(t["name"] for t in prefix))
        die(f"no track named '{spec}'")

    def update(self, n: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE tracks SET {cols} WHERE n = ?", (*fields.values(), n))

    def insert(self, **fields) -> dict:
        fields.setdefault("n", (self.conn.execute("SELECT MAX(n) FROM tracks").fetchone()[0] or 0) + 1)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn:
            self.conn.execute(f"INSERT INTO tracks ({cols}) VALUES ({marks})", tuple(fields.values()))
        return self.track(str(fields["n"]))

    def delete(self, n: int) -> None:
        with self.conn:
            row = self.conn.execute("SELECT file FROM tracks WHERE n = ?", (n,)).fetchone()
            if row:
                self.conn.execute("DELETE FROM fx WHERE owner = ?", (row["file"],))
            self.conn.execute("DELETE FROM tracks WHERE n = ?", (n,))
            rows = self.conn.execute("SELECT n FROM tracks ORDER BY n").fetchall()
            for new, row in enumerate(rows, 1):  # keep numbering contiguous
                if row["n"] != new:
                    self.conn.execute("UPDATE tracks SET n = ? WHERE n = ?", (new, row["n"]))

    def unique_name(self, wanted: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", wanted).strip("-.") or "track"
        taken = {t["name"] for t in self.tracks()}
        taken |= {p.stem for p in self.tracks_dir.iterdir()} if self.tracks_dir.is_dir() else set()
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        return name

    # ---- effect chains

    def chains(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self.conn.execute("SELECT * FROM fx ORDER BY owner, pos, id"):
            out.setdefault(r["owner"], []).append(
                {"id": r["id"], "kind": r["kind"], "params": r["params"], "on": bool(r["enabled"])})
        return out

    def chain(self, owner: str) -> list[dict]:
        return self.chains().get(owner, [])

    def fx_insert(self, owner: str, kind: str, params: str, pos: int | None = None, on: bool = True) -> int:
        """Put an effect into a chain at position pos (1-based; the end when None). Returns its id."""
        with self.conn:
            size = self.conn.execute("SELECT COUNT(*) FROM fx WHERE owner = ?", (owner,)).fetchone()[0]
            pos = size + 1 if pos is None else max(1, min(pos, size + 1))
            self.conn.execute("UPDATE fx SET pos = pos + 1 WHERE owner = ? AND pos >= ?", (owner, pos))
            cur = self.conn.execute("INSERT INTO fx (owner, pos, kind, params, enabled) VALUES (?, ?, ?, ?, ?)",
                                    (owner, pos, kind, params, int(on)))
            return cur.lastrowid

    def fx_get(self, fid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM fx WHERE id = ?", (fid,)).fetchone()
        return None if r is None else {"id": r["id"], "owner": r["owner"], "pos": r["pos"], "kind": r["kind"],
                                       "params": r["params"], "on": bool(r["enabled"])}

    def fx_set(self, fid: int, params: str | None = None, on: bool | None = None) -> None:
        with self.conn:
            if params is not None:
                self.conn.execute("UPDATE fx SET params = ? WHERE id = ?", (params, fid))
            if on is not None:
                self.conn.execute("UPDATE fx SET enabled = ? WHERE id = ?", (int(on), fid))

    def fx_remove(self, fid: int) -> None:
        item = self.fx_get(fid)
        if item is None:
            return
        with self.conn:
            self.conn.execute("DELETE FROM fx WHERE id = ?", (fid,))
            self._renumber(item["owner"])

    def fx_move(self, fid: int, pos: int) -> None:
        item = self.fx_get(fid)
        if item is None:
            return
        ids = [i["id"] for i in self.chain(item["owner"]) if i["id"] != fid]
        ids.insert(max(0, min(pos - 1, len(ids))), fid)
        with self.conn:
            for new, i in enumerate(ids, 1):
                self.conn.execute("UPDATE fx SET pos = ? WHERE id = ?", (new, i))

    def fx_clear(self, owner: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM fx WHERE owner = ?", (owner,))

    def _renumber(self, owner: str) -> None:
        rows = self.conn.execute("SELECT id FROM fx WHERE owner = ? ORDER BY pos, id", (owner,)).fetchall()
        for new, r in enumerate(rows, 1):
            self.conn.execute("UPDATE fx SET pos = ? WHERE id = ?", (new, r["id"]))

    # ---- envelopes

    def envelope(self, name: str, path: Path) -> bytes:
        """Cached peaks for a file in master/ (or master.wav); recomputed when the file changed."""
        try:
            st = path.stat()
        except OSError:
            return b""
        row = self.conn.execute("SELECT size, mtime, rate, peaks, wave FROM envelopes WHERE file = ?",
                                (name,)).fetchone()
        if (row and row["size"] == st.st_size and row["mtime"] == st.st_mtime and row["rate"] == ENV_RATE
                and row["wave"] is not None):
            return row["peaks"]
        peaks, wave = compute_envelope(path)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO envelopes (file, size, mtime, rate, peaks, wave)"
                              " VALUES (?, ?, ?, ?, ?, ?)", (name, st.st_size, st.st_mtime, ENV_RATE, peaks, wave))
        return peaks

    def wave(self, name: str, path: Path) -> bytes:
        """Cached highest and lowest point per 20 ms (two bytes a window) of a file."""
        if not self.envelope(name, path):
            return b""
        row = self.conn.execute("SELECT wave FROM envelopes WHERE file = ?", (name,)).fetchone()
        return (row["wave"] if row else None) or b""

    def loudness(self, name: str, path: Path) -> dict | None:
        """Cached integrated loudness, true peak and LRA of a file; measured on first use."""
        self.envelope(name, path)  # makes sure the row exists and is fresh
        row = self.conn.execute("SELECT lufs, tp, lra FROM envelopes WHERE file = ?", (name,)).fetchone()
        if row is None:
            return None
        if row["lufs"] is not None:
            return {"i": row["lufs"], "tp": row["tp"], "lra": row["lra"]}
        m = measure_loudness(path)
        if m is None:
            return None
        with self.conn:
            self.conn.execute("UPDATE envelopes SET lufs = ?, tp = ?, lra = ? WHERE file = ?",
                              (m["i"], m["tp"], m["lra"], name))
        return m

    def spectrum(self, name: str, path: Path) -> bytes:
        """Cached average spectrum of a file; computed on first use, dropped when the file changes."""
        self.envelope(name, path)
        row = self.conn.execute("SELECT spectrum FROM envelopes WHERE file = ?", (name,)).fetchone()
        if row is None:
            return b""
        if row["spectrum"] is not None:
            return row["spectrum"]
        try:
            duration = probe(path)["duration"]
        except GoutError:
            return b""
        spec = compute_spectrum(path, duration)
        with self.conn:
            self.conn.execute("UPDATE envelopes SET spectrum = ? WHERE file = ?", (spec, name))
        return spec

    def forget_envelope(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM envelopes WHERE file = ?", (name,))

    # ---- history

    def snapshot(self) -> dict:
        settings = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM project")
                    if not r["key"].startswith("ui_")}  # ui_ keys are preferences, not state
        return {"project": settings, "master": {"fx": self.chain(MASTER_OWNER)}, "tracks": self.tracks()}

    def state_fingerprint(self) -> str:
        """A hash of everything that decides how master.wav sounds: settings, tracks, effect
        chains and the track files themselves. mix stores it, play compares it."""
        snap = self.snapshot()
        snap["project"] = {k: v for k, v in snap["project"].items() if not k.startswith("master_")}
        for t in snap["tracks"]:
            try:
                st = (self.tracks_dir / t["file"]).stat()
                t["file_stat"] = [st.st_size, st.st_mtime]
            except OSError:
                t["file_stat"] = None
        for items in [t["fx"] for t in snap["tracks"]] + [snap["master"]["fx"]]:
            for item in items:
                item.pop("id", None)
        return hashlib.sha1(json.dumps(snap, sort_keys=True).encode()).hexdigest()

    def master_is_current(self) -> bool:
        return self.master.exists() and self.get("master_state") == self.state_fingerprint()

    def record(self, command: str, undoable: bool = True) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO history (ts, command, undoable, snapshot) VALUES (?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), command, int(undoable),
                 json.dumps(self.snapshot())),
            )

    def created(self, files: list[str]) -> None:
        """Note files the command being recorded copied into master/ (undo may delete them)."""
        if files:
            with self.conn:
                self.conn.execute("UPDATE history SET created = ? WHERE id = (SELECT MAX(id) FROM history)",
                                  (json.dumps(files),))

    def document(self) -> dict:
        """The state as gout.json holds it: the snapshot without database ids."""
        snap = self.snapshot()

        def clean(items: list[dict]) -> list[dict]:
            return [{"kind": i["kind"], "params": i["params"], "on": i["on"]} for i in items]

        snap["master"]["fx"] = clean(snap["master"]["fx"])
        for t in snap["tracks"]:
            t.pop("owner", None)
            t["fx"] = clean(t["fx"])
        return {"gout": __version__, **snap}

    def sync_json(self) -> None:
        """Write gout.json next to the database whenever the state it describes changed."""
        text = json.dumps(self.document(), indent=2) + "\n"
        path = self.root / SIDECAR
        try:
            if path.exists() and path.read_text() == text:
                return
            tmp = path.with_name(SIDECAR + ".part")
            tmp.write_text(text)
            tmp.replace(path)
        except OSError as exc:
            print(f"      could not write {SIDECAR}: {exc}", file=sys.stderr)

    def reorder(self, files: list[str]) -> None:
        """Number the tracks so those in `files` come first, in that order."""
        tracks = self.tracks()
        ranked = sorted(tracks, key=lambda t: (files.index(t["file"]) if t["file"] in files
                                               else len(files) + t["n"]))
        with self.conn:
            self.conn.execute("UPDATE tracks SET n = -n")
            for new, t in enumerate(ranked, 1):
                self.conn.execute("UPDATE tracks SET n = ? WHERE n = ?", (new, -t["n"]))

    def restore(self, snap: dict) -> None:
        """Put back a snapshot, including ones taken before effects became a chain."""
        master = (snap.get("master") or {}).get("fx")
        if master is None:
            master = legacy_chain(snap["project"])
        with self.conn:
            self.conn.execute("DELETE FROM tracks")
            self.conn.execute("DELETE FROM fx")
            for t in snap["tracks"]:
                cols = [c for c in TRACK_COLUMNS if c in t]
                self.conn.execute(
                    f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple(t[c] for c in cols),
                )
            owners = [(t["file"], t["fx"] if "fx" in t else legacy_chain(t)) for t in snap["tracks"]]
            for owner, items in owners + [(MASTER_OWNER, master)]:
                for pos, item in enumerate(items, 1):
                    self.conn.execute("INSERT INTO fx (id, owner, pos, kind, params, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                                      (item.get("id"), owner, pos, item["kind"], item["params"], int(item["on"])))
            self.conn.execute("DELETE FROM project WHERE key NOT LIKE 'ui_%'")
            for k, v in snap["project"].items():
                if k not in LEGACY_FX:
                    self.conn.execute("INSERT INTO project (key, value) VALUES (?, ?)", (k, v))

    def undo(self) -> str:
        row = self.conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            die("nothing to undo")
        if not row["undoable"]:
            die(f"cannot undo '{row['command']}': it rewrote or deleted an audio file")
        snap = json.loads(row["snapshot"])
        self.restore(snap)
        still_used = {t["file"] for t in snap["tracks"]}
        for name in json.loads(row["created"] or "[]"):  # only copies gout made, never the user's files
            path = self.tracks_dir / name
            if name not in still_used and path.exists():
                path.unlink()
        with self.conn:
            self.conn.execute("DELETE FROM history WHERE id = ?", (row["id"],))
        return row["command"]
