#!/usr/bin/env python3
"""
scan_signals.py — find the places where an AI product's numbers can look fine while being wrong.

It is a *locator*, not a judge. Every hit is a place to read, with the question to ask there.
The skill (SKILL.md) turns hits into verdicts by tracing each trusted number to its writer.

Usage:
  python scan_signals.py PATH [--verbose | --json] [--include-tests]
  python scan_signals.py --self-test

Output: the text report is quiet by default. Every HIGH finding is listed in full; MEDIUM
findings show the first 5 per check, then a count; INFO findings are summarised, one line per
check (model ids as the distinct ids with counts). The totals line always counts everything.
--verbose lists every finding. --json is always complete.

Checks (JS/TS and Python):
  fallback-fabricates   a catch/except whose only job is to return 0, [], {}, "", null, "N/A"
  swallowed-error       an empty catch / `except: pass`, or `.catch(() => {})`
  metric-coalesce       `?? 0` / `|| 0` / `or 0` on a name that looks like a score, cost or count
  debug-gated-telemetry trace/telemetry/usage/cost collection behind a debug flag
  fire-and-forget-meter a telemetry/cost/usage write whose failure is discarded (`.catch(() => {})`,
                        or a bare `asyncio.create_task(...)` / `executor.submit(...)`)
  reader-no-writer      a metadata field read in code and written nowhere in the scanned tree
                        (INFO when the object comes from a call defined outside the tree)
  llm-call-uncapped     an LLM call with no max-tokens argument in reach, or one that can be undefined
  loop-uncapped         `while (true)` / `while True` / retry loop with no visible cap (MEDIUM when it
                        calls a model or tool and doesn't end on a sentinel, INFO otherwise)
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
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path

CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"}
DATA_EXT = {".json", ".jsonl", ".yaml", ".yml", ".csv"}
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv", "__pycache__",
             ".turbo", "coverage", ".vercel", "out", "vendor", ".cache"}
LEVELS = {"INFO": 0, "MEDIUM": 1, "HIGH": 2}
MEDIUM_SHOWN = 5  # per check, in the default (quiet) text output; HIGH is never collapsed
# The INFO summary prints one question per check. Checks whose `ask` names one variable need a
# question that fits all of them, or the summary would describe the first hit only.
SUMMARY_ASK = {
    "metric-coalesce": "A missing value here silently becomes 0, indistinguishable from a real zero. "
                       "Is 'missing' possible? If so, keep it distinct.",
}
MODEL_ID_ASK = ("Check each against the provider's model catalogue: a retired id can fail silently "
                "into a fallback.")

METRIC_WORDS = r"(score|confidence|cost|usage|tokens?|count|total|accuracy|rate|latency|price|spend|similarity|relevance|faithfulness|precision|recall|hits?)"
TELEMETRY_WORDS = r"(trace|telemetry|track|meter|usage|cost|analytics|metric|log_event|logEvent|capture|posthog|langfuse|insertEvent|recordUsage)"
LLM_CALL = re.compile(
    r"(messages\.create|chat\.completions\.create|completions\.create|responses\.create|"
    r"generateText|streamText|generateObject|streamObject|ChatCompletion\.create|invoke_model|"
    r"\.generate_content|client\.chat\(|litellm\.completion)\s*\(")
# The same kind of call in the Python frameworks (LlamaIndex, LangChain): a model method on a
# receiver named like one. Tested 2026-09-27: a planted `self._llm.acomplete(...)` with no cap
# was invisible to every pattern above.
LLM_RECV_CALL = re.compile(r"\b(\w*(?:llm|model|chat)\w*)\.(a?complete|a?chat|a?invoke|a?stream)\s*\(", re.I)
# `chat_service.chat(...)` is the app's own layer, which sets (or doesn't) the cap further down.
NOT_A_MODEL = {"service", "services", "facade", "manager", "router", "controller", "repo", "repository", "store",
               "history", "session", "handler", "api", "view", "views"}
CAP_ARG = re.compile(r"max_tokens|maxTokens|max_output_tokens|maxOutputTokens|max_completion_tokens|maxCompletionTokens"
                     r"|max_new_tokens|num_predict")
# A definition or declaration of a model method is not a call (tested 2026-09-27: 6 of 9
# llm-call-uncapped hits on one repo were `abstract generateText(...)` and method definitions).
DEF_MODIFIER = r"(?:export|default|public|private|protected|static|override|abstract|async|readonly|declare|function|def|get|set)"
# A model or tool call inside a loop body: what makes an unbounded loop expensive.
TOOL_CALL = re.compile(r"\b\w*[tT]ool\w*\s*\(")
# A loop that ends on a sentinel ends when its producer does (tested 2026-09-27: all 13 sampled
# loop-uncapped hits on two repos were false, most of them queue consumers or generator drains).
SENTINEL = re.compile(r"\bis\s+None\b|\bStopIteration\b|\bis\s+_?[A-Z][A-Z0-9_]*\b|===?\s*(?:null|undefined)\b"
                      r"|isinstance\([^)]*,\s*\(?_?\w*(?:Done|Sentinel|Stop|End|Eof|EOF)\w*|\bsentinel\b", re.I)
# A bare `asyncio.create_task(...)` / `executor.submit(...)` statement: nothing keeps the result.
PY_BACKGROUND = re.compile(r"^(?:[\w.]+\.)?(create_task|ensure_future|submit|run_coroutine_threadsafe)\s*\(")
# Words that name a meter write, matched as whole parts of an identifier (`saveTokenUsage` has the
# part `usage`; `parameters` does not have the part `meter`).
TELEMETRY_PARTS = {"trace", "traces", "tracing", "telemetry", "track", "tracking", "meter", "metering", "usage",
                   "cost", "costs", "analytics", "metric", "metrics", "posthog", "langfuse", "billing", "spend"}
MODEL_ID = re.compile(
    r"""["'`]((?:anthropic/|openai/|google/|meta-llama/|mistralai/|deepseek/|x-ai/|qwen/)?"""
    r"""(?:claude-[a-z0-9.\-]+|gpt-[a-z0-9.\-]+|o[134]-?[a-z0-9.\-]*|gemini-[a-z0-9.\-]+|"""
    r"""mistral-[a-z0-9.\-]+|mixtral-[a-z0-9.\-]+|llama-?[0-9][a-z0-9.\-]*|deepseek-[a-z0-9.\-]+|"""
    r"""grok-[a-z0-9.\-]+|command-r[a-z0-9.\-]*|text-embedding-[a-z0-9.\-]+|embed-[a-z0-9.\-]+))["'`]""")
PLACEHOLDER = re.compile(r"REPLACE_WITH|REPLACE_ME|<YOUR[_ -]|YOUR_[A-Z_]+_HERE|PLACEHOLDER|CHANGEME|\bTODO\b|\bTBD\b|xxx+", re.I)
# `payload` is deliberately absent: it is usually a JWT, a webhook body or a buffer, whose
# writer is an external contract (tested on a real repo 2026-09-26: 2 of 2 payload hits were).
# The field name must be a whole word not followed by `(`: without the `\b`, `metadata.extend(`
# backtracked to a field called `exten` (tested 2026-09-27: 17 of 26 reader-no-writer hits on
# four repos were method calls read that way). `import.meta` is the module, not a field.
META_OBJ = r"(?<!import\.)\b(?:metadata|meta|attributes|properties|extra|details)"
META_READ = re.compile(META_OBJ + r"\??\.([A-Za-z_]\w*)\b(?!\s*\()"
                       + r"|" + META_OBJ + r"\s*\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]"
                       + r"|" + META_OBJ + r"\.get\(\s*[\"']([A-Za-z_]\w*)[\"']")
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


def classify(text: str, py: bool) -> str:
    """One character per character of `text`: 'c' code, 's' inside a string literal, 'k' comment.
    A small lexer, not a parser: enough to tell a field read in code from the same words in a
    prompt template, and a brace in code from one in a string. JS regex literals are not
    recognised; a quote inside one is a string until the end of that line."""
    out: list[str] = []
    n, i = len(text), 0
    state = "code"      # code | line | block | str
    quote, fstr = "", False
    stack: list[list] = []  # open `${` / f-string `{`: [state to return to, quote, fstr, depth]
    while i < n:
        ch = text[i]
        if state == "code":
            if ch == "\n":
                out.append("c"); i += 1; continue
            if py and ch == "#" or not py and text.startswith("//", i):
                state = "line"; out.append("k"); i += 1; continue
            if not py and text.startswith("/*", i):
                state = "block"; out.append("kk"); i += 2; continue
            if stack and ch == "{":
                stack[-1][3] += 1
            elif stack and ch == "}":
                if stack[-1][3] == 0:
                    _, quote, fstr, _ = stack.pop()
                    state = "str"; out.append("c"); i += 1; continue
                stack[-1][3] -= 1
            if ch in "\"'" or (not py and ch == "`"):
                if py and text[i:i + 3] in ('"""', "'''"):
                    quote = text[i:i + 3]
                else:
                    quote = ch
                pre = re.search(r"([A-Za-z]{1,2})$", text[max(0, i - 2):i])
                fstr = py and bool(pre) and "f" in pre.group(1).lower()
                if not py and ch == "`":
                    fstr = True  # a template literal's `${` opens code
                state = "str"; out.append("c" * len(quote)); i += len(quote); continue
            out.append("c"); i += 1; continue
        if state == "line":
            if ch == "\n":
                state = "code"; out.append("c")
            else:
                out.append("k")
            i += 1; continue
        if state == "block":
            if text.startswith("*/", i):
                state = "code"; out.append("kk"); i += 2; continue
            out.append("c" if ch == "\n" else "k"); i += 1; continue
        # inside a string
        if ch == "\\":
            out.append("ss"[:n - i]); i += 2; continue
        if text.startswith(quote, i):
            state = "code"; out.append("c" * len(quote)); i += len(quote); continue
        if ch == "\n" and len(quote) == 1 and quote != "`":
            state = "code"; out.append("c"); i += 1; continue  # an unterminated quote ends at the line
        if fstr and quote == "`" and text.startswith("${", i):
            stack.append(["str", quote, fstr, 0]); state = "code"; out.append("cc"); i += 2; continue
        if fstr and py and ch == "{":
            if text.startswith("{{", i):
                out.append("ss"); i += 2; continue
            stack.append(["str", quote, fstr, 0]); state = "code"; out.append("c"); i += 1; continue
        out.append("c" if ch == "\n" else "s"); i += 1
    return "".join(out)[:n]


class Source:
    """A file's lines, with a lazily built map of which characters are code."""

    def __init__(self, lines: list[str], py: bool):
        self.lines, self.py = lines, py
        self.text = "\n".join(lines)
        self.starts = [0]
        for l in lines:
            self.starts.append(self.starts[-1] + len(l) + 1)
        self._kinds: str | None = None

    @property
    def kinds(self) -> str:
        if self._kinds is None:
            self._kinds = classify(self.text, self.py)
        return self._kinds

    def is_code(self, i: int, col: int) -> bool:
        return self.kinds[self.starts[i] + col] == "c"

    def without_comments(self, a: int, b: int) -> str:
        k = self.kinds
        return "".join(" " if k[j] == "k" else self.text[j] for j in range(a, b))

    def brace_block(self, i: int, col: int, reach: int = 300, limit: int = 20000) -> tuple[int, int] | None:
        """(start, end) offsets of the text inside the first code `{` at or after line i, column
        col (within `reach` characters) and its matching `}`."""
        k, t = self.kinds, self.text
        p = self.starts[i] + col
        end_search = min(len(t), p + reach)
        while p < end_search and not (t[p] == "{" and k[p] == "c"):
            p += 1
        if p >= end_search:
            return None
        depth, q = 0, p
        stop = min(len(t), p + limit)
        while q < stop:
            if k[q] == "c":
                if t[q] == "{":
                    depth += 1
                elif t[q] == "}":
                    depth -= 1
                    if depth == 0:
                        return p + 1, q
            q += 1
        return None

    def indented_block(self, i: int, limit: int = 200) -> list[str]:
        """The raw lines of the Python block opened by line i (deeper indentation than line i)."""
        base = len(self.lines[i]) - len(self.lines[i].lstrip())
        out = []
        for l in self.lines[i + 1:i + 1 + limit]:
            if not l.strip():
                continue
            if len(l) - len(l.lstrip()) <= base:
                break
            out.append(l)
        return out

    def statement_start(self, i: int, col: int, max_lines: int = 15) -> int:
        """The line on which the JS expression ending at (i, col) starts: walks back over
        balanced brackets and over lines that continue a chain (`.then(...)`, `})`)."""
        k, t = self.kinds, self.text
        p = self.starts[i] + col - 1
        depth, line = 0, i
        floor = self.starts[max(0, i - max_lines)]
        while p >= floor:
            c = t[p]
            if k[p] == "c":
                if c in ")]}":
                    depth += 1
                elif c in "([{":
                    depth -= 1
                    if depth < 0:
                        return line
                elif c == ";" and depth == 0:
                    return line
                elif c == "\n":
                    if depth == 0 and not re.match(r"\s*[.)\]}?]", self.lines[line]):
                        return line
                    line -= 1
            p -= 1
        return line


def ident_parts(text: str) -> set[str]:
    """Lower-cased words of every identifier: `saveTokenUsage` -> {save, token, usage}."""
    parts = set()
    for ident in re.findall(r"[A-Za-z_]\w*", text):
        for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", ident):
            parts.add(w.lower())
    return parts


def model_call(text: str) -> re.Match | None:
    """The first LLM call in `text`: a known SDK call, or a model method on a receiver named
    like a model (`self._llm.acomplete(`, `chat_model.invoke(`), not like a service."""
    m = LLM_CALL.search(text)
    if m:
        return m
    for r in LLM_RECV_CALL.finditer(text):
        if not ident_parts(r.group(1)) & NOT_A_MODEL:
            return r
    return None


def receiver_construction(lines: list[str], i: int, recv: str, back: int = 200) -> str:
    """The nearest assignment above line i that builds `recv` from a call, with the 20 lines
    after it (the constructor's arguments), or "" when it isn't in this file."""
    pat = re.compile(rf"\b{re.escape(recv)}\s*(?::[^=\n]+)?=(?!=)\s*(?:await\s+)?(?:new\s+)?[\w.]+\s*\(")
    for j in range(i - 1, max(-1, i - 1 - back), -1):
        if pat.search(lines[j]):
            return window(lines, j, after=20)
    return ""


def handler_statements(src: Source, i: int, py: bool) -> list[str] | None:
    """The statements of the catch/except block that starts on line i, comments removed.
    None when the block can't be found (then nothing is claimed about it)."""
    line = src.lines[i]
    if py:
        m = re.match(r"\s*except\b[^:]*:(.*)$", line)
        if not m:
            return None
        inline = m.group(1).split("#")[0].strip()
        if inline:
            return [inline]
        return [l.strip() for l in src.indented_block(i) if not l.strip().startswith("#")]
    m = re.search(r"(?<!\.)\bcatch\b|\.catch\s*\(", line)
    if not m or not src.is_code(i, m.start()):
        return None  # no handler here, or the word "catch" inside a string or comment
    if m.group(0).startswith("."):
        # `.catch(...)`: only a block-bodied handler has statements; `=> expr` is an expression.
        head = re.match(r"\.catch\s*\(\s*(?:async\s*)?(?:\(?\s*\w*\s*\)?\s*=>|function\b[^{]*)\s*\{", line[m.start():])
        if not head:
            return None
    block = src.brace_block(i, m.end())
    if not block:
        return None
    body = src.without_comments(*block)
    return [s.strip() for s in body.split("\n") if s.strip()]


def is_definition(line: str, m: re.Match) -> bool:
    """Whether an LLM_CALL match is a method definition or declaration rather than a call."""
    if m.group(0).startswith("."):
        return False
    prefix = line[:m.start()].strip()
    if prefix.endswith("."):
        return False
    if not re.fullmatch(rf"(?:{DEF_MODIFIER}\s+)*(?:{DEF_MODIFIER}|\*)?\s*\*?", prefix):
        return False  # something precedes the name: `await`, `=`, `return`, an argument list
    if re.search(r"\b(function|def|abstract|async|public|private|protected|static|override|declare)\b|\*", prefix):
        return True
    rest = line[m.start():]
    # a bare `name(` at the start of a statement: a TS member has typed parameters or a return type
    return bool(re.match(r"\w+\s*(?:<[^>]*>)?\s*\([^)]*\)\s*:", rest)
                or re.match(r"\w+\s*(?:<[^>]*>)?\s*\(\s*\w+\??\s*:", rest))


def multiline_signature(lines: list[str], i: int, m: re.Match) -> bool:
    """`streamText(` at the start of a line, then `input: Type,` on the next: a TS member
    declared over several lines. A call's argument can't be `name: Type` without braces."""
    return (not m.group(0).startswith(".") and lines[i].rstrip().endswith("(") and i + 1 < len(lines)
            and not lines[i].strip()[:m.start()].strip() and bool(re.match(r"\s*\w+\??\s*:\s*[A-Za-z_]", lines[i + 1])))


def maybe_undefined_cap(call: str) -> str | None:
    """If every token cap in `call` is set from a value that can be undefined or None
    (`maxTokens: this.config.options?.maxTokens`, `max_tokens=kwargs.get("max_tokens")`),
    return the first such assignment. An undefined cap sends no cap at all."""
    caps = list(CAP_ARG.finditer(call))
    if not caps:
        return None
    first, consumed = None, 0
    for c in caps:
        if c.start() < consumed:
            continue  # part of the value just read
        if c.start() > 0 and call[c.start() - 1] == "." and not re.match(r"\s*=(?!=)", call[c.end():]):
            continue  # a property read (`opts.maxTokens`), not a cap being passed
        m = re.match(r"[\"']?\s*[:=](?!=)\s*", call[c.end():])
        if not m:
            return None  # a cap we can't read counts as a cap
        v_start = c.end() + m.end()
        depth, j = 0, v_start
        while j < len(call):
            ch = call[j]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            elif ch == "\n" and depth == 0 and call[v_start:j].strip():
                break
            j += 1
        value = call[v_start:j].strip()
        consumed = j
        last = re.split(r"\?\?|\|\||\bor\b", value)[-1].strip()
        if not (re.fullmatch(r"undefined|null|None", last) or "?." in last
                or re.search(r"\.get\(\s*[^,()]+\)$", last) or re.search(r"getattr\([^()]*,\s*None\s*\)$", last)):
            return None
        first = first or f"{c.group(0)}{call[c.end():v_start]}{value}"
    return first


# ------------------------------------------------------------------ per-file checks

def scan_code(rel: str, lines: list[str], out: list[Finding], include_tests: bool) -> None:
    test = is_test_path(rel)
    evalish = is_eval_path(rel)
    py = rel.endswith(".py")
    src = Source(lines, py)

    for i, raw in enumerate(lines):
        line = raw.strip()
        n = i + 1
        if not line or line.startswith(("//", "#", "*", "/*")):
            continue
        indent = len(raw) - len(raw.lstrip())  # `line` is stripped; Source works in raw columns

        if not test or include_tests:
            # --- a catch whose only statement returns a fabricated value, or that does nothing.
            # The handler's own block is read (tested 2026-09-27: reading "the next line" took a
            # function's closing brace after `.catch((err) => console.log(err))` for an empty catch).
            promise_catch = re.search(r"\.catch\(\s*\(?\s*[a-z_]*\s*\)?\s*=>\s*(\{\s*\}|0|null|undefined|\[\]|\{\s*\}\s*)\s*\)", line)
            if not promise_catch and ((not py and re.search(r"(?<!\.)\bcatch\b|\.catch\s*\(", line))
                                      or (py and re.match(r"except\b", line))):
                body = handler_statements(src, i, py)
                first = body[0] if body else ""
                context = window(lines, i, after=4, before=15) + "\n" + rel
                ai_near = bool(AI_CONTEXT.search(context))
                # Only a failure that becomes a *number* (or a number-shaped collection) fabricates a
                # fact. `return null/false` is an absence the caller can see; flagging it made most of
                # the hits noise on a real repo (tested 2026-09-26).
                if body is None:
                    pass
                elif re.match(r"return\s+(0|0\.0)\s*;?\s*$", first):
                    out.append(Finding("HIGH" if ai_near or re.search(METRIC_WORDS, context, re.I) else "MEDIUM",
                                       "fallback-fabricates", rel, n, raw.strip()[:140],
                                       "A failure here becomes a believable 0. Would anyone ever know it failed? Log it with identifiers, and return an explicit 'unavailable' the UI can render differently."))
                elif re.match(r"return\s+(\[\]|\{\}|['\"]N/?A['\"]|['\"]unknown['\"])\s*;?\s*$", first) and ai_near:
                    out.append(Finding("MEDIUM", "fallback-fabricates", rel, n, raw.strip()[:140],
                                       "A failure here looks like 'nothing found'. Is an empty result distinguishable from a failed call downstream?"))
                elif ai_near and (all(s in ("pass", "...") for s in body) if py else not body):
                    out.append(Finding("MEDIUM", "swallowed-error", rel, n, raw.strip()[:140],
                                       "An error near model, cost or telemetry code vanishes. Keep tolerating it if that's right, but add one log line with the identifiers someone would need."))
            if promise_catch:
                # The write that fails silently is the statement the `.catch` hangs off, which can
                # start lines earlier (tested 2026-09-27: `saveTokenUsage({...}).catch(() => {})`
                # was reported on its last line, and an audio level meter's `.close()` was read as
                # a usage meter because "meter" appeared on a line above it).
                start = src.statement_start(i, indent + promise_catch.start())
                statement = "\n".join(lines[start:i + 1])[:2000]
                if ident_parts(statement) & TELEMETRY_PARTS or re.search(r"insertEvent|logEvent|log_event|recordUsage", statement):
                    out.append(Finding("HIGH", "fire-and-forget-meter", rel, start + 1, lines[start].strip()[:140],
                                       "This write can fail forever without anyone noticing. Log the failure, and count rows landed per day somewhere a human looks."))
                elif AI_CONTEXT.search(window(lines, i, after=2, before=10)):
                    out.append(Finding("MEDIUM", "swallowed-error", rel, n, raw.strip()[:140],
                                       "An error near model, cost or telemetry code vanishes. Is that failure ever visible to anyone?"))
            # A Python background write whose task nothing keeps: its result and exception vanish.
            if py and PY_BACKGROUND.match(line):
                call = "\n".join(lines[i:i + 4])
                depth, j = 0, 0
                for j, ch in enumerate(call):
                    depth += ch == "("
                    depth -= ch == ")"
                    if ch == ")" and depth == 0:
                        break
                if ident_parts(call[:j + 1]) & TELEMETRY_PARTS:
                    out.append(Finding("HIGH", "fire-and-forget-meter", rel, n, raw.strip()[:140],
                                       "This write runs in the background and nothing keeps its result: a failure is never seen, and an unreferenced asyncio task can be dropped mid-flight. Keep the task, log failures in a done-callback, and count rows landed per day."))

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

            # --- telemetry gated by a debug flag. Any identifier containing "debug" is a flag
            # (tested 2026-09-27: `settings().server.debug_mode` slipped past a fixed list of
            # names), except a log call (`logger.debug(`) and the logging level constant.
            gate = re.search(r"(\bif\b|\band\b|&&|\?)(.*)", line)
            cond = gate.group(2) if gate else ""
            flags = [t for t in re.finditer(r"\b\w*debug\w*\b|\bverbose\b", cond, re.I)
                     if not re.match(r"\s*\(", cond[t.end():]) and not re.search(r"logging\.$|isEnabledFor\(\s*$", cond[:t.start()])]
            if flags or re.search(r"\b(collect\w*|trace\w*|telemetry\w*|track\w*)\s*[:=]\s*[^,;]*\b\w*debug", line, re.I):
                if re.search(TELEMETRY_WORDS, window(lines, i, after=3), re.I):
                    out.append(Finding("HIGH", "debug-gated-telemetry", rel, n, raw.strip()[:140],
                                       "Is this collection only on when debugging? Then production traffic records nothing, and every number built on it (cost, usage, feedback joins) is empty."))

            # --- uncapped LLM calls (not definitions or declarations of the method)
            lm = model_call(line)
            if lm and not is_definition(line, lm) and not multiline_signature(lines, i, lm):
                call = window(lines, i, after=14)
                if lm.re is LLM_RECV_CALL:
                    # A model object is often capped where it is built (`new ChatOpenAI({ maxTokens })`,
                    # `createLLM(name, t, maxTokens)`): that construction is in reach too, when it is
                    # in this file (tested 2026-09-27: 3 of 3 such calls on a real repo were capped so).
                    call += "\n" + receiver_construction(lines, i, lm.group(1))
                spread = re.search(r"\.\.\.\s*\w*(opts|options|params|config|args)\b", call)
                if not CAP_ARG.search(call) and not spread:
                    out.append(Finding("MEDIUM", "llm-call-uncapped", rel, n, raw.strip()[:140],
                                       "No output-token cap in reach. What bounds the cost of one request, including retries and tool loops?"))
                elif not spread:
                    loose = maybe_undefined_cap(call)
                    if loose:
                        out.append(Finding("MEDIUM", "llm-call-uncapped", rel, n, raw.strip()[:140],
                                           f"The cap here can be undefined (`{' '.join(loose.split())[:80]}`), and an undefined cap sends no cap. What sets it in production?"))

            # --- loops with no visible cap. Only a loop in code: the same words in a docstring
            # ("...while a retry of the same task resumes") are prose.
            loop =re.search(r"while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;\s*\)|while\s+True\s*:", line)
            if loop and src.is_code(i, indent + loop.start()):
                body = re.sub(r"Math\.(max|min)\(", "", window(lines, i, after=15))
                stream_reader = re.search(r"\.read\(\)|if\s*\(\s*done\s*\)\s*break|async for|for await", body)
                # A counter is not a cap: `attempt++` with nothing comparing it bounds nothing
                # (a fixture with exactly that slipped through, 2026-09-26).
                capped = re.search(r"\b(max|limit|budget|MAX_|deadline|timeout)\w*\b", body, re.I) or \
                    re.search(r"\b(attempts?|tries|retries|i|n|count)\w*\s*(<|<=|>=|>)\s*\w", body)
                if not stream_reader and not capped:
                    if py:
                        block = "\n".join(src.indented_block(i))
                    else:
                        span = src.brace_block(i, 0)
                        block = src.without_comments(*span) if span else window(lines, i, after=15)
                    calls_out = model_call(block) or TOOL_CALL.search(block)
                    sentinel = SENTINEL.search(block) and re.search(r"\b(break|return)\b", block)
                    if calls_out and not sentinel:
                        out.append(Finding("MEDIUM", "loop-uncapped", rel, n, raw.strip()[:140],
                                           "Nothing visible bounds this loop, and it calls a model or a tool. What stops one request costing 100x?"))
                    else:
                        out.append(Finding("INFO", "loop-uncapped", rel, n, raw.strip()[:140],
                                           "No visible bound, but the body calls no model or tool, or ends on a sentinel. Confirm the producer always ends."))
            retry_loop = re.search(r"\b(while|for)\s*\(|\bwhile\s+\w", line)
            if re.search(r"\b(retry|retries|attempt)\b", line, re.I) and retry_loop and \
               src.is_code(i, indent + retry_loop.start()) and not re.search(r"max|limit|<=?|MAX_|range\(", line, re.I):
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
            # `score >= 0` passes for every non-negative score, so it checks nothing about quality.
            if re.search(r"^\s*assert\s+[\w.\[\]()'\"]+\s*>=\s*0(\.0+)?\s*(,.*)?$"
                         r"|\bassert\s*\(\s*[\w.\[\]()]+\s*>=\s*0(\.0+)?\s*\)"
                         r"|\.toBeGreaterThanOrEqual\(\s*0(\.0+)?\s*\)", line):
                out.append(Finding("HIGH" if evalish else "MEDIUM", "eval-cannot-fail", rel, n, raw.strip()[:140],
                                   "`>= 0` holds for every non-negative score, so this passes whatever the system does. What score should fail?"))
            if evalish and re.search(r"(threshold|min_?score|pass_?rate|minPass\w*)\s*[:=]\s*0(\.0+)?\s*[,;)]?\s*$", line, re.I):
                out.append(Finding("HIGH", "eval-cannot-fail", rel, n, raw.strip()[:140],
                                   "A zero threshold means every run passes. What score should fail the build?"))
            if evalish and re.search(r"\b(it|test|describe)\.skip\(|pytest\.skip\(|@pytest\.mark\.skip|\.skipIf\(", line):
                out.append(Finding("MEDIUM", "eval-skips", rel, n, raw.strip()[:140],
                                   "How many cases does this skip on a normal run? A published result should state runnable cases, not planned ones."))

    # --- readers with no writer are found across files, in readers_without_writers


def scan_data(rel: str, lines: list[str], out: list[Finding]) -> None:
    if not is_eval_path(rel) or re.search(r"example|sample|template", rel, re.I):
        return
    hits = [(i + 1, l) for i, l in enumerate(lines) if PLACEHOLDER.search(l) and not re.search(r"_comment|\"description\"", l)]
    if hits:
        n, l = hits[0]
        out.append(Finding("HIGH", "eval-placeholder", rel, n, l.strip()[:140],
                           f"{len(hits)} line(s) in this eval data hold placeholder values. Cases like these usually self-skip, so the runnable set is smaller than the published one. Count what actually runs."))


# ------------------------------------------------------------------ cross-file: readers with no writer

def external_binding(lines: list[str], obj: str, upto: int, defined) -> str | None:
    """If `obj` is bound in this file, before line `upto`, to what a call returns
    (`metadata = s3.head_object(...)`, `meta = Parser().parsestr(p)`) and the function called is
    not defined anywhere in the tree, return its name: the fields' writer is a library or
    another service, outside what a text search can check."""
    for l in reversed(lines[max(0, upto - 80):upto]):
        m = re.match(rf"\s*{re.escape(obj)}\s*(?::[^=]+)?=(?!=)\s*(?:await\s+)?(.+)$", l)
        if not m:
            continue
        rhs = m.group(1).strip()
        if re.search(r"\b(metadata|meta|attributes|properties|extra|details)\b", rhs) or not re.match(r"[\w.]+(\(|$)", rhs):
            return None  # built from another metadata object, or not a call chain
        calls, depth = [], 0
        for t in re.finditer(r"(\w+)\s*\(|[()]", rhs):
            if t.group(1) and depth == 0:
                calls.append(t.group(1))
            depth += t.group(0).endswith("(") - (t.group(0) == ")")
        if calls and not defined(calls[-1]):
            return calls[-1]
        return None
    return None


def readers_without_writers(files: dict[str, list[str]], out: list[Finding], include_tests: bool) -> None:
    reads: dict[str, tuple[str, int, str, str | None]] = {}
    corpus_parts = []
    sources: dict[str, Source] = {}
    for rel, lines in files.items():
        if is_test_path(rel) and not include_tests:
            continue
        for i, l in enumerate(lines):
            # Comments are told apart by the lexer below, not by a leading `*`: a Python
            # continuation line `* node.metadata.get(...)` is code (tested 2026-09-27: a planted
            # score input on such a line went unreported).
            for m in META_READ.finditer(l):
                name = m.group(1) or m.group(2) or m.group(3)
                if not name or name in {"length", "get", "set", "map", "filter", "keys", "values", "items", "toString", "id"}:
                    continue
                # a read, not an assignment target
                after = l[m.end():]
                if re.match(r"\s*=(?!=)", after):
                    continue
                # a read in code, not the same words inside a string, a prompt template or a comment
                # (tested 2026-09-27: prompt prose "...details. For example" became a field `For`)
                src = sources.setdefault(rel, Source(lines, rel.endswith(".py")))
                if not src.is_code(i, m.start()):
                    continue
                obj = re.match(r"\w+", l[m.start():]).group(0)
                bare = not re.search(r"[\w)\]]\??\.$", l[:m.start()])
                reads.setdefault(name, (rel, i + 1, l.strip()[:140], obj if bare else None))
        corpus_parts.append("\n".join(lines))
    corpus = "\n".join(corpus_parts)
    defined_cache: dict[str, bool] = {}

    def defined(fn: str) -> bool:
        if fn not in defined_cache:
            f = re.escape(fn)
            defined_cache[fn] = bool(re.search(
                rf"\b(?:def|function|class)\s+{f}\b"
                rf"|^\s*(?:(?:public|private|protected|static|async|override)\s+)*{f}\s*\([^)\n]*\)\s*(?::[^{{\n]*)?\{{"
                rf"|\b{f}\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)\n]*\)\s*=>)", corpus, re.M))
        return defined_cache[fn]

    for name, (rel, n, snip, obj) in sorted(reads.items()):
        esc = re.escape(name)
        writer = re.search(
            rf"(^|[\s{{,(])[\"']?{esc}[\"']?\s*:(?!:)"          # object literal key  name: / "name":
            rf"|\.{esc}\s*=(?!=)"                               # obj.name = ...
            rf"|\[\s*[\"']{esc}[\"']\s*\]\s*=(?!=)"             # obj["name"] = ...
            rf"|\b{esc}\s*=(?!=)"                               # name = ... (shorthand source)
            # ("name", value) in a setter call. Not any `"name",`: a read's own
            # `.get("name", default)` was its writer (tested 2026-09-27: a planted score input
            # nothing writes went unreported that way).
            rf"|\b(?:set|setdefault|update|put|setattr|setItem|__setitem__)\s*\(\s*(?:[^,()\n]*,\s*)?[\"']{esc}[\"']\s*,",
            corpus, re.M)
        if writer:
            continue
        fn = external_binding(files[rel], obj, n - 1, defined) if obj else None
        if fn:
            out.append(Finding("INFO", "reader-no-writer", rel, n, snip,
                               f"`{name}` is read from what `{fn}()` returns, and `{fn}` isn't defined in this tree, so its writer is a library or another service. Confirm that response really carries it."))
        else:
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


# ------------------------------------------------------------------ text report

def listed_line(f: Finding) -> str:
    return f"  {f.file}:{f.line}  {f.snippet}"


def totals_line(found: list[Finding]) -> str:
    counts = {lv: sum(f.level == lv for f in found) for lv in LEVELS}
    return f"{counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['INFO']} INFO — each is a place to read, not a verdict"


def render_text(found: list[Finding], verbose: bool = False) -> list[str]:
    """The text report. Quiet by default: every HIGH in full, MEDIUM capped per check,
    INFO summarised. `verbose` lists every finding. The totals always count everything."""
    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in found:  # already sorted by level, then check
        groups.setdefault((f.level, f.check), []).append(f)
    out: list[str] = []
    summarised: list[tuple[str, list[Finding]]] = []
    for (level, check), rows in groups.items():
        if level == "INFO" and not verbose:
            summarised.append((check, rows))
            continue
        shown = rows if verbose or level == "HIGH" else rows[:MEDIUM_SHOWN]
        out.append(f"\n[{level}] {check} ({len(rows)})")
        out.append(f"  ask: {rows[0].ask}")
        out.extend(listed_line(f) for f in shown)
        if len(rows) > len(shown):
            out.append(f"  … and {len(rows) - len(shown)} more (use --verbose)")
    if summarised:
        n = sum(len(rows) for _, rows in summarised)
        out.append(f"\n[INFO] {n} finding(s) summarised, not listed (use --verbose to list them)")
        for check, rows in summarised:
            if check == "model-id":
                ids = Counter(f.snippet for f in rows)
                listing = ", ".join(f"{m} ({c})" for m, c in sorted(ids.items()))
                out.append(f"  model-id ({len(rows)}, {len(ids)} distinct): {listing}. {MODEL_ID_ASK}")
            else:
                asks = {f.ask for f in rows}
                ask = asks.pop() if len(asks) == 1 else SUMMARY_ASK.get(check, rows[0].ask)
                out.append(f"  {check} ({len(rows)}): {ask}")
    out.append("\n" + totals_line(found))
    return out


def render_self_test(found: list[Finding]) -> list[tuple[str, bool]]:
    """Prove the quiet report hides nothing it must show. Padded with synthetic findings so
    HIGH exceeds the old 25-per-check cap and MEDIUM exceeds MEDIUM_SHOWN."""
    def fake(level: str, check: str, i: int, snippet: str = "") -> Finding:
        return Finding(level, check, f"synthetic/{check}.ts", i, snippet or f"{check} hit {i}", f"ask for {check}")
    sample = list(found)
    sample += [fake("HIGH", "reader-no-writer", i) for i in range(1, 31)]
    sample += [fake("MEDIUM", "swallowed-error", i) for i in range(1, MEDIUM_SHOWN + 4)]
    sample += [fake("INFO", "metric-coalesce", i) for i in range(1, 5)]
    sample += [fake("INFO", "model-id", i, mid) for i, mid in enumerate(["gpt-4o", "claude-x", "gpt-4o", "gpt-4o"], 1)]
    sample.sort(key=lambda f: (-LEVELS[f.level], f.check, f.file, f.line))
    quiet = render_text(sample)
    loud = render_text(sample, verbose=True)
    quiet_set, loud_set = set(quiet), set(loud)

    every_high = all(listed_line(f) in quiet_set for f in sample if f.level == "HIGH")
    info = [f for f in sample if f.level == "INFO"]
    info_checks = Counter(f.check for f in info if f.check != "model-id")
    summary_ok = bool(info_checks) and all(
        any(l.startswith(f"  {c} ({n}): ") for l in quiet) for c, n in info_checks.items()) \
        and not any(listed_line(f) in quiet_set for f in info)
    ids = Counter(f.snippet for f in info if f.check == "model-id")
    want = ", ".join(f"{m} ({c})" for m, c in sorted(ids.items()))
    models_ok = len(ids) > 1 and any(l.startswith("  model-id (") and want in l for l in quiet)
    medium_checks = {f.check for f in sample if f.level == "MEDIUM"}
    medium_capped = all(
        sum(listed_line(f) in quiet_set for f in sample if f.level == "MEDIUM" and f.check == c) <= MEDIUM_SHOWN
        for c in medium_checks) and any(l.endswith("more (use --verbose)") for l in quiet)
    verbose_all = all(listed_line(f) in loud_set for f in sample)
    totals_ok = quiet[-1].strip() == loud[-1].strip() == totals_line(sample)
    return [("default output lists every HIGH finding", every_high),
            ("INFO summary names each collapsed check with its count", summary_ok),
            ("model ids grouped, distinct, with counts", models_ok),
            (f"MEDIUM collapsed after {MEDIUM_SHOWN} per check", medium_capped),
            ("--verbose lists every finding", verbose_all),
            ("totals count every level, quiet or verbose", totals_ok)]


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
        # Only the `>= 0` patterns can fire in these two files, so each pattern is proven on its own.
        "eval/test_accuracy.py": """
def test_accuracy(results):
    score = sum(r.correct for r in results) / len(results)
    assert score >= 0
""",
        "eval/recall.test.ts": """
expect(passRate).toBeGreaterThanOrEqual(0);
""",
        # Each file below pairs a defect that must be found with a look-alike that must not be,
        # from a measurement on four open-source RAG apps (2026-09-27).
        "cases/meta.py": """
def rank(node, parts, metadata):
    metadata.extend(parts)
    boost = node.metadata.get("source_priority", 1.0)
    return (node.score * boost
            * node.metadata["freshness"])
""",
        "cases/prompt.ts": """
/**
 * Reads metadata.author when present.
 */
const here = import.meta.url;
export const prompt = (doc: any) => `Answer from metadata.summary only.
Include the details. For example, prices.
Title: ${doc.metadata.headline}`;
""",
        "cases/help.tsx": """
export const Help = () => <p>Read the details. Formatting is kept as written.</p>;
""",
        "cases/storage.py": """
def size(s3, path):
    metadata = s3.head_object(Key=path)
    return int(metadata["ContentLength"])
""",
        "cases/usage.py": """
import asyncio


class UsageRecorder:
    async def on_completion(self, settings, row):
        if settings.server.debug_mode:
            await self._repo.insert_usage(row)

    def record(self, row):
        asyncio.create_task(self._repo.insert_usage(row))

    def record_kept(self, row):
        self._task = asyncio.create_task(self._repo.insert_usage(row))

    async def summary(self, text):
        return await self._llm.acomplete(text, max_tokens=256)

    async def title(self, text):
        return await self._llm.acomplete(text)
""",
        "cases/logs.py": """
def report(logger, logging, usage, cost):
    if usage: logger.debug("usage %s", usage)
    if logger.isEnabledFor(logging.DEBUG):
        logger.info("cost %s", cost)
""",
        "cases/loops.py": """
def drain(q):
    while True:  # drain
        item = q.get()
        if item is None:
            break
        run_tool(item)


def poll(q):
    while True:  # poll
        handle(q.get())


def reconnect(conn):
    \"\"\"Reconnects and retries once if the connection died while the query ran.\"\"\"
    return conn
""",
        "cases/llm.ts": """
abstract class BaseLLM {
  abstract generateText(input: GenerateTextInput): Promise<GenerateTextOutput>;
  abstract streamText(
    input: GenerateTextInput,
  ): AsyncGenerator<StreamTextOutput>;
}
interface Provider {
  generateObject(input: GenerateObjectInput): Promise<unknown>;
  streamObject(
    input: GenerateObjectInput,
  ): AsyncGenerator<unknown>;
}
class OpenAILLM {
  async streamText(input: GenerateTextInput): Promise<GenerateTextOutput> {
    return await this.client.chat.completions.create({
      model: this.config.model,
      max_completion_tokens:
        input.options?.maxTokens ?? this.config.options?.maxTokens,
    });
  }
}
""",
        "cases/capped.ts": """
export async function answer(llm: any, opts: any, messages: any) {
  return await generateText({ model: llm, messages, maxTokens: opts?.maxTokens ?? 1024 });
}
""",
        "cases/built.ts": """
export async function summarize(prompt: string) {
  const summaryModel = new ChatOpenAI({
    temperature: 0.3,
    maxTokens: 150,
  });
  const header = 'Summary';
  const response = await summaryModel.invoke(prompt);
  return header + response;
}
export async function route(chatModel: any, prompt: string) {
  return await chatModel.invoke(prompt);
}
export async function handle(chat_service: any, request: any) {
  return await chat_service.chat(request);
}
""",
        "cases/catches.ts": """
export async function ask(llm: any, prompt: string) {
  try {
    return await llm.complete(prompt, { maxTokens: 64 });
  } catch (err) {
    console.error('llm call failed', err);
  }
  fetch('/api/prompt')
    .then((r) => r.json())
    .catch((err) => console.log(err));
}
""",
        "cases/empty_catch.ts": """
export async function rerank(prompt: string) {
  try {
    await scoreWithModel(prompt);
  } catch (err) {
    // tolerated
  }
}
""",
        "cases/meter.ts": """
export function stopLevelMeter(ctx: any) {
  void ctx.close().catch(() => undefined);
}
export function save(chatId: string, tokens: number) {
  saveTokenUsage(chatId, {
    promptTokens: tokens,
  }).catch(() => {});
}
""",
    }
    must_hit = [("eval-cannot-fail", "eval/test_accuracy.py"), ("eval-cannot-fail", "eval/recall.test.ts")]
    # (name, check, file, text in the reported line, must it be reported?, at which level)
    cases = [
        ("a method call is not a field read", "reader-no-writer", "cases/meta.py", "metadata.extend", False, None),
        ("a .get(name, default) is not its own writer", "reader-no-writer", "cases/meta.py", "source_priority", True, "HIGH"),
        ("a read on a `* ...` continuation line is code", "reader-no-writer", "cases/meta.py", "freshness", True, "HIGH"),
        ("a comment is not a read", "reader-no-writer", "cases/prompt.ts", "metadata.author", False, None),
        ("words in a prompt template are not reads","reader-no-writer", "cases/prompt.ts", "metadata.summary", False, None),
        ("prose in a prompt string is not a read", "reader-no-writer", "cases/prompt.ts", "details. For", False, None),
        ("prose in JSX text is not a read", "reader-no-writer", "cases/help.tsx", "details. Formatting", False, None),
        ("import.meta is not metadata", "reader-no-writer", "cases/prompt.ts", "import.meta", False, None),
        ("a read inside ${...} is code", "reader-no-writer", "cases/prompt.ts", "metadata.headline", True, "HIGH"),
        ("a field an external call returns is INFO", "reader-no-writer", "cases/storage.py", "ContentLength", True, "INFO"),
        ("a debug_mode flag gates telemetry", "debug-gated-telemetry", "cases/usage.py", "debug_mode", True, "HIGH"),
        ("a log call or log level is not a debug flag", "debug-gated-telemetry", "cases/logs.py", "if", False, None),
        ("a bare create_task meter write","fire-and-forget-meter", "cases/usage.py", "asyncio.create_task(self._repo", True, "HIGH"),
        ("a kept task is not fire-and-forget", "fire-and-forget-meter", "cases/usage.py", "self._task", False, None),
        ("self._llm.acomplete with no cap", "llm-call-uncapped", "cases/usage.py", "acomplete(text)", True, "MEDIUM"),
        ("self._llm.acomplete with max_tokens", "llm-call-uncapped", "cases/usage.py", "max_tokens=256", False, None),
        ("a loop that ends on a sentinel is not MEDIUM", "loop-uncapped", "cases/loops.py", "# drain", False, "MEDIUM"),
        ("a loop that calls no model or tool is INFO", "loop-uncapped", "cases/loops.py", "# poll", True, "INFO"),
        ("a docstring saying 'retries ... while' is not a loop", "loop-uncapped", "cases/loops.py", "Reconnects", False, None),
        ("an unbounded loop calling a tool is MEDIUM", "loop-uncapped", "src/route.ts", "while (true)", True, "MEDIUM"),
        ("an abstract declaration is not a call", "llm-call-uncapped", "cases/llm.ts", "abstract", False, None),
        ("an interface member is not a call", "llm-call-uncapped", "cases/llm.ts", "generateObject(", False, None),
        ("a member declared over several lines is not a call", "llm-call-uncapped", "cases/llm.ts", "streamObject(", False, None),
        ("a method definition is not a call", "llm-call-uncapped", "cases/llm.ts", "async streamText", False, None),
        ("a cap that can be undefined is no cap", "llm-call-uncapped", "cases/llm.ts", "chat.completions.create", True, "MEDIUM"),
        ("a cap with a default is a cap", "llm-call-uncapped", "cases/capped.ts", "generateText", False, None),
        ("a model built with a cap is capped", "llm-call-uncapped", "cases/built.ts", "summaryModel.invoke", False, None),
        ("a model passed in uncapped is not", "llm-call-uncapped", "cases/built.ts", "chatModel.invoke", True, "MEDIUM"),
        ("a chat service is not a model", "llm-call-uncapped", "cases/built.ts", "chat_service.chat", False, None),
        ("a catch that logs is not empty", "swallowed-error", "cases/catches.ts", "catch", False, None),
        ("a multi-line catch with only a comment is empty", "swallowed-error", "cases/empty_catch.ts", "catch (err)", True, "MEDIUM"),
        ("a level meter's close() is not a usage meter", "fire-and-forget-meter", "cases/meter.ts", "ctx.close", False, None),
        ("a multi-line meter write is reported at its call", "fire-and-forget-meter", "cases/meter.ts", "saveTokenUsage(chatId", True, "HIGH"),
    ]
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
    hit = {(f.check, f.file.replace(os.sep, "/")) for f in found}
    missed_cases = [mh for mh in must_hit if mh not in hit]
    for check, rel in missed_cases:
        print(f"MISS {check} in {rel}")
    failed_cases = 0
    for name, check, rel, text, want, level in cases:
        got = [f for f in found if f.check == check and f.file.replace(os.sep, "/") == rel and text in f.snippet]
        if want:
            passed = any(f.level == level for f in got)
        else:  # must not be reported (at all, or at `level` when one is given)
            passed = not any(level is None or f.level == level for f in got)
        failed_cases += not passed
        print(f"{'ok  ' if passed else 'FAIL'} case: {name}")
    report = render_self_test(found)
    for name, passed in report:
        print(f"{'ok  ' if passed else 'FAIL'} output: {name}")
    bad_output = sum(not passed for _, passed in report)
    ok = expected <= fired and not wrongly and not missed_cases and not bad_output and not failed_cases
    if wrongly:
        print("FALSE POSITIVE: reader-no-writer flagged `similarity`, which src/ingest.ts writes")
    print(f"\nself-test {'passed' if ok else 'FAILED'}: {len(expected & fired)}/{len(expected)} checks fired"
          f"{'' if not wrongly else ', 1 false positive'}"
          f"{'' if not failed_cases else f', {failed_cases} case(s) failed'}"
          f"{'' if not bad_output else f', {bad_output} output check(s) failed'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", type=Path)
    ap.add_argument("--json", action="store_true", help="every finding as JSON (always complete)")
    ap.add_argument("--verbose", action="store_true",
                    help="list every finding (default: every HIGH, the first 5 MEDIUM per check, INFO summarised)")
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
        print("\n".join(render_text(found, a.verbose)))
    if a.fail_on and any(LEVELS[f.level] >= LEVELS[a.fail_on] for f in found):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
