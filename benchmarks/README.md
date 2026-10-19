# Benchmarks

The job, the drivers and the raw results behind [docs/results.md](../docs/results.md). Everything runs through QueenBee's
local HTTP API against a second copy of QueenBee with its own state folder, so your own window is not in the way and
nothing is steered after the start. The agents run on your own accounts: a run of the queen's team costs about five
million tokens of Claude Sonnet 5.5 and Codex, a lone agent about three.

| What | Where |
|---|---|
| MiniDB 2: a small SQL engine, nine features to add, 154 checks | `minidb2/` (`TASK.md`, `QUEEN.md`, `check_minidb.py`) |
| MiniDB 3: nine more features on a finished MiniDB 2, 219 checks | `minidb3/` (`TASK.md`, `check_minidb3.py`; its `.queenbee/FINDINGS.md` is what the earlier teams left) |
| The long job: both in one go | `prepare_long_job.py` |
| The three arms: alone, side by side, with the queen | `bench.py` |
| Fresh server per run, shuffled order, a manifest per run, the exclusion rule | `hermetic.py` |
| The hand-written brief and the carried-over findings of the brief experiments | `briefs/` |
| Every run cited in the results page | `runs/` |

## Run one arm

1. Install QueenBee from source ([docs/install.md](../docs/install.md)), with Claude Code and Codex signed in.
2. Start a second server with a state folder of its own, headless:

       QUEENBEE_STATE_DIR=/some/empty/folder python -m queenbee.mother --port 7990 --host-port 7987 --no-window

   (PowerShell: `$env:QUEENBEE_STATE_DIR = 'C:\some\empty\folder'`.) The drivers read the server's token from that folder
   (`_api.py`); set `QUEENBEE_STATE_DIR` for them too, or `QB_TOKEN`.
3. From this folder:

       python bench.py team 1        # the queen's team on MiniDB 2
       python bench.py peers 1       # three agents side by side, no queen
       python bench.py solo 1        # one agent alone

   The job is copied to `~/QueenBeeDemo/video-N`, the result goes to `runs/video-<arm>-1.json`, the timeline next to it.
   A run takes five to fifteen minutes; the driver gives up after 40 (the third argument).

Switches, all through the environment: `TASK=2|3|23` (MiniDB 2, MiniDB 3, the long job); `BRIEF=short|none|full|findings`
(the planner's own brief, none, the hand-written `briefs/minidb2-full.md`, or the planner's plus `briefs/minidb2-findings.md`
put in place as `.queenbee/FINDINGS.md`); `DELAY=<seconds>` (how long the stand-in person takes to answer a prompt in the
solo and peers arms; 0.5 unless set, 12 for about 15 s with the server's 3 s looks); `CLAUDE_MODEL` (`sonnet`),
`CODEX_MODEL` (`gpt-5.6-sol`); `TEAM_AGENT=claude` pins the team to Claude, `""` lets the planner use both vendors;
`READ_SCOPE=own`, `INTERVAL=<seconds>`, `WAKE=off` are the queen's role switches from the brief experiments.

## Run a chain

`hermetic.py` runs several arms several times, each on a fresh server and state folder, in a shuffled order, and writes
`runs/hermetic/<run id>/manifest.json` and `result.json` for each:

    python hermetic.py --runs 3 short=TASK=2,BRIEF=short find=TASK=2,BRIEF=findings mix=TASK=3,TEAM_AGENT=

The exclusion rule is fixed in the runner, not applied by a person afterwards: a run is invalid when a worker or the queen
met the provider's usage limit, when no worker produced a token, or when the server did not answer. An invalid run keeps
its manifest with the reason and counts for nothing. The summary at the end gives each arm's median time, tokens and
integration rounds over its valid runs. Needs `psutil`.

## Reading a result

`seconds` is start to the end of the last worker, the waiting for answers inside it. `final` is the checker's last line.
`questions` is the number of prompts a person (the stand-in) answered; the queen's team has none. `verify_rounds` is the
number of integration fixes the queen sent (0 means her check passed first time). `tokens` is every
transcript's count plus the queen's own; `director` is the queen's looks and their cost; `costs` is per worker.

The manifests record the Python version, platform, switches, start and end, and the commit of the development tree the
run was made on. The film's recorded runs are summarised in `runs/film-*.json` (recorded with a window, not by `bench.py`,
so their fields are fewer); the side-by-side run the film shows is `runs/video-peers-F1.json`.
