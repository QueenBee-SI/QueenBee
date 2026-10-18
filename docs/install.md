# Install QueenBee

## Windows 11

1. Download **`QueenBee-0.1.4-setup.exe`** from the [releases page](https://github.com/QueenBee-SI/QueenBee/releases).
2. Run it. It installs under your own account (no administrator rights), adds QueenBee to the Start menu and the desktop,
   and starts it. Uninstall from *Settings, Apps*.

Because the program is not code-signed yet, Windows asks twice, once each:

- The browser may say the file "is not commonly downloaded". Edge: the **...** on that line, **Keep**, **Keep anyway**.
  Chrome: **Keep**.
- The first start shows a blue *"Windows protected your PC"* screen with only a *Don't run* button. Click the small
  **More info** link under the text; a **Run anyway** button appears.

Prefer no installer? The same page has `QueenBee-0.1.4-win64.zip`. **Extract it first** (right-click, *Extract All*) and
start `QueenBee.exe` from the extracted folder. Started from inside the zip it cannot find the files next to it and shows
"Failed to load Python DLL".

## macOS

Download `QueenBee-0.1.4-macos.zip` from the same page, unzip, drag `QueenBee.app` to Applications. The first open is
refused because the app is not notarized: right-click it, choose **Open**, confirm (on newer macOS: System Settings,
Privacy & Security, **Open Anyway**).

## Linux

Download `QueenBee-0.1.4-linux-x86_64.tar.gz` from the same page, unpack it (`tar -xzf`) and start `QueenBee/QueenBee`.
It is built and started on GitHub's Ubuntu machines; its own window needs a web engine the system provides (see the Linux
note below), and without one it opens the page in your browser. Running from source is the better-trodden path on Linux.

## From source (Windows, macOS, Linux)

Python 3.11 or newer and Git:

```
git clone https://github.com/QueenBee-SI/QueenBee
cd QueenBee
powershell -ExecutionPolicy Bypass -File scripts\install.ps1      # Windows
bash scripts/install.sh                                           # macOS, Linux
```

This makes a private `.venv` next to the code and puts a QueenBee shortcut on the desktop (Windows: also the Start menu;
macOS: a `QueenBee.command`; Linux: a desktop entry).

**Linux.** `install.sh` uses the newest Python 3.11+ it finds (`python3.13`, `python3.12`, `python3.11`, then `python3`).
Ubuntu 22.04's own `python3` is 3.10: without sudo, `curl -LsSf https://astral.sh/uv/install.sh | sh` and
`uv python install 3.12` put a `python3.12` in `~/.local/bin`; with sudo, the deadsnakes PPA has `python3.12`. QueenBee's
own window needs a web engine the system provides; without one QueenBee opens in your browser instead and says how to get
the window (Qt: `sudo apt install libnss3 libxkbcommon-x11-0 libxcb-cursor0` then `.venv/bin/pip install 'pywebview[qt]'`).
Tried on Ubuntu 22.04 under WSL: the install, the page in the browser and the test suite; the window itself and a real
job with signed-in agents have not been tried on Linux yet.

## What you need besides

**Claude Code or Codex, installed and signed in.** QueenBee has no account of its own: it starts the agents as you, so the
sign-in you did once in a terminal is the one they use. If you have neither yet, the *Agents on this computer* card in
the window offers each vendor's own install and sign-in command and shows it to you before it runs.

Nothing is installed system-wide. QueenBee keeps its own files in `%LOCALAPPDATA%\queenbee` (saved queens, logs) and puts
demo projects in `~\QueenBeeDemo`. To remove everything, uninstall (or delete the extracted folder) and delete those two
folders.

## Is the file safe?

The warnings above are about the missing signature, not about anything found in the file. Three ways to check:

- *It is what GitHub built.* Every file on the releases page is built from this repository's code by GitHub Actions
  ([`build.yml`](../.github/workflows/build.yml)), on GitHub's machines, from the tagged commit; no file is uploaded from
  anyone's computer. Releases made while this repository is public also carry GitHub's signed record of that, checked
  with `gh attestation verify <file> --repo QueenBee-SI/QueenBee`.
- *It was not changed on the way.* `SHA256SUMS-windows.txt`, `SHA256SUMS-macos.txt` and `SHA256SUMS-linux.txt` on the same
  page list each file's checksum; compare with `Get-FileHash QueenBee-0.1.4-setup.exe` (PowerShell), `shasum -a 256`
  (macOS) or `sha256sum` (Linux).
- *It does nothing hidden.* The code is all here. QueenBee needs no administrator rights, keeps its own files in the
  folders named above, and listens only on `127.0.0.1` (this computer, never the network). It sends nothing anywhere
  itself: the agents it starts talk to their own vendors as they always do and work in the project folder you give them,
  and an **Install** button runs that vendor's own installer only after showing you the command.

## The first five minutes

Click **Try a demo**. QueenBee copies a small project, a notebook program in three parts, to `~\QueenBeeDemo`, creates a
queen and three workers and starts them. The task deliberately leaves one thing open (what a note looks like inside), so
you can watch the workers settle it on the hive board and the queen run the whole project's tests at the end. A few
minutes, on your own account: Claude Code (Haiku workers, a Sonnet queen) if you have it, otherwise Codex for the workers
and the queen alike. What to look at while it runs: [`free-demo.md`](free-demo.md). Every button, in order:
[`user-guide.md`](user-guide.md).

## Trouble?

| What you see | What to do |
|---|---|
| The browser says the file "is not commonly downloaded" | Edge: the **...** on the download line, **Keep**, **Keep anyway**. Chrome: **Keep**. The file is not signed yet. |
| "Windows protected your PC", and only a *Don't run* button | Click the small **More info** link; **Run anyway** appears. |
| "Failed to load Python DLL" or "This app may depend on other compressed files" | You started the exe from inside a zip. Use the installer, or extract the zip first and start the exe from the extracted folder. |
| "Claude Code is not installed" or "not signed in" | **Install** / **Sign in** in *Agents on this computer*, or run `claude` in a terminal once and log in. |
| The window stays blank or does not open | Run `QueenBee.exe --no-window` in a terminal and open the printed address in a browser; the message is in the terminal. |
| A worker asks me a question anyway | The rule did not settle it (outside the folder, or a choice about the work). Answer in its terminal; the rule is `QUEEN.md` in your project if you want to extend it. |
| Nothing opens, or no second window | A QueenBee is already running and was brought to the front. `QueenBee.exe --stop-all`, then start again. |
| The computer slows down | Each worker is a real agent in a real terminal. Set **Workers at most** in the queen's *Limits* to 2 or 3. |
| I want everything to stop | `QueenBee.exe --stop-all`. |

If something was hard to set up, open an issue and say which step. That is the most useful report of all.
