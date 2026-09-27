#!/usr/bin/env python3
"""
build_fixture.py - rebuild the synthetic "contractlens" app used by the published evals.

Usage:
  python build_fixture.py OUT_DIR

Creates OUT_DIR/contractlens/, a tiny made-up AI contract reviewer (TypeScript, not meant to
run) with 8 planted defects in the numbers it shows, and 1 decoy that handles its failure
correctly. The app, its name and its claims are invented.

Deterministic and stdlib only: the same bytes every time. The answer key is evals.json next
to this script; it is never copied into OUT_DIR.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_NAME = "contractlens"

# 40 golden cases: 15 runnable, 25 still holding a placeholder tenant, so they self-skip.
GOLDEN = {"cases": [
    {"id": i, "tenant": "t-demo" if i < 15 else "REPLACE_WITH_TENANT", "question": f"q{i}", "expected": f"a{i}"}
    for i in range(40)
]}

FILES = {
    "README.md": """# ContractLens
AI contract reviewer. Evaluated on 40 real contracts: **95% accuracy**, zero hallucinations.
Every answer shows a confidence score; answers above 80% are marked "Verified".
""",
    "app/api/chat/route.ts": """import { generateText } from 'ai';
import { confidence } from '../../../lib/confidence';
import { recordUsage } from '../../../lib/usage';
const MODELS = ['openai/gpt-4-32k', 'anthropic/claude-sonnet-5'];
export async function POST(req: Request) {
  const url = new URL(req.url);
  const debug = url.searchParams.get('debug') === '1';
  const { question, chunks, queryId } = await req.json();
  let answer = '';
  let attempt = 0;
  while (true) {
    try {
      const r = await generateText({ model: MODELS[attempt % MODELS.length], prompt: question + chunks.map((c: any) => c.content).join('\\n') });
      answer = r.text;
      if (debug) { recordUsage(queryId, r.usage.inputTokens, r.usage.outputTokens, r.usage.inputTokens * 3e-6 + r.usage.outputTokens * 15e-6); }
      break;
    } catch { attempt++; }
  }
  const conf = Math.max(...chunks.map(confidence));
  return Response.json({ answer, confidence: conf, verified: conf >= 0.8 });
}
""",
    "app/dashboard/page.tsx": """import { pendingReviews, documentCount } from '../../lib/stats';
import { costPerQueryToday } from '../../lib/usage';
export default async function Dashboard({ orgId }: { orgId: string }) {
  const [pending, docs, cost] = await Promise.all([pendingReviews(orgId), documentCount(orgId), costPerQueryToday()]);
  return (<div><p>{pending} contracts awaiting review</p><p>{docs ?? 'unavailable'} documents</p><p>${cost.toFixed(4)} per query today</p></div>);
}
""",
    # CRLF because the original was written on Windows; kept so the rebuild is byte-identical to
    # the fixture the published runs saw. The scanner and the prompts don't care either way.
    "eval/golden.json": json.dumps(GOLDEN, indent=1).replace("\n", "\r\n"),
    "eval/run.test.ts": """import golden from './golden.json';
const threshold = 0;
it('accuracy', async () => {
  const runnable = golden.cases.filter((c: any) => !c.tenant.startsWith('REPLACE'));
  let correct = 0;
  for (const c of runnable) { /* call the app, compare */ correct++; }
  expect(correct / runnable.length).toBeGreaterThanOrEqual(threshold);
});
""",
    "ingest/chunk.ts": """export function toChunk(text: string, sim: number, pageNo: number) {
  return { content: text, metadata: { similarity: sim, page: pageNo } };
}
""",
    "lib/confidence.ts": """// Confidence shown to the user; >= 0.8 renders the "Verified" badge.
export function confidence(chunk: { content: string; metadata: any }) {
  let score = 0.5 * (chunk.metadata.similarity ?? 0);
  if (chunk.metadata.source_quality === 'primary') score += 0.25;
  if (chunk.metadata.clause_type) score += 0.15;
  score += Math.min(chunk.content.length / 4000, 0.1);
  return score;
}
""",
    "lib/db.ts": """export const db: any = {};
""",
    "lib/stats.ts": """import { db } from './db';
export async function pendingReviews(orgId: string): Promise<number> {
  try {
    return await db.count('reviews', { orgId, status: 'pending' });
  } catch {
    return 0;
  }
}
// decoy: this one is handled correctly
export async function documentCount(orgId: string): Promise<number | null> {
  try {
    return await db.count('documents', { orgId });
  } catch (err) {
    console.error('documentCount failed', { orgId, err });
    return null;
  }
}
""",
    "lib/usage.ts": """import { db } from './db';
export function recordUsage(queryId: string, inputTokens: number, outputTokens: number, costUsd: number) {
  db.insert('usage_events', { queryId, inputTokens, outputTokens, costUsd }).catch(() => {});
}
export async function costPerQueryToday(): Promise<number> {
  const rows = await db.select('usage_events', { since: 'today' });
  return rows.reduce((s: number, r: any) => s + r.costUsd, 0) / Math.max(rows.length, 1);
}
""",
}


def build(out_dir: Path) -> Path:
    target = out_dir / FIXTURE_NAME
    if target.exists():
        raise SystemExit(f"{target} already exists; pick an empty OUT_DIR so no run reuses an old copy")
    for rel, body in FILES.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode("utf-8"))  # bytes, so line endings are LF on every OS
    return target


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = build(Path(sys.argv[1]))
    print(f"fixture at {target} ({len(FILES)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
