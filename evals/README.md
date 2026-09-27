# Evals: Cairn Signals (ai-signals-audit)

Everything needed to re-run the with-skill / without-skill comparison behind the numbers in
the main README: the prompts, a script that rebuilds the test app from scratch, and the answer
key the runs were graded against.

| File | What it is |
|---|---|
| `build_fixture.py` | Rebuilds `contractlens/`, the made-up app both published evals run against. Stdlib only, deterministic. |
| `evals.json` | The prompts, the assertions (the answer key), the list of planted defects, and the published results. |

## The fixture

`contractlens` is a tiny invented AI contract reviewer: ten files of TypeScript that are read, not
run. The app, its name and its claims are made up. Its README claims 95% accuracy on 40 contracts
and a "Verified" badge above 80% confidence. Planted in it:

1. the confidence score reads two metadata fields nothing writes, so it cannot reach 0.8
2. (the second of those fields)
3. usage and cost are recorded only when `?debug=1`
4. the usage write's failure is thrown away by `.catch(() => {})`
5. a dashboard count returns 0 when its query fails
6. 25 of the 40 golden eval cases hold a placeholder and skip themselves
7. the accuracy test has a threshold of 0, so it cannot fail
8. an uncapped `while (true)` retry that alternates between two models, one of them likely retired,
   with no output-token cap

plus **one decoy**: `documentCount` handles its failure correctly (logs it, returns null, and the page
shows "unavailable"). A good run flags the other count and leaves this one alone.

## Rebuild it

```
python evals/build_fixture.py /tmp/signals-fixture
```

This writes `/tmp/signals-fixture/contractlens/`. The output is byte-identical to the fixture the
published runs used. It refuses to write into a folder that already has one.

Check the rebuild with the plugin's own scanner:

```
python skills/ai-signals-audit/scripts/scan_signals.py /tmp/signals-fixture/contractlens
```

It should report 7 HIGH, 4 MEDIUM and 2 INFO: debug-gated telemetry, an eval that cannot fail,
25 placeholder cases, the 0 fallback, the discarded usage write, two fields read with no writer, the
uncapped model call and loop, and both model ids. It should not flag `documentCount`.

## Run the comparison

For each prompt in `evals.json`:

1. Build a **fresh** fixture for every run. Never let two runs share one.
2. Put it somewhere the run cannot reach `evals/`. The answer key sits in `evals.json` in plain sight;
   a run that can read it is not a test. Copy the fixture out of this repository and start the run
   there.
3. Replace `<fixture>` in the prompt with the folder you built into.
4. Run the prompt once **with** the skill installed and once **without** it. Same model, same settings.
5. Save each run's final answer, and hash the fixture before and after (for `fixture-untouched`).

## How it was graded

Each run's final answer was read against the assertions in `evals.json`: pass or fail per assertion,
with a line of evidence. `fixture-untouched` was checked by hashing the fixture before and after. The
per-eval score is assertions passed over assertions total.

## Results published so far

Run on 2026-09-26, one run per configuration per eval.

The headline in the main README is **100% vs 80% of checks** (with skill vs without), the average of
the per-eval pass rates over **three** evals: the two in this folder, and a third, **large-repo**, that
ran on a real production codebase. That codebase is private, so the large-repo eval, its answer key and
its fixture are not published and cannot be re-run from here.

The two published evals on their own:

| Eval | With skill | Without skill |
|---|---|---|
| investor-numbers | 9/9 | 8/9 (no fix said how it would be made to fail) |
| bill-doubled | 6/6 | 6/6 |
| **Published evals total** | **15/15** | **14/15** |

So on the evals anyone can re-run, the skill's measured advantage is one assertion. Most of the gap
in the headline comes from the private eval.

With the skill the runs were slower: 290 s against 118 s on investor-numbers, 487 s against 124 s on
bill-doubled.

## Known limits

- **Small n.** Two public evals and one private, one run each per configuration. One run can swing an
  assertion either way.
- **Made-up app.** The fixture is small, invented and planted on purpose. A real codebase hides its
  defects among thousands of healthy lines; the private eval was the only one that tested that.
- **Part of the headline can't be checked.** The 100% vs 80% includes an eval nobody else can re-run.
- **Author-built.** The same author wrote the skill, the fixture, the prompts and the assertions, and
  graded the runs.
- **The fixture names its decoy.** A code comment in lib/stats.ts says the decoy is handled correctly.
  It is kept so the rebuild matches what the published runs saw; a stricter re-run could delete it.
