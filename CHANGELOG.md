# Changelog

Each release raises `version` in `.claude-plugin/plugin.json` and is tagged `vX.Y.Z`.

## 1.2.0 (2026-09-27)

Measured on four open-source RAG apps (all HIGH findings labeled, MEDIUM sampled with a fixed
seed): precision went from 10.9% to 37.1%, and recall on 16 planted defects from 11 to 16,
with every control still clean. HIGH findings fell from 38 to 16, but none of the 16 was a
confirmed real problem (6 false alarms, 10 unclear), so HIGH is still a lead to check, not a
verdict. Still 11 checks.

**Fixed**
- `reader-no-writer`: a method call is no longer read as a field (`metadata.extend(` was the
  field `exten`); `import.meta`, prose, and words inside strings, prompt templates and
  comments are no longer reads; a `.get("name", default)` no longer counts as its own writer;
  a read on a `* ...` continuation line is no longer skipped as a comment. A field read from
  what an out-of-tree call returns (`s3.head_object(...)`) is INFO, since its writer is a
  library or another service.
- `llm-call-uncapped`: method definitions and declarations are no longer calls; a model
  built with a cap in the same file counts as capped; a cap whose value can be undefined
  (`maxTokens: this.config.options?.maxTokens`) is reported.
- `loop-uncapped`: a loop that ends on a sentinel, or calls no model or tool, is INFO;
  loop words in docstrings are no longer loops.
- `swallowed-error` reads the handler's own block, so a catch that logs isn't empty.
- `fire-and-forget-meter` is reported on the call that writes, and a line mentioning
  "meter" above an unrelated `.close()` no longer makes it a meter.

**Added**
- Python idioms: any `*debug*` flag (`debug_mode`), a bare `asyncio.create_task(...)` or
  `executor.submit(...)` meter write, and `.complete/.chat/.invoke/.stream` (and async
  forms) on a receiver named like a model.
- `--self-test` has a must-find and a must-not-find case for each of these, and each fix
  was broken once to prove its case fails.

## 1.1.0 (2026-09-27)

**Changed**
- A quiet default: every HIGH finding is listed in full, the first five MEDIUM per check,
  and a one-line summary per INFO check, with model ids grouped as distinct ids with counts.
  `--verbose` lists everything; `--json` is unchanged. On a 1,000-file RAG codebase the
  default listing went from 88 findings to 27, with all 15 HIGH kept.
- The trigger description tells Claude to use the skill before investigating any single
  number by hand. On fresh prompts it now triggers 5 of 5, up from 6 of 10, with no false
  triggers.

**Added**
- `evals/`: the rebuildable synthetic fixture and answer key for the with-and-without
  comparison.
- `AGENTS.md`, and a CI step where this repo passes Cairn Memory's audit.
- A case study, linked from the Evidence section.

**Fixed**
- The old 25-per-check cap on text output could hide HIGH findings. It's gone.
- The skill description contained `: `, which a strict YAML parser rejects. CI now parses
  the frontmatter strictly.

## 1.0.0 to 1.0.2 (2026-09-27)

First release, then the Cairn logo and README layout, a check for eval assertions against
`>= 0`, the icon as `.claude-plugin/icon.svg`, and a link to the public principles and
evidence.
