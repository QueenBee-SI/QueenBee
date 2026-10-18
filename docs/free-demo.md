# The demo: a queen and three worker bees (about 10 minutes, setup included)

You will watch **three worker bees build one small program together, in the same folder, at the same
time**, while a **queen bee** (Sonnet 5.5) looks in on them every couple of minutes, settles what they
disagree on, answers their routine approvals and checks the result.

The program is a command-line notebook in three parts: a store, a search and an export. The parts
only fit if the workers agree on what a note looks like, and the task deliberately does not say. The
workers have to settle it between themselves, with the queen's help.

You need QueenBee installed (see the [README](../README.md)) and Claude Code or Codex signed in. With
both, the team is one of each: the first worker is Codex, the other two Haiku, and the queen Sonnet. With Claude Code
alone the workers are Haiku and the queen Sonnet; with Codex alone, the workers and the queen are Codex. QueenBee
is free; the agents and the queen's model use your own account and quota. A run takes a few minutes.

## The one-click way

1. Open **QueenBee** and click **▶ Try a demo** (left panel).
2. It copies the project to `~/QueenBeeDemo/free-demo`, creates the queen *Notes demo* (limits: 3
   workers, 20 M tokens) and adds three workers (Codex and Haiku with both installed, Haiku with Claude Code alone, Codex
   with Codex alone): **Store**, **Search** and **Export**, each with its own
   check command.
3. Watch the map. Click a worker to open its live terminal; you can type to it.
4. When all three are done, the queen runs the whole project's tests (**Delivery check**) and shows
   the result in her panel: *Delivered: the whole project's check passes.*

## What to look at

- **The queen's panel** (click the queen): her model (you can switch Sonnet 5.5 / Opus 5.5 / Codex; her box on the map names it),
  the line *Director · N looks · $… · next look in …s*, and her last observation. She only looks when
  something changed, and never while nothing is running.
- **The hive board** (same panel): the workers announce what they decided, for example the shape of a
  note; the queen adds a decision when two disagree or one is waiting. You can write on it too, to all
  workers or one.
- **Decisions tab:** every permission question the queen answered for a worker, and why.
- **A failed check:** if a worker says "done" but its check fails, the queen sends it back with the
  failure.

## Try steering it

- Write on the board: *"A note is a dictionary with id, text and tags."* All three read it.
- Stop a worker that looks stuck (**✕ Stop**), then **Remove**. Add a different one with **+ Worker bee**
  and another model, for example a Sonnet worker to review the code.
- Lower **Workers** or the budget in the queen's panel and see her hold back new workers.
- Switch the queen to Opus 5.5 and compare what she says.

## The manual way (your own project)

1. **Create a queen bee:** a name, what she is for, her model, optional limits.
2. **+ Worker bee:** a task, the project folder, a short name, a model, and (best) a command that proves
   the task is done, such as your tests.
3. Optionally set the **Delivery check** in the queen's panel: the command she runs over the whole
   project when every worker is in. If it fails she sends a worker to make the parts fit.

Several workers in one folder is the case the hive board and the queen's look-ins are for; workers in
different folders just run side by side.
