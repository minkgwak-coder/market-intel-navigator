import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


class PlannerTests(unittest.TestCase):
    def ids(self, q, n=1):
        return [s["id"] for s in agent.make_plan(q, [0] * n, None)]

    def test_entry_question_adds_regulation(self):
        self.assertIn("regulatory", self.ids("Should we enter Brazil?"))

    def test_two_markets_add_comparison(self):
        self.assertIn("compare", self.ids("Brazil vs Japan dental", 2))

    def test_currency_only_when_asked(self):
        self.assertNotIn("fx", self.ids("Which countries lead imports?"))
        self.assertIn("fx", self.ids("What is the local currency price?"))
        self.assertIn("fx", self.ids("환율 반영해줘"))

    def test_internal_notes_always_checked(self):
        self.assertEqual(self.ids("anything")[-1], "internal")


class VerifyTests(unittest.TestCase):
    SRC = [{"id": "S1", "text": "Brazil imports $150.9M in 2025, -6.2% versus 2024, CAGR +8.6%."}]

    def test_clean_answer(self):
        self.assertTrue(agent.verify("Imports were $150.9M [S1], down 6.2% [S1].", self.SRC)["ok"])

    def test_unknown_citation(self):
        self.assertEqual(agent.verify("Imports were $150.9M [S9].", self.SRC)["bad_cites"], ["S9"])

    def test_invented_figure(self):
        r = agent.verify("Imports were $150.9M [S1] and Korea held 12.5% [S1].", self.SRC)
        self.assertEqual(r["ungrounded"], ["12.5%"])
        self.assertFalse(r["ok"])

    def test_uncited_sentence_with_figure(self):
        self.assertEqual(agent.verify("Imports were $150.9M.", self.SRC)["uncited"], 1)


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        agent.NOTES_PATH = os.path.join(self.tmp, "notes.json")

    def test_add_list_delete(self):
        n = agent.add_note("BRA/dental", "sales", "Distributor wants a local warranty.")
        self.assertEqual([x["id"] for x in agent.list_notes("BRA/dental")], [n["id"]])
        self.assertEqual(agent.list_notes("JPN/dental"), [])
        self.assertTrue(agent.delete_note(n["id"]))
        self.assertEqual(agent.list_notes(), [])

    def test_rejects_bad_input(self):
        for args in (("brazil", "sales", "long enough text"), ("BRA/dental", "boss", "long enough text"), ("BRA/dental", "cs", "no")):
            with self.assertRaises(ValueError):
                agent.add_note(*args)

    def test_notes_become_sources(self):
        agent.add_note("BRA/dental", "lab", "Our unit passed the local electrical test.")
        plan = agent.make_plan("anything", [0], None)
        calls, src = agent.run_tools(plan, [("BRA/dental", {"iso3": "BRA", "name": "Brazil"})], {})
        self.assertEqual(src[0]["kind"], "internal")
        self.assertIn("lab", src[0]["text"])


class ToolTests(unittest.TestCase):
    def test_regulatory_known_and_unknown(self):
        self.assertEqual(agent.regulatory("BRA")["authority"], "ANVISA")
        self.assertIsNone(agent.regulatory("ZZZ"))

    def test_fx_uses_live_rate_then_caches(self):
        agent._fx_cache.clear()
        calls = []

        def fake(url):
            calls.append(url)
            return {"date": "2026-01-02", "rates": {"BRL": 5.5}}
        self.assertEqual(agent.fx_rate("BRL", fake)[0], 5.5)
        agent.fx_rate("BRL", fake)
        self.assertEqual(len(calls), 1)

    def test_fx_converts_the_import_total(self):
        agent._fx_cache.clear()
        plan = [{"id": "fx", "tools": ["fx"]}]
        fake = lambda url: {"date": "2026-01-02", "rates": {"BRL": 5.0}}  # noqa: E731
        _, src = agent.run_tools(plan, [("BRA/dental", {"iso3": "BRA", "name": "Brazil"})],
                                 {"BRA/dental": {"usd": "$100.0M", "usd_raw": 1e8}}, _fetch=fake)
        self.assertIn("5 BRL", src[0]["text"])


if __name__ == "__main__":
    unittest.main()
