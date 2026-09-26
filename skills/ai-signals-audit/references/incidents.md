# Incidents behind the checks

All of these come from one production RAG product, found over a few days in September
2026. Several have since been fixed. Each lesson holds either way. None of them threw an error. Each one was found by
tracing a number back to its writer.

## Contents
- The confidence score that could never say "high"
- The cost meter that saved nothing
- The benchmark that advertised four times what it could run
- The retired model that failed into a fallback
- The metric that couldn't see the failures
- The zero that looked like a fact
- The dashboard timings nothing recorded

---

### The confidence score that could never say "high"
A confidence scorer combined several factors, five of them read from chunk metadata:
`enhanced_chunking`, `context_summary`, `confidence_indicators`, `document_type` and
`similarity`. The ingest pipeline wrote none of them. With those factors permanently at
their defaults, displayed confidence topped out around 72%, below the 80% threshold for
"high". It stayed that way for months, because a scorer returning 65% looks exactly like a
scorer that works.
**Check:** `reader-no-writer`. **Verdict rule:** a field is done when something fails if
it isn't written, not when something reads it.

### The cost meter that saved nothing
Request tracing was switched on only when a debug parameter was present. Real traffic
built no trace. The cost meter calculated correctly and then persisted onto the trace,
so production cost went nowhere. Feedback collection lost its join key the same way,
because the query id travelled only inside the trace. The final insert was also
fire-and-forget with its failure discarded, so even with tracing on, a failed write would
have been invisible.
**Checks:** `debug-gated-telemetry`, `fire-and-forget-meter`. **Fix:** always collect,
gate only what's *exposed* to the client, and count rows written per day.

### The benchmark that advertised four times what it could run
A public benchmarks page reported results dated five months earlier. When re-run, the
largest suite, about half of all cases, skipped itself entirely, because its golden file
held a placeholder tenant id (`REPLACE_WITH_…`) in place of a provisioned one. Only about
a quarter of the advertised cases were runnable. The published figure described a run the
repository could no longer reproduce.
**Checks:** `eval-placeholder`, `eval-skips`. **Rule:** publish the runnable count, the
date and the reproducing command, and confirm the command still runs.

### The retired model that failed into a fallback
An intent classifier was configured with a model id the provider had retired. Calls
failed, the fallback took over, and classification quietly degraded, with no error
anywhere a human looked. A later check that compared all nine configured model ids against
the provider's live catalogue of 458 would have caught it before deploy.
**Check:** `model-id` (the list to verify). **Fix:** one catalogue check in CI.

### The metric that couldn't see the failures
A faithfulness metric was proposed to measure answer quality. It inspected the quotes
inside answers. But 68 of the 70 real failures were cases where no answer was given at
all, and the metric could not see them. Four experiments measured it carefully before one
sentence ruled it out: "This measures X. Our main failure is Y. Can it see Y?"
**Rule:** check what a metric is defined over before testing how well it works. Extra
rigour on the wrong population only strengthens the wrong conclusion.

### The zero that looked like a fact
Failed database queries fell back to `0` and rendered as "0 pending requests". A real
absence and a broken query looked identical, and they need opposite responses.
**Checks:** `fallback-fabricates`, `metric-coalesce`. **Fix:** log with identifiers, and
render "unavailable" rather than `0`.

### The dashboard timings nothing recorded
An admin processing-metrics page read per-stage timings (vision, parsing, chunking,
embedding, upsert) and a created-chunks count from document metadata. The fields were
declared in a type and read in the page, but no code path wrote them. The dashboard showed
nothing, or its defaults, for every document.
**Check:** `reader-no-writer`. A type declaration is not a writer.
