# Security

QueenBee runs coding agents on your own machine, with your own accounts. This page says what it protects, what it does
not, and how to report a problem.

## What the queen's rule does

- Every worker is a Claude Code or Codex process you could have started yourself, in a folder you named. The vendor's own
  permission system is in force; QueenBee answers its questions through the vendor's hook (Claude Code `PreToolUse`) or
  policy (Codex `workspace-write`, no approval prompts), by a rule that is code, not a model.
- The rule, in order: refuse what deletes, downloads, installs, pushes, stops processes or reaches the network; allow
  reads, and edits and shell writes inside the project folder (a shell command is judged by where it writes: redirections,
  `tee`, PowerShell's file cmdlets, copies and moves, an inline `open(..., "w")`, `mkdir`; a leading `cd` is not a write,
  it only sets the folder that the relative targets after it are judged from); refuse an edit or a write
  that climbs out with `..`, names the home folder, or lands outside the project; leave anything else to the vendor's own
  prompt. Every decision is written down with its reason.
- The local API (the page server on 127.0.0.1) needs a token made at every start; the page gets it from the URL the app
  opens, the hook reads it from the state folder. Other websites are refused by Host and Origin checks;
  other programs on the machine are refused without the token.

## How QueenBee runs your agents

- It starts each vendor's own, unmodified command-line program (`claude`, `codex`, ...), on your computer, as you. The
  queen's own replies (the plan, her look-ins, the report) come from the same programs (`claude -p`, `codex exec`).
- You sign in with the vendor's own sign-in, in a terminal, once. QueenBee never opens, copies, stores or forwards a sign-in
  token or a key: it only checks that the vendor's credentials file exists, by its size. It sends no request to any
  vendor itself and has no account or server of its own.
- What the agents use is billed to your own plan or key, as if you had started them yourself, and it counts against that
  plan's limits; Anthropic says the Pro and Max limits assume ordinary, individual use, so for heavy parallel work an API
  key or a team plan may suit better. A queen's token budget and worker limit stop new work at the level you set.
- The prompts are answered through each vendor's documented controls (Claude Code's hooks and permission flags, Codex's
  sandbox and approval flags), or by you.
- Using an agent through QueenBee is still using that agent: its vendor's terms apply to you as they would without
  QueenBee ([Anthropic](https://code.claude.com/docs/en/legal-and-compliance),
  [OpenAI](https://openai.com/policies/)). Read them for how you use it. QueenBee is an independent project, not made,
  endorsed or supported by Anthropic, OpenAI or any other agent's vendor.

## What it does not do

- It is not a sandbox. What a vendor's auto mode would run without asking, it still runs; a worker's *reading* is not
  restricted (the brief may advise a worker to read only its part, and that is advice). An agent that writes a file
  through a program the rule does not know is not stopped by the rule. Use the vendor's sandbox for that.
- It does not hide your accounts from your own agents: the workers run as you.
- It does not sign the Windows binary yet.

## Reporting

Write to the maintainer at the address on the GitHub profile, or open a private security advisory on the repository.
Please include the QueenBee version, the vendor and model, and what the worker did. Problems in the queen's rule
(something approved that should not have been) are the ones to report first.
