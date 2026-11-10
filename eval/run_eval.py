"""Measure the RAG pipeline: citation accuracy, number match rate, answer correctness, retrieval hit rate, abstention.

    pip install -r requirements.txt
    set OPENAI_API_KEY=sk-...            (Mac/Linux: export OPENAI_API_KEY=sk-...)
    python eval/run_eval.py              # 28 questions, writes eval/results.json and eval/RESULTS.md
    python eval/run_eval.py --limit 5    # quick try

The first run collects statistics for four markets (about 30 s each), then every question calls the live pipeline.
"""
import argparse
import json
import os
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import metrics  # noqa: E402
import questions  # noqa: E402
import rag  # noqa: E402
import server  # noqa: E402


def pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    st = rag.status()
    if not st["available"]:
        sys.exit(f"RAG is not available: {st}")
    meta = server.meta()
    qs = questions.build(server.collect_market, meta)
    if args.limit:
        qs = qs[: args.limit]
    rows = []
    for i, q in enumerate(qs, 1):
        t0 = time.time()
        sources, answer, err = [], "", None
        for ev in rag.ask(q["question"], meta, server.collect_market):
            if ev.get("error"):
                err = ev
            elif ev.get("step") == "extract":
                sources = ev["sources"]
            elif ev.get("step") == "answer":
                answer = ev["text"]
        sc = metrics.score(q, answer, sources)
        rows.append({"question": q, "answer": answer, "error": err, "score": sc, "seconds": round(time.time() - t0, 1)})
        print(f"[{i}/{len(qs)}] {q['id']:<34} correct={sc['correct']} numbers={sc['grounded']}/{sc['numbers']}", flush=True)
    agg = metrics.aggregate(rows)
    with open(os.path.join(HERE, "results.json"), "w", encoding="utf8") as f:
        json.dump({"date": date.today().isoformat(), "embed_model": rag.EMBED_MODEL, "chat_model": rag.CHAT_MODEL, "top_k": rag.TOP_K,
                   "aggregate": agg, "rows": rows}, f, ensure_ascii=False, indent=1)
    by_type = {}
    for r in rows:
        by_type.setdefault(r["question"]["type"], []).append(r)
    md = [f"# Evaluation results ({date.today().isoformat()})", "",
          f"Embeddings `{rag.EMBED_MODEL}`, answer model `{rag.CHAT_MODEL}`, top-k {rag.TOP_K}, {agg['questions']} questions.", "",
          "| Metric | Result | Based on |", "|---|---|---|",
          f"| Number match rate (answer numbers found in the retrieved context) | {pct(agg['number_match_rate'])} | {agg['numbers_checked']} numbers |",
          f"| Citation accuracy (cited source really contains the sentence's numbers) | {pct(agg['citation_accuracy'])} | {agg['numeric_sentences']} sentences |",
          f"| Citation coverage (number sentences that cite a source) | {pct(agg['citation_coverage'])} | {agg['numeric_sentences']} sentences |",
          f"| Citation validity (cited ids exist) | {pct(agg['citation_validity'])} | |",
          f"| Answer correct (expected database value appears) | {pct(agg['answer_correct_rate'])} | {agg['factual_questions']} factual questions |",
          f"| Retrieval hit rate (vector search alone retrieves the right fact) | {pct(agg['retrieval_hit_rate'])} | {agg['factual_questions']} factual questions |",
          f"| Abstention (says the data is missing when it is) | {pct(agg['abstention_rate'])} | {agg['abstention_questions']} questions |", "",
          "| Question type | n | Correct | Retrieval hit |", "|---|---|---|---|"]
    for t, rs in by_type.items():
        c = [r for r in rs if r["score"]["correct"] is not None]
        h = [r for r in rs if r["score"]["retrieved_hit"] is not None]
        md.append(f"| {t} | {len(rs)} | {pct(sum(1 for r in c if r['score']['correct']) / len(c) if c else None)} | "
                  f"{pct(sum(1 for r in h if r['score']['retrieved_hit']) / len(h) if h else None)} |")
    wrong = [r for r in rows if r["score"]["correct"] is False]
    if wrong:
        md += ["", "## Questions answered incorrectly", ""] + [f"- `{r['question']['id']}`: {r['question']['question']}" for r in wrong]
    with open(os.path.join(HERE, "RESULTS.md"), "w", encoding="utf8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
