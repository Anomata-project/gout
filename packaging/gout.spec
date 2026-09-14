# PyInstaller: gout as one folder, the program next to its own Python (packaging/build.py app).
# The whole standard library goes in, not only what gout imports, so addons can use any of it.
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
LEAVE_OUT = {"tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv",
             "pydoc_data", "distutils", "antigravity", "this"}

stdlib = []
for name in sorted(sys.stdlib_module_names):
    if name in LEAVE_OUT or name.startswith("_test"):
        continue
    try:
        if importlib.util.find_spec(name) is None:  # not on this system: winreg on macOS, termios on Windows
            continue
    except (ImportError, ValueError):
        continue
    stdlib.append(name)
    stdlib += collect_submodules(name, filter=lambda mod: ".test" not in mod and "idle_test" not in mod)

analysis = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "examples" / "addons"), "examples/addons"), (str(ROOT / "docs" / "addons.md"), "docs"),
           (str(ROOT / "LICENSE"), ".")],
    hiddenimports=sorted(set(stdlib)) + collect_submodules("gout", filter=lambda mod: not mod.startswith("gout.web")),
    excludes=sorted(LEAVE_OUT) + ["gout.web"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="gout", console=True, upx=False,
          icon=str(ROOT / "packaging" / "windows" / "gout.ico") if sys.platform == "win32" else None)
COLLECT(exe, analysis.binaries, analysis.datas, name="gout", upx=False)
