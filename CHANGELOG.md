# Changelog

Each release raises `version` in `.claude-plugin/plugin.json` and is tagged `vX.Y.Z`.

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
