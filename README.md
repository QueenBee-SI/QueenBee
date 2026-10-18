<p align="center">
  <img src="queenbee/assets/queenbee.png" alt="QueenBee" width="88">
</p>

<h1 align="center">QueenBee</h1>

<p align="center"><b>Run Claude Code and Codex as one team, and get the job finished.</b></p>

<p align="center">
Every agent in its own live terminal, on one map, working on one job. A queen splits the work, answers the routine
prompts by a rule you can read, and runs your check at the end. Done means the check passed, not an agent saying so.
</p>

<p align="center">
  <a href="https://github.com/QueenBee-SI/QueenBee/releases"><b>Download</b></a> (Windows, macOS, Linux) &nbsp;·&nbsp;
  <a href="docs/install.md">Install guide</a> &nbsp;·&nbsp;
  <a href="docs/tour.md">Tour of the screen</a> &nbsp;·&nbsp;
  <a href="docs/assets/film/queenbee-film.mp4">Three-minute film</a>
</p>

![Real runs, sped up: three projects on one map, each a Sonnet queen with a Codex worker and two Claude Haiku workers; the view closes in on one queen while her workers go from working to done and she delivers, then pulls back to the whole hive](docs/assets/queenbee-hive.gif)

## Why

- **It finishes.** You name the check that means done (a test command, for example). The queen runs it at the end; if
  the parts do not meet, she sends one more worker to join them and runs it again. The report keeps what she checked
  apart from what a worker only claims.
- **One team across vendors.** Claude Code and Codex work on the same job from the same brief, each on the part that
  suits it. Whatever agents are installed on your computer can take any seat, the queen's too.
- **You choose who answers.** *The queen answers*: she answers the workers' routine permission prompts by a rule you can
  read (yes inside the project folder, no to deleting, downloading, installing or writing outside it), applied by code,
  not by a model, and every decision is listed with its reason; you can leave. *I answer*: every prompt is yours, and the
  queen only monitors.
- **It remembers.** What a team learns about your project is kept as findings, marked out of date when the files they
  name change, and handed to the next team in that folder.
- **Every agent in one place.** Claude Code, Codex, Gemini CLI, Kimi, OpenCode, Cursor Agent, Mistral Vibe, Grok Build:
  each in a real terminal you can open and type into. One map shows who is working, who is done and who needs you.
  (Claude Code and Codex are the two we run every day.)
- **Token use in view, under your limits.** Every worker's tokens are counted as it works and in the report. Give a
  queen a token budget and a number of workers at most; at the limit she starts no new worker.

## Get started

1. **Install.** [Windows installer or zip, macOS app, Linux tarball, or from source](docs/install.md). You need Claude
   Code or Codex installed and signed in; QueenBee has no account of its own and starts the agents as you.
2. **Try a demo.** One click: three agents build one small program in a few minutes, on your own account.
3. **Your own job.** Create a queen, pick a folder, type one sentence with what "done" means. For example:
   *"Add CSV export to the reports page. Done means `pytest tests/` passes."*

## How it works

Every worker is a real agent in a real terminal; click any hexagon and you are in it. The queen briefs them all from the
same short brief, looks in when something happens, and settles disagreements on a shared board. Their permission prompts
she answers by one rule, or leaves every one to you if you chose *I answer*. When everyone is done she runs your check,
sends one more worker if the parts do not meet, and writes a report of what was done, how it was checked and what is
not proven. The [tour of the screen](docs/tour.md) shows each part; the [user guide](docs/user-guide.md) has every button.

## What it is not

It does not make agents smarter. It is not a sandbox: the rule is the agent's own permission system, run through its
hook ([what that covers](SECURITY.md)). Everything runs on your computer, on your accounts; nothing is sent anywhere. It
is not a way around a vendor's terms: it runs each vendor's own unmodified program under your own sign-in, never touches
a token, and is not affiliated with Anthropic or OpenAI. What it does not do yet: [known limits](docs/known-limits.md).

## More

[Install guide](docs/install.md) · [Tour of the screen](docs/tour.md) · [User guide](docs/user-guide.md) ·
[The demo, step by step](docs/free-demo.md) · [Known limits](docs/known-limits.md) · [Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

Apache-2.0. Something hard to set up? Open an issue and say which step.
