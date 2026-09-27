---
name: ai-signals-audit
description: Checks whether the numbers an AI product shows or decides on are real (confidence scores, verified badges, cost and usage meters, eval pass rates, benchmarks, fallback answers) by tracing each back to what writes it and what happens when that fails. Finds inputs nothing writes, failures that become a believable 0, telemetry only on in debug, evals that can't fail, silent model fallbacks and uncapped cost. Ships a self-testing scanner and fixes proven to fail. Use it before investigating any one of these by hand. Use it whenever someone asks for an AI feature audit, RAG health check or AI due diligence; says a confidence score never moves, a 'verified' badge is on everything, evals pass even when the prompt is broken, the LLM bill spiked, customers show 0 tokens used, the app may be falling back to a cheaper model, or a benchmark can't be reproduced; or is about to publish an accuracy or cost number, even if they never say "audit".
---

# AI signals audit

An AI product is a machine that emits numbers people act on: a confidence percentage, an
answer marked "verified", a cost per query, an eval pass rate, a benchmark headline. The
product can look perfectly healthy while those numbers are wrong, because the usual
failure throws no error and returns no 500. A score is computed from an input nothing
writes. A failed query becomes `0`. A meter meters and saves nothing. An eval skips half
its cases and still reports green.

This skill finds those failures by working **backwards from each number people trust** to
the code that produces it, then asking what that code does when it fails.

## Scope: what this covers, and what to use instead

Covered here: whether the numbers are **real**.

These are well served elsewhere, so point there rather than redoing them:

| Need | Use |
|---|---|
| Prompt injection, OWASP LLM Top 10 | Anthropic's `security-guidance` plugin, `claude-code-owasp` |
| Designing evals, calibrating LLM judges | `evals-skills` |
| Adding tracing and observability | the Langfuse or MLflow plugins |
| Tuning chunking, retrieval, reranking | `claude-rag-skills` |

If the only prompt-injection note worth making is where untrusted text reaches a prompt,
make it in one line and move on.

## The method

### 1. Inventory the trusted numbers

List every number or label that a user sees, or that the code branches on. Look at UI
components, API responses, dashboards, published pages (benchmarks, accuracy claims,
pricing), alerts, and anything with `score`, `confidence`, `cost`, `usage`, `rate`,
`accuracy`, `verified` or `hit` in its name. For each one, record where it is shown and
what decision rests on it.

If the owner can name the three numbers they'd be most embarrassed to have wrong, start
there.

### 2. Run the scanner

```bash
python <skill-dir>/scripts/scan_signals.py <repo> [--verbose | --json] [--include-tests]
python <skill-dir>/scripts/scan_signals.py --self-test
```

The default text report is quiet. Every HIGH finding is listed in full. MEDIUM findings
show the first 5 per check, then a count. INFO findings become one summary line per check,
and model ids become one line of distinct ids with counts to check against the provider's
catalogue. The totals line always counts every finding. Use `--verbose` to list every
finding, or `--json` for the complete machine-readable list (never collapsed).

It is a **locator**, not a judge. Each hit is a place to read, with the question to ask
there. It checks JS/TS and Python for:

- scores fed by fields nothing writes;
- failures that return `0`;
- errors swallowed near model, cost or telemetry code;
- metrics defaulted to `0`;
- telemetry behind a debug flag;
- fire-and-forget meter writes;
- LLM calls with no token cap;
- loops with no bound;
- evals that can't fail;
- placeholder values in eval data;
- hard-coded model ids.

Expect a few false positives, and resolve each one by reading the code. `--fail-on HIGH`
turns it into a CI gate once the findings are triaged.

### 3. Trace each trusted number to its writer

This is the step no scanner does for you, and it is the one that finds the expensive
problems. For each number from step 1, follow it back:

1. **What computes it?** Open the function.
2. **What are its inputs, and what writes each one?** Search for the writer, not the
   reader. A field is live when something fails if it isn't written, not when something
   reads it. Check every hop across a boundary (code to database, service to service,
   ingest to query), because that's where writers go missing.
3. **What happens when each input is missing, or its call fails?** If the answer is "it
   becomes 0, empty, a default, or the previous value", the number can be wrong while
   looking right.
4. **Is the writer on in production?** Check for debug flags, feature flags, env vars
   that default off, and sampling rates.
5. **Does anything prove the number is correct?** Is there a test that fails if the number
   is wrong, not just one that runs? An eval with a threshold that can be missed? A
   published result that can be re-run from the repo today?

**Name what would disprove the verdict before you commit to it.** A search result
describes where you looked, not what exists. A literal grep can't see a field name built
from a template string, and a repo search can't see a writer in another service. When a
writer might live outside the repo, say so, and ask for the one query that would settle it
(for example, rows written in the last 24 hours).

### 4. Give a verdict per number

| Verdict | Meaning |
|---|---|
| **Real** | every input has a live writer, failures are visible, and something would fail if it went wrong |
| **Partly real** | it works, but a failure path would silently distort it |
| **Fabricated** | an input is never written, or the failure path always wins |
| **Can't tell** | the writer is outside what you can see; name the query that settles it |

### 5. Give each finding a one-line fix that has been proven to fail

For every non-Real verdict, propose the smallest change that makes the failure visible or
impossible: a log line with identifiers, an explicit "unavailable" state instead of `0`,
an assertion that the writer exists, an eval threshold that can be missed, or a cap on
retries and tokens. `references/enforcers.md` has patterns in TypeScript and Python.

**Break it once on purpose.** Remove the writer, force the failure or lower the score,
and confirm the new check fails. A check that has never failed has never been tested.
When you can't run it, write the exact command that would.

**Reproduce safely.** The bugs this skill finds include unbounded loops and retries, so a
faithful reproduction can run forever or bill forever.

- Work on a copy, never in the user's repo.
- Mock every model and provider call. Never let a reproduction reach a paid API.
- Put a hard iteration cap inside the harness (for example, the mock throws after 50
  calls), and run it under a timeout (`timeout 30 node …`, or `--test-timeout`).
- If something still hangs, stop only the process you started, by its PID. **Never kill
  processes by name** (`taskkill /IM node.exe`, `pkill node`, `killall python`). That
  stops the user's dev servers, other agents and tools they rely on.

In a test run, a reproduction of an uncapped retry loop hung, and the stop command killed
every Node process on the machine.

### 6. Check the published numbers

Any number that appears on a public page (accuracy, benchmark pass rate, "zero
hallucinations", cost claims) needs four things:

- the date of the run;
- the **runnable** case count, not the planned one;
- the command that reproduces it;
- confirmation that the command still runs today.

If eval data holds placeholders that make cases skip, the published figure overstates
what was measured. Say so, and give the count that actually ran.

## Report format

```markdown
# AI signals audit: <product>

## Verdict
<two sentences: how many trusted numbers, how many real, the one to fix first>

## The numbers
| Number | Shown / used where | Written by | Fails how | Verdict |
|---|---|---|---|---|

## Findings, most expensive first
### 1. <number>: <what's wrong>
Evidence: `file:line` (and the writer search that came up empty)
Impact: <who sees what wrong value, and what decision it drives>
Fix: <one line>. Proven by: <how it was made to fail>

## Out of scope, with where to go instead
## What I couldn't see
<writers in other services, production data, dashboards; the query that would settle each>
```

## Scoring (0–10)

One point each, or half a point if partly met. Cite evidence for every point. Mark an item
N/A if the product doesn't have that feature, and rescale over the items that apply.

1. Every trusted number has an identified writer for every input.
2. No trusted number can silently become `0`, empty or a default when a call fails.
3. Cost and usage are recorded on production traffic, not only in debug, and a failed
   write is visible.
4. Every LLM call has a token cap, and every retry or tool loop has a bound.
5. Model ids are checked against the provider's live catalogue: a retired model fails
   loudly, not into a fallback.
6. Fallback paths have been exercised at least once, and their use is counted.
7. At least one eval can fail, and fails the build when it does.
8. Published results state the date and the runnable case count, and reproduce from the
   repo today.
9. Quality metrics can see the main failure mode. Ask: "this measures X; our main failure
   is Y; can it see Y?"
10. Every fix proposed in this audit was broken once to prove it fails.

## Why each rule exists

`references/incidents.md` holds the real incidents behind these checks, all from one
production RAG product. They include a confidence score capped at 72% for months because
five inputs were never written, a cost meter that metered perfectly and saved nothing, a
benchmark page advertising almost four times the cases that could actually run, and a
quality metric that could not see 68 of the 70 failures it was meant to catch. Read it when a finding seems too
small to matter.
