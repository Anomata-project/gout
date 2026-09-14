Installers for gout, a command-line DAW. Pick the one for your computer.

gout is free and not signed with a paid certificate, so Windows and macOS ask you once whether
you trust it. The steps below get you past that.

## Windows 10 and 11

1. Download `gout-…-windows-x64-setup.exe` and open it.
2. If Windows says "Windows protected your PC", click **More info**, then **Run anyway**.
3. Click through the installer. It installs for you only and needs no administrator.
4. Open **gout** from the Start menu. A terminal opens in your `gout` folder with the first commands.

## macOS

Apple silicon (M1 and later): `gout-…-macos-arm64.pkg`. Intel: `gout-…-macos-x86_64.pkg`.

1. Download the pkg and open it.
2. macOS says it cannot verify the developer: click **Done**.
3. Open **System Settings → Privacy & Security**, scroll down to the message about gout and click
   **Open Anyway**, then **Open**, and give your password.
4. Click through the installer. Open **gout** from Launchpad, or type `gout` in Terminal.

## Linux (Ubuntu, Debian, Mint, Pop!_OS)

1. Download `gout_…_all.deb`.
2. Open it (Ubuntu's App Center installs it), or run `sudo apt install ./gout_…_all.deb` in its folder.
   apt brings Python and ffmpeg along.
3. Open **gout** from your applications, or type `gout` in a terminal.

Other Linux: run gout from its source (see the README): it needs Python 3.9 or newer and ffmpeg.

## Then

    gout new song && cd song
    gout add ~/Music/drums.wav
    gout

`gout help` lists everything; `gout addons examples` installs the example addons, and
`docs/addons.md` shows how to write your own.

gout is free software under the GNU GPL, version 3 or later. The Windows and macOS installers
carry ffmpeg (GPL), with its licence and a note on its source next to it.
