# One-line fixes that fail

Each fix makes a silent failure visible or impossible. Use the smallest one that works,
then break it once to prove it can fail.

## A failure that becomes 0

```ts
// before
try { return await db.countPending(tenantId) } catch { return 0 }
// after
try { return await db.countPending(tenantId) }
catch (err) { console.error('countPending failed', { tenantId, err }); return null } // UI renders "unavailable"
```
```python
except Exception:
    log.exception("total_cost failed", extra={"tenant": tenant_id})
    return None  # caller shows "unavailable", never 0
```
**Prove it:** point the query at a missing table. The log line appears and the UI shows
"unavailable".

## A score input nothing writes

```ts
// in the scorer's test: fails the day ingest stops writing the field
expect(ingestChunk(sampleDoc).metadata.document_type).toBeDefined()
```
If there is no writer and none is coming, delete the factor. A dead factor is worse than
no factor, because it caps the score without saying so.
**Prove it:** remove the writer line. The test fails.

## Telemetry behind a debug flag

```ts
const trace = buildTrace(req)                                   // always
const clientTrace = debugRequested ? redact(trace) : undefined  // only exposure is gated
```

## A meter write whose failure is discarded

```ts
void persistTrace(trace).catch(err => console.error('persistTrace failed', { queryId: trace.id, err }))
```
Also add one daily check that a human sees, such as "rows written to the trace table in
the last 24 hours". A meter that writes nothing produces no log line at all.

## An LLM call with no cap

```ts
await generateText({ model, prompt, maxOutputTokens: 800 })
```
```ts
for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) { … }   // never while (true) around a model or tool call
```

## A model id nobody checks

```ts
// CI: every configured model id exists in the provider's live catalogue
const live = new Set((await fetch(CATALOGUE_URL).then(r => r.json())).data.map(m => m.id))
for (const id of CONFIGURED_MODELS) if (!live.has(id)) throw new Error(`retired model: ${id}`)
```

## An eval that cannot fail

```ts
expect(passRate).toBeGreaterThanOrEqual(0.85)   // a threshold that can be missed
expect(runnableCases).toBe(goldenCases.length)  // no case silently skipped
```
**Prove it:** add one known-bad case, or set a threshold the current run misses. The
build goes red.

## A published number

Next to the figure, show the date of the run, the runnable case count, and the command
that reproduces it. Add a scheduled job that re-runs the command and fails when it can't
run.
