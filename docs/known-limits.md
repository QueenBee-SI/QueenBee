# Known limits

What QueenBee does not do yet, or has not been tried on.

- **Not signed yet.** Windows and macOS warn on the first open ([install guide](install.md)). Signing is the next
  release step.
- **No one has opened the macOS app on a real Mac yet.** It is built and started on GitHub's macOS machines, like the
  Linux build. Windows 11 is where QueenBee is used every day; on Linux the window needs the system's web engine and
  otherwise opens in the browser ([install guide](install.md)), and no real job has been run on Linux yet.
- **The queen's rule covers Claude Code workers**, through their hook. Codex team workers run inside Codex's own
  sandbox, so the queen never refuses them anything. The other agents (Gemini CLI, Kimi, OpenCode, Cursor Agent, Mistral
  Vibe, Grok Build) keep their own prompts and may ask you.
- **The rule is not a sandbox.** It decides what an agent may do through the agent's own permission system; for code you
  do not trust, use a container ([SECURITY.md](../SECURITY.md)).
- **Your project is the measurement that counts.** What QueenBee saves depends on how the job splits and how often the
  parts meet; try it on a small job first.
