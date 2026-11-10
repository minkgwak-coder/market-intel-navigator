"""Collect real data for the demo markets and store it in demo/demo_cache.sqlite (used by the public demo).

    python warm_cache.py            # about 30 s per market the first time (Comtrade rate limit)
"""
import os
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["MI_CACHE"] = os.path.join(HERE, "demo", "demo_cache.sqlite")
os.environ["MI_STALE_OK"] = "0"
import server  # noqa: E402

MARKETS = [("BRA", "medtech"), ("USA", "dental"), ("DEU", "imaging"), ("VNM", "consumables"), ("MEX", "implants"), ("JPN", "medtech"),
           ("USA", "medtech"), ("DEU", "medtech"), ("MEX", "medtech"), ("BRA", "dental"), ("VNM", "medtech"), ("JPN", "dental")]

if __name__ == "__main__":
    server.meta()
    for iso3, ind in MARKETS:
        t0 = time.time()
        try:
            server.collect_market(iso3, ind)
            print(f"{iso3}/{ind}: ok ({time.time() - t0:.0f}s)", flush=True)
        except Exception as e:  # keep going, report at the end
            print(f"{iso3}/{ind}: FAILED {e}", flush=True)
    server.cache_set("snapshot:date", date.today().isoformat())
    print("snapshot date", date.today().isoformat())
