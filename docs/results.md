# What we measured

One job, the same words, three ways of doing it: one agent alone, three agents side by side with nobody in charge, and
three agents with the queen. The same checker decides every time. Everything here can be run again with the files in
[`benchmarks/`](../benchmarks/README.md); the raw result of every run named below is in `benchmarks/runs/`.

The job is **MiniDB**, a small SQL engine in Python with no dependencies. *MiniDB 2* asks for nine features on an engine
that already works (93 of its 154 checks pass at the start). *MiniDB 3* asks for nine more on a finished MiniDB 2 (159 of
219 at the start). *The long job* is both in one go (98 of 219 at the start). The agents are Claude Code on Sonnet 5.5
and Codex on gpt-5.6; the queen is Sonnet 5.5. Where a person has to answer prompts, a stand-in answers them after a fixed delay and every answer is counted.

## 1. Alone, side by side, with the queen

The runs the film is cut from. MiniDB 2, 154 checks; the stand-in person answers each prompt about 15 s after it
appears, inside the run, so the times include the waiting. One run after another, each on a fresh server.

| Arm | Time | Checks | Prompts to a person | Tokens |
|---|---|---|---|---|
| One Sonnet 5.5 agent alone | 13:41 | 154 / 154 | 23 | 3.23 M |
| Three Sonnet 5.5 agents side by side, no queen | 13:43 | **153 / 154** | 57 | 5.61 M |
| Queen + Sonnet 5.5, Codex, Sonnet 5.5 (the film's run) | **6:20** | 154 / 154 | **0** | 4.34 M |
| The same team, five more runs | 7:01, 7:51, 8:06, 9:50, 9:59 | 154 / 154 all | 0 | 4.59 to 6.72 M |

**Side by side stopped one check short with all three agents saying "done".** Check 109 is the seam between the parser's
part and the executor's part, and nobody owned the seam: nobody ran the whole check and nobody sent the fix. The queen's
team met the same seam. Her delivery check caught it, she sent one more worker to join the parts, and ran the check again.
That is the point of the queen in one line: she answers the prompts herself, and she makes sure the job is actually
finished.

The first time we ran these three arms (2026-10-14, answers after half a second) it went the same way: side by side
reached **151 of 154** in 5:54 with 38 prompts and stopped; the queen's team finished 154 of 154 in 7:04 with none; the
lone agent finished in 11:11 with 19 prompts (`runs/video-peers-P1.json`, `video-team-V1.json`, `video-solo-V1.json`).

The film shows the best of the six queen runs. All six finished before either other arm and asked nobody anything; five
of the six used fewer tokens than side by side, one used more. The lone agent is the cheapest arm and the slowest.

## 2. The long job, with a person's answer time

MiniDB 2 and 3 in one go, 219 checks (`runs/video-*-BIG2.json`). Here the stand-in answered after about 4.5 s, so the
last column adds what a person at 15 s a prompt would have cost each agent.

| Arm | Time as run | Checks | Prompts to a person | Time with 15 s answers (estimated) | Tokens |
|---|---|---|---|---|---|
| Queen + Sonnet 5.5, Codex, Sonnet 5.5, one integration fix | **8:13** | 219 / 219 | **0** | **8:13** | 5.75 M |
| Three Sonnet 5.5 agents side by side, no queen | 7:22 | 219 / 219 | 47 | about 10:05 | 7.51 M |
| One Sonnet 5.5 agent alone | 14:55 | 219 / 219 | 31 | about 20:20 | **5.58 M** |

The same three arms on MiniDB 2 alone (`runs/video-*-R18*.json`): queen 6:31 with 0 prompts and 5.05 M; side by side
6:07 as run, about 8:57 with a person's answers, 48 prompts, 5.36 M; alone 11:15 as run, about 14:45 with answers, 20
prompts, 3.48 M. As run, bare parallelism is a little faster than the queen's team (the delivery check and the
integration fix are the difference). With a person answering, the queen's team is the fastest arm and the only one nobody
has to sit with, and it used 23 percent fewer tokens than side by side on the long job.

## 3. What a team learns stays with the project

A team's workers post what they decide and find on a board; the queen keeps it as findings in `.queenbee/`, the
integration-fix worker's note among them, and the next team in that folder gets the ones that concern its part in its
brief. The question: does that remove the seam?

| MiniDB 2, same team, same brief | Runs | Needed an integration fix |
|---|---|---|
| With the findings an earlier team left | 11 (8 of them hermetic) | **0 of 11** |
| Without them (the planner's brief alone, a hand-written brief, no brief, two other switches) | 20 (14 hermetic) | 15 of 20, one of them never finished |

The control is the planner's brief alone, three hermetic runs: 7:38, 10:03, 7:16, an integration round every time
(`runs/hermetic/H1016-0650-short-*`). The findings arm, eight hermetic runs: 5:33 to 9:13, median about 6:00, no round
(`H1015-2354-wake-*`, `-timer-*`, `H1016-0055-find-1`, and `runs/video-team-E1*-findings.json`).

**On a task the findings do not describe, they make no difference.** MiniDB 3 is an unseen task on the engine a team had
just finished. Three pairs: with the earlier team's findings 4:54, 4:57, 4:16 and 5.88, 7.07, 5.77 M; without them
4:34, 4:23, 5:52 and 4.55, 5.43, 5.79 M. Every run finished first time. The spread inside each arm is larger than the
difference between them. So the claim is the narrow one: findings that name a seam remove the integration round on the
job they came from; on a next job that meets other seams they are neither help nor harm.

## 4. Two vendors on one team

MiniDB 3, the unseen task, hermetic (`runs/hermetic/H1016-0719-*`). The planner gave the storage part to Codex and the
parser and executor to Sonnet 5.5.

| Arm | Runs | Time | Checks | Prompts to a person | Tokens |
|---|---|---|---|---|---|
| Queen + Sonnet 5.5, **Codex**, Sonnet 5.5 | 2 | **4:05, 3:53** | 219 / 219 both | **0** | **3.97, 3.66 M** |
| Queen + three Sonnet 5.5 (section 3, for comparison) | 6 | 4:16 to 5:52 | 219 / 219 all | 0 | 4.55 to 7.07 M |
| One Sonnet 5.5 agent alone | 3 | 8:16, 8:19; stalled at 6:27 | 219, 219; **178 / 219** | 14, 18; 10 | 4.02, 2.73; 1.81 M |

The mixed team is the fastest and cheapest arm on this task. The Codex worker reads less before its first write, and the
storage part suits it: in the film's runs the storage part cost the Claude agent side by side 1.64 M tokens and the Codex
worker the queen gave it to 0.51 to 1.06 M. The lone agent is the arm that does not always finish: one run in three
stopped with its tenth prompt open and stayed there until the forty minutes ran out.

## What the numbers do not say

- **One to eight runs per arm.** The same arm, same brief, same models, ran in 5:15 and in 9:52, with 4.7 and 6.9 M
  tokens. A difference under about a minute or a million tokens between two single runs says nothing. What holds across
  runs is whether the job finished and whether an integration fix was needed.
- **One job family.** A Python SQL engine with a three-part split, written so that its parts have seams. Nothing here says
  what happens on your codebase.
- **The stand-in says yes.** It approves every prompt after its delay. A person reads them, and sometimes says no.
- **Tokens.** Three agents cost about what three agents cost; the queen's own share (plan, look-ins, report) is under one
  percent. The lone agent is always the cheapest arm.
- **Our checker, our job.** We wrote the checks, and "finished" means they pass. The checker only reads the engine's
  answers, so an agent cannot pass it by editing it, but a different checker could rank the arms differently.
- **One machine, October 2026.** Windows, Claude Sonnet 5.5, Codex gpt-5.6. Models and tools change; rerun before you
  quote.

## The film

[`docs/assets/film/queenbee-film.mp4`](assets/film/queenbee-film.mp4), 1 minute 51.

- **0:00 to 0:20.** Three terminals, drawn: the prompts as Claude Code and Codex show them, and the one you forgot.
- **0:20 to 0:40.** The side-by-side run of section 1 on a plain monitoring page: the prompts, when they came, where the
  agents stopped, the checker's answer (153 of 154). The bars are decoration; the numbers are the run's.
- **0:40 to 1:24.** The queen's run of section 1, from real screenshots: the plan, the hive filling with Claude Code and
  Codex on one team, two messages between them from the board, the storage part's tokens by agent, a worker's live
  terminal, the Decisions list from the run's own records (prompts to you: 0), the check passing.
- **1:24 to 1:51.** More than one queen at once, and the three runs' numbers side by side. The queen's row is the best of
  six; the five others are in section 1.

No vendor logos; QueenBee is not affiliated with Anthropic or OpenAI.
