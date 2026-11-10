"""Checks that the scoring itself behaves: grounded answers score high, invented numbers are caught.   python -m unittest discover -s eval"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metrics  # noqa: E402
import questions  # noqa: E402

SOURCES = [
    {"id": "S1", "kind": "trade_summary", "pinned": False,
     "text": "Brazil: imports 2024 $3.60B; 2025 $4.09B. Latest year 2025: $4.09B, +13.7% versus 2024, 5-year CAGR +11.1%."},
    {"id": "S2", "kind": "korea", "pinned": False, "text": "In 2025 Korea supplied $155.7M, 3.81% of total."},
    {"id": "S3", "kind": "macro", "pinned": True, "text": "Brazil health expenditure 9.7% of GDP (2023)."},
]


class Numbers(unittest.TestCase):
    def test_extracts_units_and_ignores_years(self):
        n = metrics.numbers("In 2025 imports were $4.09B (+13.7%) and HS 9018 is 57.0% [S1]")
        self.assertEqual([(x["kind"], round(x["value"], 1)) for x in n], [("usd", 4.09e9), ("pct", 13.7), ("pct", 57.0)])

    def test_same_value_different_formats(self):
        a, b, c = metrics.numbers("$4.09B")[0], metrics.numbers("4,090 million")[0], metrics.numbers("40.9억 달러")[0]
        self.assertTrue(metrics.close(a, b))
        self.assertTrue(metrics.close(a, c))
        self.assertFalse(metrics.close(a, metrics.numbers("$5.5B")[0]))


class Scoring(unittest.TestCase):
    Q = {"type": "imports_latest", "expect": {"kind": "usd", "value": 4.09e9}, "gold_kinds": ["trade_summary"]}

    def test_grounded_answer(self):
        r = metrics.score(self.Q, "Imports reached $4.09B in 2025 [S1]. Korea supplied 3.81% [S2].", SOURCES)
        self.assertEqual(r["grounded"], r["numbers"])
        self.assertEqual(r["citation_supported"], r["sentences_with_numbers"])
        self.assertTrue(r["correct"])
        self.assertTrue(r["retrieved_hit"])

    def test_invented_number_is_caught(self):
        r = metrics.score(self.Q, "Imports reached $5.5B in 2025 [S1].", SOURCES)
        self.assertEqual(r["grounded"], 0)
        self.assertEqual(r["citation_supported"], 0)
        self.assertFalse(r["correct"])

    def test_citation_to_wrong_source(self):
        r = metrics.score(self.Q, "Korea supplied 3.81% of total [S1].", SOURCES)  # true number, wrong source
        self.assertEqual(r["grounded"], 1)
        self.assertEqual(r["citation_supported"], 0)

    def test_missing_citation_and_invalid_id(self):
        r = metrics.score(self.Q, "Imports reached $4.09B. Growth was 11.1% [S9].", SOURCES)
        self.assertEqual(r["cited_sentences"], 1)
        self.assertEqual(r["cite_valid"], 0)

    def test_unpinned_retrieval_hit(self):
        q = {"type": "health_gdp", "expect": {"kind": "pct", "value": 9.7}, "gold_kinds": ["macro"]}
        self.assertFalse(metrics.score(q, "Health spend is 9.7% of GDP [S3].", SOURCES)["retrieved_hit"])  # macro was pinned, not retrieved

    def test_abstain_and_name(self):
        self.assertTrue(metrics.score({"expect": {"kind": "abstain"}}, "The context does not include domestic sales, so I cannot say.", SOURCES)["correct"])
        self.assertFalse(metrics.score({"expect": {"kind": "abstain"}}, "Domestic sales were $12B.", SOURCES)["correct"])
        self.assertTrue(metrics.score({"expect": {"kind": "name", "names": ["China"]}}, "China is the top supplier [S1].", SOURCES)["correct"])


class Builder(unittest.TestCase):
    def test_questions_use_database_values(self):
        meta = {"countries": [{"iso3": i, "name": n, "ko": k} for i, n, k in
                              (("BRA", "Brazil", "브라질"), ("USA", "United States", "미국"), ("DEU", "Germany", "독일"), ("MEX", "Mexico", "멕시코"))],
                "industries": [{"id": i, "name": {"ko": i, "en": i}} for i in ("medtech", "dental", "imaging", "consumables")]}

        def collect(iso3, ind):
            rows = lambda w, k: {"rows": [{"hs": "9018", "world": w, "kor": k}]}  # noqa: E731
            return {"years": {2023: rows(100.0, 1.0), 2024: rows(150.0, 3.0), 2025: rows(180.0, 4.5)},
                    "partners": {"year": 2025, "world": 180.0, "partners": [{"code": 156, "value": 90.0}]},
                    "macro": {"health_gdp": {"series": [[2023, 9.7]]}}, "names": {"156": "China"}}
        qs = questions.build(collect, meta)
        self.assertEqual(len(qs), 28)
        q = next(x for x in qs if x["id"] == "BRA-medtech-imports_latest")
        self.assertEqual(q["expect"]["value"], 180.0)
        y = next(x for x in qs if x["id"] == "BRA-medtech-yoy")
        self.assertAlmostEqual(y["expect"]["value"], 20.0)
        self.assertEqual(next(x for x in qs if x["id"] == "BRA-medtech-top_supplier")["expect"]["names"], ["China"])


if __name__ == "__main__":
    unittest.main()
