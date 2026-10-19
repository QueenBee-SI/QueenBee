# Changelog

## Unreleased

- `docs/results.md`: what we measured (one agent alone, three side by side, three with the queen; the
  findings experiments; a Claude + Codex team), with the film's runs, and `benchmarks/` with the job, the drivers and
  every cited run's raw result, so the numbers can be rerun.

## v0.1.4 (2026-10-18)

The first release from this repository. QueenBee runs Claude Code and Codex as one team on one job, each agent in its
own live terminal, with a queen that splits the work, answers the routine permission prompts by a rule you can read, keeps
what the team learns, and runs the check you named at the end.

What is in it:

- **One map, every agent.** Claude Code, Codex, Gemini CLI, Kimi, OpenCode, Cursor Agent, Mistral Vibe and Grok Build,
  each in a real terminal you can open and type into; green when done, orange while working, red when one needs you.
- **The queen.** Planner mode (one sentence and what "done" means), or workers you place yourself; a chat with her before
  the job; look-ins when something happens on the hive board; limits on workers, tokens, memory, CPU and GPU.
- **The queen answers, or you do.** With *The queen answers*, prompts are decided by code: yes inside the project folder,
  no to deleting, downloading, installing or writing outside it (shell commands judged by where they write), everything
  else left to you; every decision listed with its reason, a refusal shown on the worker until you say *Allow it* or
  *She was right*. With *I answer*, every prompt is yours and the queen only monitors.
- **Done is checked.** The queen runs your check when every worker is in, sends one more worker if the parts do not meet
  (at most twice), and writes the completion report: what was done, how it was checked, what is not proven, time, tokens,
  the questions that reached you.
- **Memory.** Findings kept with the project (who found what, which check validated it), marked out of date when the files
  they name change, confirmed or retired from the Report tab, and handed to the next team in that folder.
- **Branches.** Each worker on its own Git worktree if you want; the queen merges when the checks pass.
- **Local only.** Your accounts, your folders; the local page needs a token made at every start.
- **Packages.** A Windows installer and zip, a macOS app, a Linux tarball, all built and started on GitHub's machines;
  checksums beside them. Not code-signed yet.
