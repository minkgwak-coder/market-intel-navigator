"""Scoring for RAG answers. Pure functions, no network.

Numbers are compared as values (so "$4.09B", "4,090M" and "41억 달러" can match), never as strings.
  number_match   share of numbers in the answer that appear in the retrieved context (within tolerance)
  citation       share of number-bearing sentences that cite a source which really contains those numbers
  correct        the expected value from the database appears in the answer
  abstain        the answer says the data is missing when it should
"""
import re

UNIT = {"T": 1e12, "trillion": 1e12, "B": 1e9, "billion": 1e9, "M": 1e6, "million": 1e6, "K": 1e3, "thousand": 1e3,
        "조": 1e12, "억": 1e8, "만": 1e4}
NUM = re.compile(r"(?P<cur>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<pct>%|퍼센트|percent)?(?P<sp>\s?)(?P<unit>trillion|billion|million|thousand|[TBMK](?![A-Za-z])|조|억|만)?")
CITE = re.compile(r"\[S(\d+)\]")
ABSTAIN = re.compile(r"(not available|not provided|cannot|can't|no data|insufficient|not in the (provided )?context|does not (include|contain)|"
                     r"unavailable|확인할 수 없|알 수 없|근거가 (부족|없)|제공되지|포함되어 있지|없습니다|어렵습니다)", re.I)


def numbers(text):
    """Numbers that carry a unit: $ amounts, magnitudes (M, B, 억 ...) and percentages. Years, HS codes and [S#] are ignored."""
    text = CITE.sub(" ", text)
    out = []
    for m in NUM.finditer(text):
        num = float(m.group("num").replace(",", ""))
        unit = m.group("unit")
        if m.group("pct"):
            out.append({"kind": "pct", "value": abs(num), "raw": m.group(0).strip()})
        elif m.group("cur") or unit:
            out.append({"kind": "usd", "value": abs(num * UNIT.get(unit, 1)), "raw": m.group(0).strip()})
    return out


def close(a, b, tol=0.02):
    if a["kind"] != b["kind"]:
        return False
    if a["kind"] == "pct":
        return abs(a["value"] - b["value"]) <= 0.15 or abs(a["value"] - b["value"]) <= tol * max(b["value"], 1e-9)
    return abs(a["value"] - b["value"]) <= tol * max(b["value"], 1e-9)


def supported(num, pool, tol=0.02):
    return any(close(num, p, tol) for p in pool)


def sentences(text):
    text = text.replace("**", "")
    parts = re.split(r"(?<=[.!?。])\s+(?!\[S)|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def score(question, answer, sources):
    """question: {type, expect, gold_kinds}; sources: [{id, kind, text, pinned}]"""
    by_id = {s["id"]: s for s in sources}
    ctx = [n for s in sources for n in numbers(s["text"])]
    nums = numbers(answer)
    grounded = sum(1 for n in nums if supported(n, ctx))
    sent_num = cited = cite_ok = 0
    cite_ids = cite_valid = 0
    for sent in sentences(answer):
        ids = ["S" + i for i in CITE.findall(sent)]
        cite_ids += len(ids)
        cite_valid += sum(1 for i in ids if i in by_id)
        sn = numbers(sent)
        if not sn:
            continue
        sent_num += 1
        if ids:
            cited += 1
            pool = [n for i in ids if i in by_id for n in numbers(by_id[i]["text"])]
            if all(supported(n, pool) for n in sn):
                cite_ok += 1
    exp = question.get("expect")
    correct = None
    if exp and exp["kind"] in ("usd", "pct"):
        correct = any(close(n, exp, 0.02) for n in nums)
    elif exp and exp["kind"] == "name":
        low = answer.lower()
        correct = any(nm.lower() in low for nm in exp["names"])
    elif exp and exp["kind"] == "abstain":
        correct = bool(ABSTAIN.search(answer))
    gold = question.get("gold_kinds")
    retrieved_hit = None
    if gold:
        retrieved_hit = any(s["kind"] in gold and not s.get("pinned") for s in sources)
    return {"numbers": len(nums), "grounded": grounded, "sentences_with_numbers": sent_num, "cited_sentences": cited,
            "citation_supported": cite_ok, "cite_ids": cite_ids, "cite_valid": cite_valid, "correct": correct,
            "retrieved_hit": retrieved_hit}


def aggregate(rows):
    def ratio(a, b):
        return None if not b else a / b
    tot = lambda k: sum(r["score"][k] for r in rows)  # noqa: E731
    fact = [r for r in rows if r["question"]["expect"]["kind"] != "abstain"]
    ab = [r for r in rows if r["question"]["expect"]["kind"] == "abstain"]
    return {
        "questions": len(rows),
        "number_match_rate": ratio(tot("grounded"), tot("numbers")),
        "numbers_checked": tot("numbers"),
        "citation_accuracy": ratio(tot("citation_supported"), tot("sentences_with_numbers")),
        "citation_coverage": ratio(tot("cited_sentences"), tot("sentences_with_numbers")),
        "citation_validity": ratio(tot("cite_valid"), tot("cite_ids")),
        "numeric_sentences": tot("sentences_with_numbers"),
        "answer_correct_rate": ratio(sum(1 for r in fact if r["score"]["correct"]), len(fact)),
        "factual_questions": len(fact),
        "retrieval_hit_rate": ratio(sum(1 for r in fact if r["score"]["retrieved_hit"]), len([r for r in fact if r["score"]["retrieved_hit"] is not None])),
        "abstention_rate": ratio(sum(1 for r in ab if r["score"]["correct"]), len(ab)),
        "abstention_questions": len(ab),
    }
