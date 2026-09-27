# A/B: does recall change what the agent gets right?

The same task, twice: once with the skillmem hooks in the agent's settings,
once without. Measured by outcome — did the answer contain what it had to —
and by tokens to get there. Nothing else.

## Why outcome, not a proxy

While tuning the composer we measured "the right record reached the prompt".
Cutting record bodies from 400 to 50 characters raised that from 37% to 62%
on a 100-question set. It also took the agent's correct answers from 18/18 to
15/18: fifty characters carried a rule's tone and not its detail, and the
agent answered confidently and wrong. A proxy that moves the right way while
the outcome moves the wrong way is worse than no number. So this harness
measures the outcome, and every change to recall goes through it before it
is argued about.

## How to read a result

1. **Noise first.** Run the same arm twice on the same tasks before comparing
   arms. Our own noise was ±1 correct answer in 18 and under 1% on tokens for
   one-turn answers; a change smaller than your noise is not a result. The
   report prints the worst within-cell spread across repeats for this reason.
2. **Correctness before tokens.** On our corpus recall gave 17–18/18 against
   6–7/18 without it, and one turn instead of two. The token median halved,
   but the means were equal — the agent without memory sometimes gives up
   cheaply and wrong. "Saves tokens" is not a claim this harness supports on
   its own; "answers correctly instead of inventing" is.
3. **Six tasks is a stand, not a benchmark.** It answers "did this change
   hurt what recall is for" on YOUR corpus. It does not rank memory systems.

## Running it

    SKILLMEM_DB=~/Library/Application\ Support/skillmem/memory.db bench/ab/run.sh

Arm A gets the hooks, arm B gets an empty settings file. Neither gets a
`CLAUDE.md`: it carries much of the same knowledge and would leak into the
control, which is the mistake this harness exists to avoid. `SKILLMEM_BODY_CHARS`
is passed through as `BODY=`, so a composer change can be measured against
the default without two builds.

Write your own `tasks.txt`. The shipped one is six placeholders and will
score nothing on your memory. Good tasks are the things you keep re-explaining:
read-only, one-line answers, a regex that a correct answer must match, in
the form `id|prompt|regex` (the regex last, so it may contain `|`). Keep
secrets, hostnames and addresses out — results are plain text and this file
is public.
