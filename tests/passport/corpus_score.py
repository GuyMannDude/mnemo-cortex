"""Score the validator against AL's labeled corpus.

Runs every corpus entry through validate_observation, compares the returned
disposition against the expected label, reports per-file + overall accuracy
plus a confusion matrix so we know WHAT kind of errors the validator makes.

Single-scalar "score" for Karpathy-loop harnesses is macro-F1 across the
four disposition classes. Accuracy alone misleads when classes are
unbalanced.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from passport import storage, validation
from tests.passport.corpus_migrate import iter_corpus


DISPOSITIONS = ("allow", "review_required", "local_only", "hard_block")


def f1_per_class(y_true: list[str], y_pred: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for cls in DISPOSITIONS:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p == cls)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != cls and p == cls)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p != cls)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        out[cls] = f1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mismatches", type=int, default=5, help="Show first N mismatches per file")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary only")
    args = ap.parse_args()

    stable = storage.load_stable()

    results_by_file: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    # (obs_id, expected, got, rationale)

    for source, obs, expected, rationale in iter_corpus():
        vr = validation.validate_observation(obs, stable)
        results_by_file[source].append((obs.observation_id, expected, vr.disposition, rationale))

    # Build stats per file + overall
    summary = {}
    all_true, all_pred = [], []
    for source, rows in results_by_file.items():
        y_true = [r[1] for r in rows]
        y_pred = [r[2] for r in rows]
        all_true.extend(y_true)
        all_pred.extend(y_pred)
        accuracy = sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)
        cm: dict[tuple[str, str], int] = Counter(zip(y_true, y_pred))
        summary[source] = {
            "n": len(rows),
            "accuracy": accuracy,
            "confusion": {f"{t}->{p}": n for (t, p), n in cm.items()},
        }

    overall_accuracy = sum(1 for t, p in zip(all_true, all_pred) if t == p) / len(all_true)
    overall_f1 = f1_per_class(all_true, all_pred)
    macro_f1 = sum(overall_f1.values()) / len(overall_f1)
    summary["overall"] = {
        "n": len(all_true),
        "accuracy": overall_accuracy,
        "macro_f1": macro_f1,
        "f1_per_class": overall_f1,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    for source in ("benign", "toxic", "edge", "adversarial"):
        if source not in summary:
            continue
        s = summary[source]
        print(f"\n=== {source} ({s['n']} entries) ===")
        print(f"  accuracy: {s['accuracy']:.1%}")
        for transition, count in sorted(s["confusion"].items(), key=lambda kv: -kv[1]):
            marker = "✓" if transition.split("->")[0] == transition.split("->")[1] else "✗"
            print(f"    {marker}  {transition:40s}  {count}")
        mismatches = [r for r in results_by_file[source] if r[1] != r[2]][: args.mismatches]
        if mismatches:
            print(f"  first {len(mismatches)} mismatches:")
            for obs_id, exp, got, rat in mismatches:
                print(f"    {obs_id}: expected={exp:20s} got={got:20s}")
                print(f"      rationale: {rat[:100]}")

    print(f"\n=== OVERALL ===")
    print(f"  n={summary['overall']['n']}  accuracy={summary['overall']['accuracy']:.1%}  macro_F1={summary['overall']['macro_f1']:.3f}")
    for cls, f1 in summary['overall']['f1_per_class'].items():
        print(f"    F1({cls:20s}) = {f1:.3f}")


if __name__ == "__main__":
    main()
