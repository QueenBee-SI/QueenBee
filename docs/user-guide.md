# QueenBee: user guide

One page. If something here does not match what you see, the program is right and this
page is out of date.

## Who it is for

Anyone who runs AI coding agents on work with several parts. A GPU is not needed; the *GPU*
tab and the memory-aware scheduling only matter if you also train models.

## The idea in one minute

You are the boss of a **queen bee**: an AI model (Sonnet 5.5 by default) that manages a hive of
**worker bees**, each an AI coding agent in its own terminal. You add the workers and tell each its
task, or hand the queen a whole job and she splits it. Every couple of minutes she looks in: she
reads what each worker is doing and the project files, and speaks to a worker when a word will help,
for example when two workers decided the same thing differently. When a worker stops to ask "may I
run this?", she answers for you if the answer is clearly safe, and says no if it is not. When a
worker says "done", she checks it before she believes it.

## Open it and make your first queen

1. Open **QueenBee** (desktop shortcut). The top bar shows your CPU, memory and GPU.
   New here? Click **▶ Try the example demo** and read [`free-demo.md`](free-demo.md).
2. Click **Create a queen bee**. Fill in:
   - **Name**, and optionally **what she is for**: shown on her box on the map.
   - **The queen's job:** *Director* (you add the workers; she looks in on them) or *Planner* (she also
     splits your instruction into workers first; then you also give an instruction and a folder).
   - **Queen's model:** *Sonnet 5.5* (recommended), *Opus 5.5* (the strongest, about twice the cost per
     token) or *Codex* (if installed). Change it later in her panel.
   - **Limits** (optional): workers at most, tokens (millions), RAM, CPU, VRAM. Her look-ins count against
     the token budget.
3. Click the queen, then **+ Worker bee**: a **task**, the **folder**, a short **name**, the worker's
   **agent and model** (or **Let the queen advise**: easy parts get a light model, ordinary parts
   Sonnet, the hardest Opus), and **Done when this passes**, a command the queen runs herself.

## Chat with the queen

The *Chat with the queen* card in her panel starts her as an agent in a terminal of her own, in the project
folder. She reads `QUEEN.md` (your rules for her) and the README, asks what is still open (what "done" means,
what she may approve for you, limits, which agents), says how she would split the work, and when you agree
writes `.queenbee/PLAN.md` (the instruction she will plan from) and `.queenbee/CONTEXT.md` (a short brief every
worker reads first, so none of them explores the project again). **Start with this plan** appears under the
chat; press it and she plans and starts the workers. She may read the project but not change it: every write
she tries asks you in her terminal. While the team works she can answer "how is it going" from the hive.

## Beekeeper

The one who manages is checked too. The *Beekeeper* card runs a separate agent (any installed one, any model)
that reads `QUEEN.md`, the README, the task and the plan, and scores how closely the queen kept to them, with
what she missed listed first. Add extra rule files if you keep them elsewhere; **Check now** runs it at once,
and it runs by itself every few minutes while a team works. The result is kept in `.queenbee/BEEKEEPER.md`.

## Planner mode

Open *Planner mode* in the queen's panel to hand her a whole job. Next to **Plan and start workers**
there is a selector:

| Choice | Use it when |
|---|---|
| **Multi-agent** | The job has several independent parts. The queen splits it. |
| **Single agent** | One focused change. One strong worker does all of it, no planning cost. |
| **Queen decides** | You are not sure; she judges the size herself (she can be wrong). |

## Reading the screen

- **Orange box at the top: the queen.** Her colour and the words next to it say what she is
  doing (*directing*, *looking in*, *working*, *waiting*). Her running cost is shown under her name.
- **Hexagons: worker bees.** Green mark = done, orange outline = working, red = needs you.
  A worker shown as *verifying* has said it is done and the queen is running her checks.
- **Left list:** all your queens. Old ones stay until you remove them.
- **Cluster labels** (small pills such as *core* or *tests*, in your language): the queen groups workers
  of one part of the job under a name. They are only labels.
- **Tabs:** **Hive** (the map), **Report** (the queen's completion report, a timeline of when each worker ran, and
  every file the workers changed, with the diff), **Decisions** (every question the queen answered for a worker, and the
  few she left to you). **GPU** appears while a GPU job runs.

## Talking to workers and to the queen

- **Click a worker:** its live terminal opens. Type to change what it is doing.
- **Click the queen:** her event stream, the **Limits** row, and the **Hive board**.
- **Hive board:** the shared noticeboard of the hive. Type a note, choose *all workers* or one worker, press **Say**. Workers read
  it between steps. Workers also post to the board themselves when they find something the
  others need (for example "the date format is ISO, not DD/MM"); the queen passes it on.
- **Hover the queen** → **+ Worker bee** adds a worker yourself. **Hover a worker** → **✕ Stop**
  stops it, **⑂ Queen** lets it plan a sub-team of its own for one part.

## Who answers the workers' prompts

Each queen's panel has **Worker prompts**: **The queen answers** (the default, below) or **I answer**. With *I answer*
the workers start with their agent's own prompts on (a Claude Code worker asks you before every edit and command, a
Codex worker when it needs to leave its sandbox), the prompt waits for you in the worker's terminal, and the worker shows
*needs you* on the map; the queen only watches: she keeps the hive board, nudges a worker that stopped
half way, and runs the delivery check at the end. Claude Code workers already running follow a change at once; Codex
and the other agents follow it from the next worker that starts.

What each choice changes, agent by agent (the flags are the ones QueenBee starts the worker with):

| Agent | The queen answers | I answer |
|---|---|---|
| **Claude Code** | `--permission-mode acceptEdits`. Pre-approved by Claude Code itself: reading, searching, editing and writing in the folder, and `python`, `ls`, `cat`, `mkdir`, `grep` (and their PowerShell twins). Refused outright: web fetch and search, `rm`, `del`, `curl`, `wget`, `git push`, `pip install`, `Remove-Item`, `Invoke-WebRequest`. Every other call goes through the hook to the queen, who says yes or no by her rule. | `--permission-mode default`, no pre-approved and no refused list. Reading needs no answer; every edit and every command shows Claude Code's own prompt in the worker's terminal. The hook stays and hands each call to you, so the queen can take over again at once. |
| **Codex** | `-s workspace-write -a never`. It never asks: Codex's sandbox lets it write inside the folder and blocks writing outside it (and, by default, the network); a blocked action comes back to it as an error. The queen sees none of these calls. | `-s workspace-write -a on-request`. The same sandbox; Codex asks you when it judges a command needs approval, typically one that would leave the sandbox (write outside the folder, use the network). Commands it runs inside the sandbox do not ask. |
| **Gemini CLI, Kimi, OpenCode, Cursor Agent, Mistral Vibe, Grok Build** | Started with their own default prompts on; no hook. The queen answers a prompt she reads on the worker's screen only for a worker started from a prepared work order (*Prepare*, then *Start*); otherwise the prompt waits for you. | The same start; the queen answers none of them. |

A change made while workers run: a Claude Code worker follows it at once, because the hook answers "ask" out loud and
that makes Claude Code prompt even for what its start flags pre-approved (checked on Claude Code 2.1.301). This needs
QueenBee running: with its window and server closed the hook cannot reach the queen, and a worker started under *The
queen answers* goes back to its start flags. A Codex worker keeps the flags it started with, so the change reaches Codex
from the next worker that starts.

## What the queen will and will not allow

Allowed without asking you: reading and editing files inside the project folder, running tests
and ordinary project commands. **Refused, and the worker is told to carry on without:**
deleting files, downloading, installing packages, touching anything outside the folder.
Everything she decides appears under **Decisions**, her refusals in red. A worker she refused
something is marked **✕ queen said no** on the map until you answer: open it, and under its task
each refusal has **Allow it** (that exact command or file is allowed for this worker from now on,
and a running worker is told it may try again) and **She was right** (clears the mark).

## How "done" is checked

A worker saying "done" is not enough. The queen runs the project's own checks (tests, files
that must exist) and sends the worker back with the failure if they do not pass. When all
workers are done she runs the whole project's tests once more. Only then is the job done.

## Settings you may want

A file called `mother.toml` in the QueenBee folder changes the defaults. Example:

```toml
planner_model = "sonnet"     # the queen's own model
worker_model  = "haiku"      # cheaper model for easy parts
max_model     = "sonnet"     # nothing above this is ever used
```

## Stopping and cleaning up

- Closing the window leaves workers running. Reopen it any time. If they stopped in the meantime (the computer
  restarted, or every QueenBee worker was stopped), the window asks before anything starts again: **Continue**
  gives each half-finished job to a new agent, which finds the earlier work in the folder; **Stop it** leaves the
  work as it is.
- **✕ Stop** on a worker ends just that worker. **Remove queen** (in the queen's panel) takes a queen off
  the map; if she still has workers it asks, then stops them first. The very first queen cannot be
  removed, so there the button reads **Clear queen**: it forgets her plan and workers. Plan files stay
  on disk either way.
- `python -m queenbee.host --stop` ends everything QueenBee started.
- Workers keep notes in a hidden `.queenbee` folder inside your project. It is safe to
  delete when the job is finished; add it to `.gitignore` if you use Git.

## When something goes wrong

| What you see | Meaning and fix |
|---|---|
| "Claude Code is not installed" | Install it, run `claude` once in a terminal and sign in. |
| A worker is red / "needs you" | Click it. Usually it asked something outside the project folder. Answer in its terminal. |
| Nothing happens after Plan and start workers | The queen is planning (30-60 seconds the first time). The orange box says *planning*. |
| The computer is slow | Lower **Workers at most** to 2 or 3. |
| The window will not open | Run `.venv\Scripts\python.exe -m queenbee.mother` from a terminal; the message tells why. |
