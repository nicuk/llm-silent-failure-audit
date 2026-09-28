# Example: an AI signals audit, start to finish

This is the complete answer from one run of the skill, with one line changed: the location of the run's scratch folder, which named a folder on the machine it ran on. Nothing else was edited or picked from several runs.

- **Asked:** the `investor-numbers` prompt in [evals/evals.json](../evals/evals.json): a founder about to show investors an accuracy figure, a confidence score and a cost per query asks whether the numbers are real.
- **On:** ContractLens, the made-up AI contract reviewer that `python evals/build_fixture.py <folder>` builds, with 8 planted problems and one decoy that is handled correctly.
- **Run:** 2026-09-26 with a development version of the skill, before 1.0.0. The model-catalogue check it describes was done live on that date.

---

## AI signals audit: ContractLens

### Verdict

**Score: 1/10.** I checked seven trusted numbers and none of them is real. Two of the three you plan to show investors can't be backed by this repo in its current state:

- **95% accuracy.** The eval never calls the model and never compares an answer. It scores 100% on the 15 of 40 cases that run.
- **Confidence % and "Verified".** On data from your own ingest code, confidence tops out at 60%, so the badge can never appear honestly. A caller who sends the right metadata can make it appear.
- **Cost per query.** Production traffic records no usage, so the dashboard shows **$0.0000**.

**Fix first:** take the "95% accuracy, zero hallucinations, 40 real contracts" line out of the README and the deck today. It is the number an investor will quote back and ask you to reproduce, and the repo can't reproduce it. The first code fix is the retry loop in `app/api/chat/route.ts`. Your primary model id is no longer in the live catalogue, so every production request quietly fails over, and the loop has no bound.

About the score: the product itself scores 0 of 9. The one point is for item 10, "every fix proposed was broken once to prove it fails", which I did in scratch copies. You can't get the score up this week, but you can make sure nothing you show next week is false.

### The numbers

| Number | Shown / used where | Written by | Fails how | Verdict |
|---|---|---|---|---|
| Accuracy "95%", "zero hallucinations", "40 real contracts" | `README.md:2`, pitch | `eval/run.test.ts` | Calls nothing, compares nothing (`correct++` every case), threshold `0`. 25 of 40 cases are placeholders. Questions are literally `q0`/`a0` | **Fabricated** |
| Confidence % | `/api/chat` response `confidence` | `lib/confidence.ts` from request-body chunks | 2 of 4 factors (`source_quality` +0.25, `clause_type` +0.15) have no writer. Max from in-repo ingest is 0.60. Empty retrieval gives `null` | **Fabricated** |
| "Verified" badge | `/api/chat` `verified` (`conf >= 0.8`) | same | Unreachable from real ingest data. Reachable by anyone who POSTs chunks with those fields set | **Fabricated** |
| Cost per query today | `app/dashboard/page.tsx:5` | `recordUsage` in `route.ts:15` | Written only when `?debug=1`. Insert failure swallowed. 0 rows gives `$0.0000`. Price hard-coded, not per model. Not filtered by org | **Fabricated** |
| Contracts awaiting review | `app/dashboard/page.tsx:5` | `lib/stats.ts:4` | DB error returns `0` with no log | **Partly real** |
| Document count | `app/dashboard/page.tsx:5` | `lib/stats.ts:12` | Logs and returns `null`, UI shows "unavailable" | **Real** (this is the pattern to copy) |
| Which model answered | implicit (you'd say "powered by X") | `route.ts:4,13` | Primary `openai/gpt-4-32k` is not in the live catalogue. Every request fails over silently to `anthropic/claude-sonnet-5` | **Fabricated** (the config says one model, a different one serves) |

### Findings, most expensive first

#### 1. The 95% accuracy claim: nothing measures it
Evidence:
- `eval/run.test.ts:6` is `for (const c of runnable) { /* call the app, compare */ correct++; }`. No model call, no comparison.
- `eval/run.test.ts:2` sets `const threshold = 0`, so the test can't fail.
- `eval/golden.json:95-241`: cases 15–39 have `"tenant": "REPLACE_WITH_TENANT"` and are filtered out, which leaves **15 runnable of 40**.
- Every case is `"question": "qN", "expected": "aN"`, placeholders rather than contract questions.
- Nothing anywhere measures hallucination.
- There is no `package.json` or test-runner config, so the eval can't be run from the repo as checked in.

Proof: I ran the original test in a scratch copy with shimmed `it`/`expect`. It printed `computed accuracy = 1` and **passed** without touching a model.

Impact: the headline number in your pitch has no run behind it. Any technical due diligence that asks "show me the eval" finds this within minutes.

Fix, today: replace the README line with what you can defend, e.g. "Evaluation harness in progress; 40-case golden set being provisioned." After that:
- provision the 25 tenants;
- make the loop call the app and compare;
- set `THRESHOLD = 0.9`;
- add `expect(runnable.length).toBe(golden.cases.length)`;
- publish only with the run date, the runnable count and the command.

Proven by: the runnable-count assertion fails against today's `golden.json` with "25 of 40 cases hold placeholders and would skip".

#### 2. Confidence % and "Verified": the badge can't be earned, but it can be forged
Evidence:
- `lib/confidence.ts:4-5` reads `metadata.source_quality` and `metadata.clause_type`.
- The only chunk writer, `ingest/chunk.ts:2`, writes `{ similarity, page }` and nothing else. A search for both field names across the repo finds only the reader.
- `toChunk` is never imported, so in practice the chunks come from the POST body (`route.ts:8`), meaning the caller supplies the metadata.

Measured (scratch copy):
- The best chunk `toChunk` can produce (similarity 1.0, 10k chars) scores **0.60**. The threshold for "Verified" is 0.80.
- A request with caller-supplied `{similarity: 0.8, source_quality: 'primary', clause_type: 'anything'}` returns **`confidence: 0.80025, verified: true`**.
- A request with no chunks returns `"confidence": null` next to an answer. `Math.max()` of nothing is `-Infinity`, which JSON turns into `null`.

The score also never looks at the answer. It measures how good retrieval looked, not whether the answer is right. So even once it's fixed, "confidence" can't catch the main failure you care about, a wrong answer built on good chunks.

Impact: if the demo shows "Verified", either the chunks were hand-built or the badge is fake. If it shows 45–60% on every answer, investors will ask why your product is never confident.

Fix: either write `source_quality` and `clause_type` at ingest, or delete those two factors and rescale. Compute confidence from server-side retrieval, never from request-body metadata. Don't show the badge in the pitch until then.

Proven by: the test "a strong chunk from the ingest writer can reach Verified" fails on the original ("ingest does not write source_quality") and passes with the writer added. I then deleted the writer line from the fixed copy and it failed again.

#### 3. Primary model is retired, the fallback is silent, and the retry loop has no bound
Evidence:
- `route.ts:4` lists `MODELS = ['openai/gpt-4-32k', 'anthropic/claude-sonnet-5']`.
- `route.ts:11` is `while (true)`.
- `route.ts:17` is `catch { attempt++; }`: no log, no backoff, no limit.
- `route.ts:13` has no `maxOutputTokens`.

Checked today (2026-09-26) against the public AI Gateway catalogue (`https://ai-gateway.vercel.sh/v1/models`, 390 models):
- `openai/gpt-4-32k` is **absent**;
- `anthropic/claude-sonnet-5` is present.

Measured (scratch copy, mocked model):
- **Primary failing:** every request goes gpt-4-32k, then sonnet-5, and the response carries nothing to show it fell back.
- **Both models failing (outage or rate limit):** 50 model calls for one request before my harness stopped it. It would not have stopped on its own.
- **`?debug=1` with the repo's `lib/db.ts` stub:** `recordUsage` throws synchronously inside the `try`. That re-runs the paid, successful model call, 50 calls before the harness stopped it.

Impact:
- Every production request pays an extra failed round trip.
- One provider outage turns every open request into an infinite retry storm.
- A single response can run to the model's full output limit, 128k tokens per the catalogue.

Fix: `for (let attempt = 0; attempt < MODELS.length; attempt++)`, plus `console.error('generateText failed', { queryId, model, attempt, err })`, a fallback counter, `maxOutputTokens: 800`, and a live model id as the primary. Add a CI check that fails when any configured id is missing from the catalogue.

Proven by:
- The "bounded retries" test fails on the original (50 calls) and passes when fixed (≤2).
- The "fallback counted and logged" test fails on the original (no counter) and passes when fixed.
- The catalogue check already fails on today's config (`openai/gpt-4-32k False`).

#### 4. Cost per query shows $0.0000 on real traffic
Evidence:
- `route.ts:15` wraps the only `recordUsage` call in `if (debug)`.
- `lib/usage.ts:3` has `.catch(() => {})`.
- `lib/usage.ts:7` has `/ Math.max(rows.length, 1)`, which turns zero rows into `$0`.
- `route.ts:15` hard-codes $3/$15 per million tokens for whichever model answered.
- `costPerQueryToday()` takes no `orgId`, but the dashboard is per org.

Measured:
- **Production requests:** 5 of them wrote **0 rows**, and the dashboard showed `$0.0000 per query`.
- **One `?debug=1` request:** the dashboard jumped to `$0.0060`, an average over debug requests only.
- **Failed insert:** 0 log lines.
- **Pricing:** the catalogue lists claude-sonnet-5, the model actually serving, at **$2/$10 per million tokens**. So even the debug figure overstates cost by about 50%.

Impact: any unit-economics slide built on this dashboard is either $0 or a small debug sample at the wrong price.

Fix: always call `recordUsage`, gate only what goes back to the client, log insert failures with `queryId`, return `null` ("unavailable") when there are no rows, filter by org, and price per model from the catalogue.

Proven by: the tests "every production request writes a usage row" (original: 0 of 5), "no rows shows unavailable, not $0" (original: 0) and "usage write failure is logged" (original: no log) all fail on the original and pass when fixed. I then re-added the `if (debug)` gate to the fixed copy and the first test failed again.

#### 5. "Contracts awaiting review" shows 0 when the DB is down
Evidence: `lib/stats.ts:5-6` is `catch { return 0; }`. `documentCount` a few lines below already does this correctly: it logs and returns `null`.

Fix: copy `documentCount`, i.e. `console.error('pendingReviews failed', { orgId, err }); return null;`, and render "unavailable".

Proven by: with `db.count` throwing, the original returns `0` and the test fails. The fixed version returns `null` and passes.

### Scoring

| # | Item | Score | Evidence |
|---|---|---|---|
| 1 | Every trusted number has a writer for every input | 0 | `source_quality`, `clause_type` have no writer. Cost rows are written only in debug |
| 2 | Nothing silently becomes 0/empty/default | 0 | `pendingReviews` becomes 0, cost becomes $0, `similarity ?? 0`, empty chunks give `null` confidence |
| 3 | Cost recorded on production, failed write visible | 0 | `if (debug)` gate, `.catch(() => {})` |
| 4 | Token cap and bounded loops | 0 | No `maxOutputTokens`, `while (true)` |
| 5 | Model ids checked against the live catalogue | 0 | No check, and the primary id is already missing |
| 6 | Fallback exercised and counted | 0 | Fallback runs on every request, nothing counts or logs it |
| 7 | An eval can fail and fails the build | 0 | Threshold 0, `correct++` unconditionally |
| 8 | Published results dated, runnable count, reproducible | 0 | No date, claims 40 (15 run), no runner config |
| 9 | Quality metric can see the main failure | 0 | Accuracy measures nothing. Confidence never looks at the answer. No hallucination measure behind "zero hallucinations" |
| 10 | Every proposed fix broken once to prove it fails | 1 | All 8 enforcers fail on the original. Two were also re-broken on the fixed copy |
| | **Total** | **1/10** | |

### What to say next week instead

- **Accuracy:** "We have a 40-case golden set of real contracts and are wiring the harness; first numbers by <date>." Don't use 95%.
- **Confidence:** show the answer with its source citations. Leave the % and the badge off the demo.
- **Cost:** quote a per-query estimate from the model's list price times your measured average tokens, and label it an estimate. Don't show the dashboard figure.

### Out of scope, with where to go instead

- **Prompt injection.** Untrusted contract text and client-supplied chunks are concatenated straight into the prompt (`route.ts:13`). Use Anthropic's `security-guidance` plugin.
- **Designing the real accuracy eval and grading answers:** `evals-skills`.

### What I couldn't see

- **The retrieval service that builds `chunks`.** It lives outside this repo and might write `source_quality` and `clause_type`. The query that settles it is a sample of 100 recent `/api/chat` request bodies, counting how many chunks carry each field.
- **The real `db` client.** `lib/db.ts` is `{}` here, so I tested against a mock. Run `SELECT count(*) FROM usage_events WHERE created_at > now() - interval '24 hours'` next to your request count for the same window. If rows are near zero while requests are in the hundreds, finding 4 is confirmed in production.
- **Which provider you actually route through.** The `openai/...` id format and the `ai` package suggest Vercel AI Gateway, which is the catalogue I checked. If you call OpenAI directly, check `GET /v1/models` with your key instead.

Proof harness (scratch, outside your repo): `enforcers.test.ts`, run with `ROOT=contractlens|fixed|broken node --test enforcers.test.ts`, plus `probe.ts` and `fixed/` holding the proposed changes. Your repo was not modified.
