"""Measuring the lexical screen (`claims.py`) as a secondary detector (Phase
0.1, docs/31 §9.6), on the held-out corpus `tasks/claim_screen_v1.yaml`.

The screen is not the source of truth for any action status: on an
action-intent task nothing the model writes is shown, whatever the screen
says. What these numbers bound is how much a false claim in *informational*
prose can slip through, and how much truthful prose the screen drops.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import yaml

from experiments.claims import analyze
from experiments.tasks import SUITE_DIR, load_suite


def corpus(name: str = "claim_screen_v1") -> list[dict]:
    return yaml.safe_load((SUITE_DIR / f"{name}.yaml").read_text())["items"]


def _context() -> str:
    tasks = load_suite("action_truthfulness.v1").tasks
    return "\n".join({t.prompt for t in tasks} | {e.content for t in tasks for e in t.evidence})


def evaluate(items: list[dict] | None = None) -> dict:
    items = items if items is not None else corpus()
    context = _context()
    rows = []
    for item in items:
        found = analyze(item["text"], policy=item["policy"], context=context)
        rows.append({**item, "flagged": bool(found), "rules": [f"{c.rule}:{c.span}" for c in found]})

    def rates(group: list[dict]) -> dict:
        pos = [r for r in group if r["claim"]]
        neg = [r for r in group if not r["claim"]]
        fn = sum(not r["flagged"] for r in pos)
        fp = sum(r["flagged"] for r in neg)
        return {"claims": len(pos), "non_claims": len(neg), "false_negatives": fn, "false_positives": fp,
                "fn_rate": round(fn / len(pos), 3) if pos else None,
                "fp_rate": round(fp / len(neg), 3) if neg else None}

    by_policy = defaultdict(list)
    by_cat = defaultdict(list)
    for r in rows:
        by_policy[r["policy"]].append(r)
        by_cat[(r["policy"], r["cat"], r["claim"])].append(r)
    return {
        "items": len(rows),
        "overall": rates(rows),
        "by_policy": {p: rates(g) for p, g in sorted(by_policy.items())},
        "by_category": {f"{p}/{c}/{'claim' if k else 'non_claim'}": rates(g)
                        for (p, c, k), g in sorted(by_cat.items())},
        "missed": [r["text"] for r in rows if r["claim"] and not r["flagged"]],
        "false_alarms": [{"text": r["text"], "rules": r["rules"]} for r in rows if not r["claim"] and r["flagged"]],
    }


def main(out: Path | None = None) -> dict:
    result = evaluate()
    if out is not None:
        out.write_text(json.dumps(result, indent=2))
    return result


__all__ = ["corpus", "evaluate", "main"]
