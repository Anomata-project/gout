#!/usr/bin/env python3
"""Build gout's installers. One script for every system; .github/workflows/installers.yml runs it.

    python3 packaging/build.py deb     Linux: dist/gout_VERSION_all.deb; apt brings Python and ffmpeg
    python3 packaging/build.py app     macOS, Windows: dist/app/gout/, the program with its own
                                       Python (PyInstaller) and ffmpeg, ffprobe and ffplay
    python3 packaging/build.py pkg     macOS, after app: dist/gout-VERSION-macos-ARCH.pkg
    python3 packaging/build.py setup   Windows, after app: dist/gout-VERSION-windows-x64-setup.exe
                                       (needs Inno Setup's ISCC)

The installers hold gout's command line and terminal ui, the example addons and the addon guide.
The web preview (gout web) is never part of them.

The ffmpeg that goes into the macOS and Windows programs is a GPL build: its licence and where its
source is are written next to it (ffmpeg/SOURCE.txt). FFMPEG_MACOS_URL and FFMPEG_WINDOWS_URL
choose other builds.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packaging"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "gout" / "core.py").read_text(encoding="utf-8")).group(1)
IDENTIFIER = "eu.anomata.gout"
HOMEPAGE = "https://github.com/Anomata-project/gout"
MAINTAINER = "Anomata Project <admin@anomata.eu>"
NOT_SHIPPED = {"web.py", "webpage", "__pycache__"}  # the web preview stays on its server

FFMPEG_WINDOWS = os.environ.get("FFMPEG_WINDOWS_URL") or \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip"
FFMPEG_MACOS = os.environ.get("FFMPEG_MACOS_URL") or "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{tool}.zip"
TOOLS = ("ffmpeg", "ffprobe", "ffplay")


def say(text: str) -> None:
    print(f"build  {text}", flush=True)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    say("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def download(url: str, name: str) -> Path:
    cache = BUILD / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    if not target.exists():
        say(f"download {url}")
        request = urllib.request.Request(url, headers={"User-Agent": "gout-build"})
        with urllib.request.urlopen(request, timeout=300) as reply, open(target.with_suffix(".part"), "wb") as out:
            shutil.copyfileobj(reply, out)
        target.with_suffix(".part").replace(target)
    return target


def copy_source_tree(dst: Path) -> None:
    """gout as it runs from a checkout: bin/gout, the package, the examples, the guide, the licence."""
    def skip(folder, names):
        return [n for n in names if n in NOT_SHIPPED or n.endswith(".pyc")]

    (dst / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "gout", dst / "bin" / "gout")
    shutil.copytree(ROOT / "gout", dst / "gout", ignore=skip)
    shutil.copytree(ROOT / "examples" / "addons", dst / "examples" / "addons", ignore=skip)
    (dst / "docs").mkdir()
    shutil.copy2(ROOT / "docs" / "addons.md", dst / "docs" / "addons.md")
    for name in ("LICENSE", "README.md"):
        shutil.copy2(ROOT / name, dst / name)


# ---------------------------------------------------------------------------- Linux

def deb() -> Path:
    stage = fresh(BUILD / "deb")
    lib = stage / "usr" / "lib" / "gout"
    copy_source_tree(lib)
    bindir = stage / "usr" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "gout").symlink_to("../lib/gout/bin/gout")  # bin/gout finds the package through the link
    shutil.copy2(PACK / "gout-start", bindir / "gout-start")
    share = stage / "usr" / "share"
    (share / "applications").mkdir(parents=True)
    shutil.copy2(PACK / "linux" / "gout.desktop", share / "applications" / "gout.desktop")
    for size in ("scalable",):
        icons = share / "icons" / "hicolor" / size / "apps"
        icons.mkdir(parents=True)
        shutil.copy2(PACK / "gout.svg", icons / "gout.svg")
    doc = share / "doc" / "gout"
    doc.mkdir(parents=True)
    (doc / "copyright").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: gout\nSource: {HOMEPAGE}\n\n"
        "Files: *\nCopyright: 2026 Anomata Project\nLicense: GPL-3.0-or-later\n"
        " On Debian and Ubuntu the full text is in /usr/share/common-licenses/GPL-3.\n", encoding="utf-8")

    debian = stage / "DEBIAN"
    debian.mkdir()
    stage.chmod(0o755)
    for path in stage.rglob("*"):
        if path.is_symlink():
            continue
        executable = path.is_dir() or path.name in ("gout", "gout-start") and path.parent.name in ("bin",)
        path.chmod(0o755 if executable else 0o644)
    size_kib = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and not p.is_symlink()) // 1024 + 1
    (debian / "control").write_text(f"""Package: gout
Version: {VERSION}
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), ffmpeg
Installed-Size: {size_kib}
Maintainer: {MAINTAINER}
Homepage: {HOMEPAGE}
Description: command-line DAW: stack tracks on a timeline, mix and play them
 gout stacks wav and mp3 tracks on a timeline, trims and moves them, runs
 them through effect chains (eq, compressor, delay, reverb and addons) and
 mixes them to a stereo master.wav, from a command line or a terminal ui
 with waveforms, an effect panel and full-screen visuals.
""", encoding="utf-8")
    (debian / "postinst").write_text("#!/bin/sh\nset -e\npython3 -m compileall -q /usr/lib/gout/gout || true\n", encoding="utf-8")
    (debian / "prerm").write_text("#!/bin/sh\nset -e\nfind /usr/lib/gout -name __pycache__ -type d -exec rm -rf {} + || true\n", encoding="utf-8")
    for script in ("postinst", "prerm"):
        (debian / script).chmod(0o755)
    DIST.mkdir(exist_ok=True)
    out = DIST / f"gout_{VERSION}_all.deb"
    run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", stage, out])
    return out


# ---------------------------------------------------------------------------- the program (macOS, Windows)

def unzip_member(archive: Path, pattern: str, dst: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        (member,) = [m for m in z.namelist() if re.search(pattern, m)]
        dst.write_bytes(z.read(member))
    return dst


def fetch_ffmpeg(folder: Path) -> None:
    fresh(folder)
    lines = ["ffmpeg, ffprobe and ffplay, from FFmpeg (https://ffmpeg.org), licensed under the GNU GPL.",
             "gout runs them as separate programs. Their source code is at https://ffmpeg.org/download.html",
             "and in https://git.ffmpeg.org/ffmpeg.git at the version below.", ""]
    if sys.platform == "win32":
        archive = download(FFMPEG_WINDOWS, Path(FFMPEG_WINDOWS).name)
        for tool in TOOLS:
            unzip_member(archive, rf"/bin/{tool}\.exe$", folder / f"{tool}.exe")
        unzip_member(archive, r"/LICENSE(\.txt)?$", folder / "LICENSE-build.txt")
        lines.append(f"Build: {FFMPEG_WINDOWS} (build scripts: https://github.com/BtbN/FFmpeg-Builds)")
        probe = folder / "ffmpeg.exe"
    elif sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "amd64"
        for tool in TOOLS:
            url = FFMPEG_MACOS.format(arch=arch, tool=tool)
            archive = download(url, f"{tool}-macos-{arch}.zip")
            target = unzip_member(archive, rf"(^|/){tool}$", folder / tool)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            run(["codesign", "--force", "--sign", "-", target])  # Apple Silicon runs signed code only
            lines.append(f"Build of {tool}: {url} (https://ffmpeg.martin-riedl.de)")
        probe = folder / "ffmpeg"
    else:
        sys.exit("the bundled ffmpeg is for macOS and Windows; the Linux package depends on the system's")
    version = subprocess.run([probe, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.splitlines()[0]
    lines.append(f"Version: {version}")
    licence = subprocess.run([probe, "-hide_banner", "-L"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    (folder / "LICENSE.txt").write_text(licence, encoding="utf-8")  # what this build says about its own licence
    (folder / "SOURCE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(version)


def app() -> Path:
    out = DIST / "app" / "gout"
    shutil.rmtree(out, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", DIST / "app", "--workpath", BUILD / "pyinstaller", PACK / "gout.spec"])
    fetch_ffmpeg(out / "ffmpeg")
    if sys.platform == "win32":
        shutil.copy2(PACK / "windows" / "gout-start.cmd", out / "gout-start.cmd")
    else:
        shutil.copy2(PACK / "gout-start", out / "gout-start.command")
        (out / "gout-start.command").chmod(0o755)
    program = out / ("gout.exe" if sys.platform == "win32" else "gout")
    result = run([program, "version"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    say(result.stdout.strip())
    if "cannot start" in result.stdout:
        sys.exit("the terminal ui would not start in this build")
    return out


# ---------------------------------------------------------------------------- macOS

def pkg() -> Path:
    program = DIST / "app" / "gout"
    if not program.is_dir():
        sys.exit("build the program first: python3 packaging/build.py app")
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    root = fresh(BUILD / "pkgroot")
    lib = root / "usr" / "local" / "lib" / "gout"
    shutil.copytree(program, lib, symlinks=True)
    bindir = root / "usr" / "local" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "gout").write_text('#!/bin/sh\nexec /usr/local/lib/gout/gout "$@"\n', encoding="utf-8")
    (bindir / "gout").chmod(0o755)
    start = lib / "gout-start.command"
    start.write_text(start.read_text(encoding="utf-8").replace("gout version", "export PATH=\"/usr/local/bin:$PATH\"\ngout version", 1), encoding="utf-8")
    apps = root / "Applications"
    apps.mkdir()
    # an app to open from Launchpad: it opens Terminal on gout-start.command (open needs no permission)
    run(["osacompile", "-o", apps / "gout.app", "-e",
         'do shell script "open -a Terminal /usr/local/lib/gout/gout-start.command"'])
    icns = BUILD / "gout.icns"
    make_icns(icns)
    shutil.copy2(icns, apps / "gout.app" / "Contents" / "Resources" / "applet.icns")
    component = BUILD / "gout-component.pkg"
    run(["pkgbuild", "--root", root, "--identifier", IDENTIFIER, "--version", VERSION, "--install-location", "/",
         component])
    resources = fresh(BUILD / "pkgresources")
    for name in ("welcome.html", "conclusion.html"):
        shutil.copy2(PACK / "macos" / name, resources / name)
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE.txt")
    distribution = (PACK / "macos" / "distribution.xml").read_text(encoding="utf-8").replace("@VERSION@", VERSION) \
        .replace("@ARCH@", arch).replace("@IDENTIFIER@", IDENTIFIER)
    (BUILD / "distribution.xml").write_text(distribution, encoding="utf-8")
    out = DIST / f"gout-{VERSION}-macos-{arch}.pkg"
    run(["productbuild", "--distribution", BUILD / "distribution.xml", "--resources", resources,
         "--package-path", BUILD, out])
    return out


def make_icns(out: Path) -> None:
    iconset = fresh(BUILD / "gout.iconset")
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            run(["sips", "-z", px, px, PACK / "gout.png", "--out", iconset / name], capture_output=True)
    run(["iconutil", "-c", "icns", iconset, "-o", out])


# ---------------------------------------------------------------------------- Windows

def setup() -> Path:
    if not (DIST / "app" / "gout" / "gout.exe").is_file():
        sys.exit("build the program first: python packaging/build.py app")
    iscc = shutil.which("ISCC") or shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    run([iscc, f"/DVersion={VERSION}", PACK / "windows" / "gout.iss"])
    return DIST / f"gout-{VERSION}-windows-x64-setup.exe"


def main() -> None:
    steps = {"deb": deb, "app": app, "pkg": pkg, "setup": setup}
    if len(sys.argv) != 2 or sys.argv[1] not in steps:
        sys.exit(__doc__)
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and tag != f"v{VERSION}":
        sys.exit(f"the tag {tag} does not match gout's version {VERSION} (gout/core.py)")
    say(f"gout {VERSION}: {sys.argv[1]}")
    out = steps[sys.argv[1]]()
    say(f"made {out}")


if __name__ == "__main__":
    main()
