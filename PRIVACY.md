# Privacy

Cairn Signals collects nothing.

- The skill is instructions that Claude reads inside your own session.
- The scanner (`skills/ai-signals-audit/scripts/scan_signals.py`) reads source and data
  files under the path you give it. It prints its findings to your terminal.
- It writes nothing to your project. `--self-test` creates a temporary folder and deletes
  it afterwards.
- It makes no network requests, and it does not call any model or provider. It has no
  telemetry, no analytics, no accounts and no API keys.

The author receives no data about you, your code or your use of the plugin.

Questions: open an issue at https://github.com/nicuk/llm-silent-failure-audit/issues
