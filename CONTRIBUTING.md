# Contributing

Thank you for looking. QueenBee is small and the rules are few.

## Run it from source

```
git clone https://github.com/QueenBee-SI/QueenBee
cd QueenBee
powershell -ExecutionPolicy Bypass -File scripts\install.ps1      # Windows
bash scripts/install.sh                                           # macOS, Linux
```

Python 3.11 or newer and Git. The agents (Claude Code, Codex) are yours; QueenBee finds them on PATH or in the usual
places. `.venv\Scripts\python -m queenbee.mother` opens the window; `--no-window` prints the page's address instead.

## Tests

```
python -m pytest -q tests
```

The suite stands in for the agents (no account is needed) and uses a state folder of its own. A change to the queen's
rule (`queenbee/mother/approver.py`) comes with a test in `tests/test_mother_hooks.py` that shows the call it allows or
refuses.

## What a change looks like

- One thing per commit, said plainly in the message: what changed and why.
- Prose in comments and docs: full sentences, no abbreviations a reader has to look up.
- No claim in the README without something a reader can check.
- The repository never contains a user's paths, tokens or transcripts.

## Where things are

- `queenbee/mother/`: the queen (planner, dispatch, director, approver and hook, board and findings, delivery check, report).
- `queenbee/mother/static/`: the page.
- `queenbee/agents/`: starting and watching the agents' terminals.
- `scripts/`: install and build for each platform.
- `docs/`: the install guide, the tour, the user guide, the demo walkthrough, the known limits.
