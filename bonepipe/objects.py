"""The objects: what was measured on each, by whom, how, and how sure.

An object is one JSON file in bonepipe/data/. Nothing in it is a guess of ours: every number has
the source it was read in, the page, the method (calipers, ct, estimate ...) and a confidence.
The same thing measured twice is there twice, and whoever needs one value says which.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
METHODS = ("calipers", "ct", "estimate", "slices")
CONFIDENCE = ("high", "medium", "low", "contested")


class DataError(ValueError):
    """Something in a data file that does not hold together."""


def sources() -> dict:
    return json.loads((DATA / "sources.json").read_text(encoding="utf-8"))


def object_ids() -> list[str]:
    return sorted(p.stem for p in DATA.glob("*.json") if p.name != "sources.json" and not p.name.endswith(".slices.json"))


class Thing:
    """One object, as its file has it."""

    def __init__(self, data: dict):
        self.data = data
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.measurements: list[dict] = data.get("measurements", [])
        self._slices: dict | None = None

    def all(self, what: str) -> list[dict]:
        return [m for m in self.measurements if m["what"] == what]

    def value(self, what: str, method: str | None = None):
        """One measurement's value; with several, say which method. DataError when there is none
        or the choice is not clear."""
        found = [m for m in self.all(what) if method is None or m["method"] == method]
        if not found:
            raise DataError(f"{self.id} has no {what}" + (f" by {method}" if method else ""))
        if len(found) > 1:
            raise DataError(f"{self.id} has {len(found)} values for {what}: say which method ({', '.join(m['method'] for m in found)})")
        return found[0]["value"]

    def span(self, what: str) -> tuple[float, float]:
        """The least and the most that was measured for something, over every method and source."""
        values = []
        for m in self.all(what):
            values += m["value"] if isinstance(m["value"], list) else [m["value"]]
        if not values:
            raise DataError(f"{self.id} has no {what}")
        return min(values), max(values)

    def slices(self) -> dict | None:
        """The slice measurements that go with the object, when it has any."""
        name = self.data.get("slices")
        if name and self._slices is None:
            self._slices = json.loads((DATA / name).read_text(encoding="utf-8"))
        return self._slices


def load(name: str) -> Thing:
    path = DATA / f"{name}.json"
    if not path.is_file():
        raise DataError(f"no object called {name!r}: there is {', '.join(object_ids())}")
    return Thing(json.loads(path.read_text(encoding="utf-8")))


def problems(thing: Thing) -> list[str]:
    """What is wrong with an object's file: a measurement without a source, a source nobody listed."""
    known = sources()
    out = []
    for m in thing.measurements:
        where = f"{thing.id}: {m.get('what')}"
        for key in ("what", "value", "unit", "method", "source", "page", "confidence"):
            if m.get(key) in (None, ""):
                out.append(f"{where} has no {key}")
        if m.get("method") not in METHODS:
            out.append(f"{where}: method {m.get('method')!r}")
        if m.get("confidence") not in CONFIDENCE:
            out.append(f"{where}: confidence {m.get('confidence')!r}")
        if m.get("source") not in known:
            out.append(f"{where}: source {m.get('source')!r} is not in sources.json")
        elif not known[m["source"]].get("read"):
            out.append(f"{where}: {m['source']} was not read, so no number may come from it")
    for item in [thing.data.get("site"), thing.data.get("date"), *thing.data.get("facts", []), *thing.data.get("blown", [])]:
        if item and item.get("source") not in known:
            out.append(f"{thing.id}: source {item.get('source')!r} is not in sources.json")
    return out
