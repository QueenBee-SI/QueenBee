# Your first job in five minutes

What you need: QueenBee installed ([install guide](install.md)), Claude Code or Codex signed in on this computer, and a
project with a command that says whether it works (a test suite, a build, a script). If you have no project at hand,
the one-click [demo](free-demo.md) brings its own.

## 1. Open QueenBee

The window opens on the Hive, an empty map. **Agents on this computer**, top left, lists the agents QueenBee found;
if the one you use is missing, the [install guide](install.md) says where it looks.

## 2. Create a queen

Click **Create a queen bee**. Give her a name, choose **Planner** as her job and **Sonnet 5.5** as her model, and
create her. She appears on the map as the big hexagon.

## 3. Give her the job

Click the queen, open **Planner mode** in her panel, and fill in:

- **Project folder:** your project.
- **Instruction:** one or two sentences, ending with what "done" means. For example:

      Add CSV export to the reports page, with a unit test for the exporter.
      Done means: pytest tests/ passes.

- **Multi-agent** if the job has separate parts, **Single agent** for one focused change, **Queen decides** if unsure.

Click **Plan and start workers**. The queen reads the project, splits the job, writes each worker a brief, and starts
them. Each worker is a real Claude Code or Codex terminal in a hexagon next to her.

## 4. Watch, or leave

Orange hexagons are working, green are done, red need you. Click any hexagon to see its terminal and type into it.
The queen answers the workers' routine permission prompts by one rule (yes inside the project folder, no to deleting,
downloading, installing or writing outside it); the **Decisions** tab lists every answer with its reason. If you
would rather answer everything yourself, click **I answer** in her panel.

You can close the lid now. A job like the example takes five to fifteen minutes.

## 5. Read the result

When every worker is in, the queen runs your "done" command herself. If it fails because the parts do not meet, she
sends one more worker to join them and runs it again. The **Report** tab shows the check's state, her completion report
(what was done, how it was checked, what is not proven, time, tokens, prompts), the timeline, and the files that
changed. **Copy the result card** gives you a few lines to paste into the pull request.

## If something goes wrong

The [known limits](known-limits.md) page lists what QueenBee does not do yet. Stopping a job, removing a queen and
cleaning up are in the [user guide](user-guide.md). If a step here did not match what you saw, that is worth an
issue: say which step.
