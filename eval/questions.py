"""Builds the evaluation set from the database itself, so every expected value is a real statistic."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rag  # noqa: E402

MARKETS = [("BRA", "medtech"), ("USA", "dental"), ("DEU", "imaging"), ("MEX", "consumables")]


def build(collect, meta):
    qs = []
    n = 0
    for iso3, ind_id in MARKETS:
        c = next(x for x in meta["countries"] if x["iso3"] == iso3)
        ind = next(i for i in meta["industries"] if i["id"] == ind_id)
        data = collect(iso3, ind_id)
        series = rag.derive(data["years"])
        m = rag.metrics(series)
        if not m:
            continue
        macro = (data.get("macro") or {}).get("health_gdp", {}).get("series") or []
        partners = (data.get("partners") or {}).get("partners") or []
        names = data.get("names", {})

        def mk(kind, ko, en, expect, gold=None):
            nonlocal n
            ko_mode = n % 2 == 0
            n += 1
            qs.append({"id": f"{iso3}-{ind_id}-{kind}", "type": kind, "market": f"{iso3}/{ind_id}", "lang": "ko" if ko_mode else "en",
                       "question": ko if ko_mode else en, "expect": expect, "gold_kinds": gold})
        cko, cen = c.get("ko") or c["name"], c["name"]
        iko, ien = ind["name"]["ko"], ind["name"]["en"]
        mk("imports_latest", f"{cko} {iko} 시장의 가장 최근 연도 수입액은 얼마야?", f"What were {cen}'s {ien} imports in the latest year?",
           {"kind": "usd", "value": m["last"]["total"]}, ["trade_summary"])
        if m["yoy"] is not None:
            mk("yoy", f"{cko} {iko} 수입액은 전년 대비 얼마나 변했어?", f"How much did {cen}'s {ien} imports change versus the prior year?",
               {"kind": "pct", "value": abs(m["yoy"] * 100)}, ["trade_summary"])
        if m["cagr"] is not None:
            mk("cagr", f"{cko} {iko} 수입의 최근 5년 연평균 성장률은?", f"What is the 5-year average annual growth of {cen}'s {ien} imports?",
               {"kind": "pct", "value": abs(m["cagr"] * 100)}, ["trade_summary"])
        if m["kor_share"] is not None:
            mk("korea_share", f"한국산이 {cko} {iko} 수입에서 차지하는 비중은?", f"What share of {cen}'s {ien} imports comes from South Korea?",
               {"kind": "pct", "value": m["kor_share"] * 100}, ["korea"])
        if partners:
            top = partners[0]["code"]
            nm = names.get(str(top), str(top))
            mk("top_supplier", f"{cko} {iko} 수입에서 1위 공급국은 어디야?", f"Which country is the top supplier of {cen}'s {ien} imports?",
               {"kind": "name", "names": [nm]}, ["partners"])
        if macro:
            mk("health_gdp", f"{cko} {iko} 시장 배경으로, 의료비는 GDP의 몇 %야?", f"For the {cen} {ien} market, what share of GDP goes to health spending?",
               {"kind": "pct", "value": macro[-1][1]}, ["macro"])
        mk("abstain", f"{cko} {iko}의 정확한 내수 시장 규모(매출, USD)는 얼마야?", f"What is the exact domestic market size (sales, USD) of {cen}'s {ien} market?",
           {"kind": "abstain"}, None)
    return qs
