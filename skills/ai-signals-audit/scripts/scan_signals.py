#!/usr/bin/env python3
"""
scan_signals.py — find the places where an AI product's numbers can look fine while being wrong.

It is a *locator*, not a judge. Every hit is a place to read, with the question to ask there.
The skill (SKILL.md) turns hits into verdicts by tracing each trusted number to its writer.

Usage:
  python scan_signals.py PATH [--json] [--include-tests]
  python scan_signals.py --self-test

Checks (JS/TS and Python):
  fallback-fabricates   a catch/except whose only job is to return 0, [], {}, "", null, "N/A"
  swallowed-error       an empty catch / `except: pass`, or `.catch(() => {})`
  metric-coalesce       `?? 0` / `|| 0` / `or 0` on a name that looks like a score, cost or count
  debug-gated-telemetry trace/telemetry/usage/cost collection behind a debug flag
  fire-and-forget-meter a telemetry/cost/usage write whose failure is discarded
  reader-no-writer      a metadata field read somewhere and written nowhere in the scanned tree
  llm-call-uncapped     an LLM call with no max-tokens argument in reach
  loop-uncapped         `while (true)` / `while True` / retry loop with no visible cap
  eval-cannot-fail      an eval/benchmark assertion that is always true, or a zero threshold
  eval-placeholder      placeholder values in eval/golden data (cases that will self-skip)
  model-id              a hard-coded model id (INFO — check each against the provider's catalogue)

Exit code: 0, unless --fail-on LEVEL is given and a finding at or above LEVEL exists.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"}
DATA_EXT = {".json", ".jsonl", ".yaml", ".yml", ".csv"}
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "__pycache__",
             ".turbo", "coverage", ".vercel", "out", "vendor", ".cache"}
LEVELS = {"INFO": 0, "MEDIUM": 1, "HIGH": 2}

METRIC_WORDS = r"(score|confidence|cost|usage|tokens?|count|total|accuracy|rate|latency|price|spend|similarity|relevance|faithfulness|precision|recall|hits?)"
TELEMETRY_WORDS = r"(trace|telemetry|track|meter|usage|cost|analytics|metric|log_event|logEvent|capture|posthog|langfuse|insertEvent|recordUsage)"
LLM_CALL = re.compile(
    r"(messages\.create|chat\.completions\.create|completions\.create|responses\.create|"
    r"generateText|streamText|generateObject|streamObject|ChatCompletion\.create|invoke_model|"
    r"\.generate_content|client\.chat\(|litellm\.completion)\s*\(")
CAP_ARG = re.compile(r"max_tokens|maxTokens|max_output_tokens|maxOutputTokens|max_completion_tokens|maxCompletionTokens")
MODEL_ID = re.compile(
    r"""["'`]((?:anthropic/|openai/|google/|meta-llama/|mistralai/|deepseek/|x-ai/|qwen/)?"""
    r"""(?:claude-[a-z0-9.\-]+|gpt-[a-z0-9.\-]+|o[134]-?[a-z0-9.\-]*|gemini-[a-z0-9.\-]+|"""
    r"""mistral-[a-z0-9.\-]+|mixtral-[a-z0-9.\-]+|llama-?[0-9][a-z0-9.\-]*|deepseek-[a-z0-9.\-]+|"""
    r"""grok-[a-z0-9.\-]+|command-r[a-z0-9.\-]*|text-embedding-[a-z0-9.\-]+|embed-[a-z0-9.\-]+))["'`]""")
PLACEHOLDER = re.compile(r"REPLACE_WITH|REPLACE_ME|<YOUR[_ -]|YOUR_[A-Z_]+_HERE|PLACEHOLDER|CHANGEME|\bTODO\b|\bTBD\b|xxx+", re.I)
# `payload` is deliberately absent: it is usually a JWT, a webhook body or a buffer, whose
# writer is an external contract (tested on a real repo 2026-09-26: 2 of 2 payload hits were).
META_READ = re.compile(r"\b(?:metadata|meta|attributes|properties|extra|details)\??\.\s*([A-Za-z_][A-Za-z0-9_]*)(?!\s*\()"
                       r"|\b(?:metadata|meta|attributes|properties|extra|details)\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"
                       r"|\b(?:metadata|meta|attributes|properties|extra|details)\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")
# "token" alone is absent on purpose: on a real repo it matched auth tokens far more often than
# model tokens. Model-token counters are caught by their longer names.
AI_CONTEXT = re.compile(r"llm|\bmodel\b|prompt|completion|embedding|rerank|retriev|\brag\b|input_?tokens|output_?tokens|"
                        r"prompt_?tokens|completion_?tokens|max_?tokens|cost|usage|telemetry|\btrace|metric|score|"
                        r"confidence|\beval|bench|openai|anthropic|openrouter|gemini|pinecone|vector", re.I)


@dataclass
class Finding:
    level: str
    check: str
    file: str
    line: int
    snippet: str
    ask: str


def is_test_path(p: str) -> bool:
    return bool(re.search(r"(^|/)(__tests__|tests?|spec|e2e)(/|$)|\.(test|spec)\.[a-z]+$|_test\.py$|(^|/)test_[^/]+\.py$", p))


def is_eval_path(p: str) -> bool:
    return bool(re.search(r"eval|bench|golden|judge|grader", p, re.I))


def walk(root: Path):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(dirpath) / f
            if p.suffix in CODE_EXT or p.suffix in DATA_EXT:
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                except OSError:
                    continue
                yield p


def read_lines(p: Path) -> list[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"note: could not read {p}: {e}", file=sys.stderr)
        return []


def window(lines: list[str], i: int, after: int = 6, before: int = 0) -> str:
    return "\n".join(lines[max(0, i - before): i + after + 1])


# ------------------------------------------------------------------ per-file checks

def scan_code(rel: str, lines: list[str], out: list[Finding], include_tests: bool) -> None:
    test = is_test_path(rel)
    evalish = is_eval_path(rel)
    py = rel.endswith(".py")
    text = "\n".join(lines)

    for i, raw in enumerate(lines):
        line = raw.strip()
        n = i + 1
        if not line or line.startswith(("//", "#", "*", "/*")):
            continue

        if not test or include_tests:
            # --- a catch whose only statement returns a fabricated value
            if (not py and re.search(r"\bcatch\b", line)) or (py and re.match(r"except\b", line)):
                body = [l.strip() for l in lines[i + 1:i + 4] if l.strip() and not l.strip().startswith(("//", "#"))]
                inline = re.search(r"\bcatch\b[^{]*\{\s*(.*?)\s*\}", line) if not py else None
                first = (inline.group(1) if inline and inline.group(1) else (body[0] if body else ""))
                context = window(lines, i, after=4, before=15) + "\n" + rel
                ai_near = bool(AI_CONTEXT.search(context))
                # Only a failure that becomes a *number* (or a number-shaped collection) fabricates a
                # fact. `return null/false` is an absence the caller can see; flagging it made most of
                # the hits noise on a real repo (tested 2026-09-26).
                if re.match(r"return\s+(0|0\.0)\s*;?\s*$", first):
                    out.append(Finding("HIGH" if ai_near or re.search(METRIC_WORDS, context, re.I) else "MEDIUM",
                                       "fallback-fabricates", rel, n, raw.strip()[:140],
                                       "A failure here becomes a believable 0. Would anyone ever know it failed? Log it with identifiers, and return an explicit 'unavailable' the UI can render differently."))
                elif re.match(r"return\s+(\[\]|\{\}|['\"]N/?A['\"]|['\"]unknown['\"])\s*;?\s*$", first) and ai_near:
                    out.append(Finding("MEDIUM", "fallback-fabricates", rel, n, raw.strip()[:140],
                                       "A failure here looks like 'nothing found'. Is an empty result distinguishable from a failed call downstream?"))
                elif ai_near and ((inline is not None and inline.group(1) == "") or (not inline and body and body[0] in ("}", "pass")) or (py and body and body[0] == "pass")):
                    out.append(Finding("MEDIUM", "swallowed-error", rel, n, raw.strip()[:140],
                                       "An error near model, cost or telemetry code vanishes. Keep tolerating it if that's right, but add one log line with the identifiers someone would need."))
            if re.search(r"\.catch\(\s*\(?\s*[a-z_]*\s*\)?\s*=>\s*(\{\s*\}|0|null|undefined|\[\]|\{\s*\}\s*)\s*\)", line):
                if re.search(TELEMETRY_WORDS, window(lines, i, after=0, before=3), re.I):
                    out.append(Finding("HIGH", "fire-and-forget-meter", rel, n, raw.strip()[:140],
                                       "This write can fail forever without anyone noticing. Log the failure, and count rows landed per day somewhere a human looks."))
                elif AI_CONTEXT.search(window(lines, i, after=2, before=10)):
                    out.append(Finding("MEDIUM", "swallowed-error", rel, n, raw.strip()[:140],
                                       "An error near model, cost or telemetry code vanishes. Is that failure ever visible to anyone?"))

            # --- metric coalescing to zero
            m = re.search(r"([A-Za-z_][A-Za-z0-9_.?\]\[]*)\s*(\?\?|\|\|)\s*0(?![.\d])", line) if not py else \
                re.search(r"([A-Za-z_][A-Za-z0-9_.\]\[\"']*)\s+or\s+0(?![.\d])", line)
            accumulator = m and re.search(re.escape(m.group(0)) + r"\s*\)?\s*[+\-]", line)
            clamped = re.search(r"Math\.(max|min)\(", line)
            if m and re.search(METRIC_WORDS, m.group(1), re.I) and not accumulator and not clamped:
                # A default in a scoring, cost or eval path decides something; a display default mostly doesn't.
                decides = re.search(r"score|confiden|metric|eval|bench|cost|usage|billing|rank", rel, re.I) or re.match(r"return\b", line)
                out.append(Finding("MEDIUM" if decides else "INFO", "metric-coalesce", rel, n, raw.strip()[:140],
                                   f"If `{m.group(1)}` is missing, it silently becomes 0 — indistinguishable from a real zero. Is 'missing' possible here? If so, keep it distinct."))

            # --- telemetry gated by a debug flag
            if re.search(r"\b(if|&&|\?)\b.*\b(debug|isDebug|DEBUG|debugRequested|verbose)\b", line) or \
               re.search(r"\b(collect\w*|trace\w*|telemetry\w*|track\w*)\s*[:=]\s*[^,;]*\bdebug", line, re.I):
                if re.search(TELEMETRY_WORDS, window(lines, i, after=3), re.I):
                    out.append(Finding("HIGH", "debug-gated-telemetry", rel, n, raw.strip()[:140],
                                       "Is this collection only on when debugging? Then production traffic records nothing, and every number built on it (cost, usage, feedback joins) is empty."))

            # --- uncapped LLM calls
            if LLM_CALL.search(line):
                call = window(lines, i, after=14)
                if not CAP_ARG.search(call) and not re.search(r"\.\.\.\s*\w*(opts|options|params|config|args)\b", call):
                    out.append(Finding("MEDIUM", "llm-call-uncapped", rel, n, raw.strip()[:140],
                                       "No output-token cap in reach. What bounds the cost of one request, including retries and tool loops?"))

            # --- loops with no visible cap
            if re.search(r"while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;\s*\)|while\s+True\s*:", line):
                body = re.sub(r"Math\.(max|min)\(", "", window(lines, i, after=15))
                stream_reader = re.search(r"\.read\(\)|if\s*\(\s*done\s*\)\s*break|async for|for await", body)
                # A counter is not a cap: `attempt++` with nothing comparing it bounds nothing
                # (a fixture with exactly that slipped through, 2026-09-26).
                capped = re.search(r"\b(max|limit|budget|MAX_|deadline|timeout)\w*\b", body, re.I) or \
                    re.search(r"\b(attempts?|tries|retries|i|n|count)\w*\s*(<|<=|>=|>)\s*\w", body)
                if not stream_reader and not capped:
                    out.append(Finding("MEDIUM", "loop-uncapped", rel, n, raw.strip()[:140],
                                       "Nothing visible bounds this loop. If it calls a model or a tool, what stops one request costing 100x?"))
            if re.search(r"\b(retry|retries|attempt)\b", line, re.I) and re.search(r"\b(while|for)\s*\(|\bwhile\s+\w", line) and \
               not re.search(r"max|limit|<=?|MAX_|range\(", line, re.I):
                out.append(Finding("MEDIUM", "loop-uncapped", rel, n, raw.strip()[:140],
                                   "A retry loop with no cap on the same line. Confirm the bound, and that each attempt's cost is counted."))

        # --- model ids (INFO): each should be checked against the provider catalogue
        if not test:
            for mm in MODEL_ID.finditer(line):
                out.append(Finding("INFO", "model-id", rel, n, mm.group(1),
                                   "Is this id still served? A retired model can fail silently into a fallback. One check against the provider's model list catches it."))

        # --- evals that cannot fail
        if evalish or test:
            if re.search(r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assert\s+True\b|assert\s*\(\s*true\s*\)|expect\(\s*1\s*\)\.toBe\(\s*1\s*\)", line):
                out.append(Finding("HIGH", "eval-cannot-fail", rel, n, raw.strip()[:140],
                                   "This assertion passes whatever the system does."))
            if evalish and re.search(r"(threshold|min_?score|pass_?rate|minPass\w*)\s*[:=]\s*0(\.0+)?\s*[,;)]?\s*$", line, re.I):
                out.append(Finding("HIGH", "eval-cannot-fail", rel, n, raw.strip()[:140],
                                   "A zero threshold means every run passes. What score should fail the build?"))
            if evalish and re.search(r"\b(it|test|describe)\.skip\(|pytest\.skip\(|@pytest\.mark\.skip|\.skipIf\(", line):
                out.append(Finding("MEDIUM", "eval-skips", rel, n, raw.strip()[:140],
                                   "How many cases does this skip on a normal run? A published result should state runnable cases, not planned ones."))

    # --- reader collection happens across files; handled in main
    _ = text


def scan_data(rel: str, lines: list[str], out: list[Finding]) -> None:
    if not is_eval_path(rel) or re.search(r"example|sample|template", rel, re.I):
        return
    hits = [(i + 1, l) for i, l in enumerate(lines) if PLACEHOLDER.search(l) and not re.search(r"_comment|\"description\"", l)]
    if hits:
        n, l = hits[0]
        out.append(Finding("HIGH", "eval-placeholder", rel, n, l.strip()[:140],
                           f"{len(hits)} line(s) in this eval data hold placeholder values. Cases like these usually self-skip, so the runnable set is smaller than the published one. Count what actually runs."))


# ------------------------------------------------------------------ cross-file: readers with no writer

def readers_without_writers(files: dict[str, list[str]], out: list[Finding], include_tests: bool) -> None:
    reads: dict[str, tuple[str, int, str]] = {}
    corpus_parts = []
    for rel, lines in files.items():
        if is_test_path(rel) and not include_tests:
            continue
        for i, l in enumerate(lines):
            if l.strip().startswith(("//", "*", "/*", "#")):
                continue
            for m in META_READ.finditer(l):
                name = m.group(1) or m.group(2) or m.group(3)
                if not name or name in {"length", "get", "set", "map", "filter", "keys", "values", "items", "toString", "id"}:
                    continue
                # a read, not an assignment target
                after = l[m.end():]
                if re.match(r"\s*=(?!=)", after):
                    continue
                reads.setdefault(name, (rel, i + 1, l.strip()[:140]))
        corpus_parts.append("\n".join(lines))
    corpus = "\n".join(corpus_parts)
    for name, (rel, n, snip) in sorted(reads.items()):
        esc = re.escape(name)
        writer = re.search(
            rf"(^|[\s{{,(])[\"']?{esc}[\"']?\s*:(?!:)"          # object literal key  name: / "name":
            rf"|\.{esc}\s*=(?!=)"                               # obj.name = ...
            rf"|\[\s*[\"']{esc}[\"']\s*\]\s*=(?!=)"             # obj["name"] = ...
            rf"|\b{esc}\s*=(?!=)"                               # name = ... (shorthand source)
            rf"|[\"']{esc}[\"']\s*,",                           # ("name", value) setters
            corpus, re.M)
        if not writer:
            out.append(Finding("HIGH", "reader-no-writer", rel, n, snip,
                               f"`{name}` is read but nothing in this tree writes it. If a score or decision depends on it, that input is permanently empty. Find the writer (maybe another service), or delete the reader."))


# ------------------------------------------------------------------ driver

def scan(root: Path, include_tests: bool = False) -> list[Finding]:
    out: list[Finding] = []
    code: dict[str, list[str]] = {}
    for p in walk(root):
        rel = p.relative_to(root).as_posix()
        lines = read_lines(p)
        if p.suffix in CODE_EXT:
            code[rel] = lines
            scan_code(rel, lines, out, include_tests)
        else:
            scan_data(rel, lines, out)
    readers_without_writers(code, out, include_tests)
    # de-duplicate identical (check, file, line)
    seen, uniq = set(), []
    for f in out:
        k = (f.check, f.file, f.line, f.snippet if f.check == "model-id" else "")
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: (-LEVELS[f.level], f.check, f.file, f.line))
    return uniq


def self_test() -> int:
    fixtures = {
        "src/score.ts": """
export function confidence(chunk: any) {
  const a = chunk.metadata.similarity ?? 0;
  const b = chunk.metadata.document_type;
  return a + (b ? 0.1 : 0);
}
export async function pending(db: any) {
  try { return await db.count(); } catch (e) { return 0; }
}
export async function quiet(db: any) {
  try { await db.x(); } catch (e) {}
}
""",
        "src/route.ts": """
const debugRequested = url.searchParams.get('debug') === '1';
const collectTrace = debugRequested;
if (collectTrace) { await insertTrace(trace); }
recordUsage(cost).catch(() => {});
const r = await client.messages.create({ model: "claude-sonnet-5", messages });
while (true) {
  const step = await callTool();
}
""",
        "src/ingest.ts": """
export const doc = { similarity: 0.4 };
""",
        "pipeline/score.py": """
def total_cost(rows):
    try:
        return sum(r.cost for r in rows)
    except Exception:
        return 0
""",
        "eval/run.test.ts": """
it('passes', () => { expect(true).toBe(true); });
const threshold = 0;
""",
        "eval/golden.json": '{"cases":[{"tenant":"REPLACE_WITH_TENANT","q":"x"}]}',
    }
    expected = {"fallback-fabricates", "swallowed-error", "metric-coalesce", "debug-gated-telemetry",
                "fire-and-forget-meter", "llm-call-uncapped", "loop-uncapped", "eval-cannot-fail",
                "eval-placeholder", "reader-no-writer", "model-id"}
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        for rel, body in fixtures.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body.strip() + "\n", encoding="utf-8")
        found = scan(root)
    fired = {f.check for f in found}
    # the one field that *is* written must not be reported
    wrongly = [f for f in found if f.check == "reader-no-writer" and "similarity" in f.snippet and "document_type" not in f.snippet]
    for c in sorted(expected):
        print(f"{'ok  ' if c in fired else 'MISS'} {c}")
    ok = expected <= fired and not wrongly
    if wrongly:
        print("FALSE POSITIVE: reader-no-writer flagged `similarity`, which src/ingest.ts writes")
    print(f"\nself-test {'passed' if ok else 'FAILED'}: {len(expected & fired)}/{len(expected)} checks fired"
          f"{'' if not wrongly else ', 1 false positive'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-tests", action="store_true", help="also scan test files for code checks")
    ap.add_argument("--fail-on", choices=list(LEVELS), help="exit 1 if a finding at or above this level exists")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.self_test:
        return self_test()
    if not a.path:
        ap.error("give a PATH to scan, or --self-test")
    found = scan(a.path.expanduser().resolve(), a.include_tests)
    if a.json:
        print(json.dumps([asdict(f) for f in found], indent=2))
    else:
        by = {}
        for f in found:
            by.setdefault(f.check, []).append(f)
        for check, rows in by.items():
            print(f"\n[{rows[0].level}] {check} ({len(rows)})")
            print(f"  ask: {rows[0].ask}")
            for f in rows[:25]:
                print(f"  {f.file}:{f.line}  {f.snippet}")
            if len(rows) > 25:
                print(f"  ... {len(rows) - 25} more (use --json for all)")
        counts = {lv: sum(f.level == lv for f in found) for lv in LEVELS}
        print(f"\n{counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['INFO']} INFO — each is a place to read, not a verdict")
    if a.fail_on and any(LEVELS[f.level] >= LEVELS[a.fail_on] for f in found):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
