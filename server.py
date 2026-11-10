#!/usr/bin/env python3
"""Global Market Intelligence Navigator - local server.

Run:  python server.py        then open http://127.0.0.1:8000
No third-party packages needed (Python 3.9+).

Real data sources, fetched live and cached in cache.sqlite:
  - UN Comtrade public API  : imports by HS code, total and from Korea, partner breakdown
  - World Bank API          : GDP, GDP per capita, health spending, population, 65+ share
  - Google News RSS / GDELT : recent news headlines (GDELT is the fallback)
Optional: set ANTHROPIC_API_KEY or OPENAI_API_KEY to enable the LLM summary button.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import rag
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DB_PATH = os.environ.get("MI_CACHE", os.path.join(HERE, "cache.sqlite"))
HOST = os.environ.get("HOST", "127.0.0.1")
# Public demo: frozen data snapshot, no LLM endpoints (no API cost), per-IP rate limit.
PUBLIC = os.environ.get("MI_PUBLIC") == "1"
STALE_OK = PUBLIC or os.environ.get("MI_STALE_OK") == "1"
DEMO_CACHE = os.path.join(HERE, "demo", "demo_cache.sqlite")

COMTRADE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
COMTRADE_REPORTERS = "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json"
WORLDBANK = "https://api.worldbank.org/v2"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

DAY = 86400
KOREA = 410

# ----------------------------------------------------------------------------
# Industries: HS codes are the backbone. Edit freely, or type "hs 901841,9018" in the search box.
# ----------------------------------------------------------------------------
INDUSTRIES = [
    {"id": "medtech", "name": {"en": "Dental, medical & vet devices", "ko": "치과·의료·동물용 기기"},
     "kw": ["dental, medical & vet devices", "치과·의료·동물용 기기", "medical device", "medical devices", "medical equipment", "medtech",
            "veterinary", "vet", "animal health", "동물용", "동물용 의료기기", "수의", "의료기기", "의료 기기", "의료기"],
     "news": ["dental", "\"medical devices\"", "\"veterinary equipment\"", "\"veterinary devices\""],
     "note": {"ko": "동물용 의료기기는 HS 코드가 따로 없어서, 의료·치과·동물용이 함께 쓰는 공용 코드로 합산합니다. 동물용만의 규모는 이 통계로 알 수 없으니 뉴스와 AI 질문으로 보완하세요.",
              "en": "Veterinary devices have no HS codes of their own, so the totals use the codes shared by medical, dental and veterinary products. The veterinary share cannot be read from these statistics; use the news and Ask AI to fill the gap."},
     "hs": [
         {"code": "9018", "en": "Medical, surgical, dental and veterinary instruments (diagnostics, syringes, catheters, dental tools)", "ko": "의료·외과·치과·수의용 기기 (진단기, 주사기, 카테터, 치과 기구 등)"},
         {"code": "9019", "en": "Therapy and respiration apparatus (oxygen, ventilators)", "ko": "치료·호흡 기기 (산소, 인공호흡기 등)"},
         {"code": "9021", "en": "Orthopaedic appliances, implants, hearing aids, pacemakers, artificial teeth", "ko": "정형·이식형 기기, 보청기, 심박동기, 인공치아"},
         {"code": "9022", "en": "X-ray, CT and radiation apparatus (medical, dental, veterinary)", "ko": "X선·CT·방사선 장치 (의료·치과·수의용)"},
         {"code": "9402", "en": "Medical, surgical, dental and veterinary furniture (exam tables, dental chairs)", "ko": "의료·치과·수의용 가구 (진료대, 치과 의자 등)"},
         {"code": "3005", "en": "Wadding, gauze and bandages for medical use", "ko": "의료용 탈지면, 거즈, 붕대"}]},
    {"id": "dental", "name": {"en": "Dental", "ko": "치과"},
     "kw": ["dental", "dentistry", "dentist", "dentists", "teeth", "치과", "덴탈", "임플란트", "implant"],
     "news": ["dental", "dentistry", "dental implants"],
     "hs": [
         {"code": "901841", "en": "Dental drill engines", "ko": "치과용 드릴 엔진"},
         {"code": "901849", "en": "Other dental instruments", "ko": "기타 치과용 기구"},
         {"code": "902121", "en": "Artificial teeth", "ko": "인공치아"},
         {"code": "902129", "en": "Other dental fittings (incl. dental implants)", "ko": "기타 치과용 보철물 (임플란트 포함)"},
         {"code": "330610", "en": "Dentifrices", "ko": "치약"},
         {"code": "300640", "en": "Dental cements and fillings", "ko": "치과용 시멘트, 충전재"},
         {"code": "940210", "en": "Dentists' and barbers' chairs", "ko": "치과용 의자"}]},
    {"id": "imaging", "name": {"en": "Diagnostic imaging", "ko": "영상진단기기"},
     "kw": ["diagnostic imaging", "imaging", "ultrasound", "x-ray", "mri", "영상진단", "초음파", "엑스레이", "엑스선"],
     "news": ["\"diagnostic imaging\"", "ultrasound", "\"x-ray\""],
     "hs": [
         {"code": "9022", "en": "X-ray, CT and radiation apparatus", "ko": "X선·CT·방사선 장치"},
         {"code": "901812", "en": "Ultrasonic scanning apparatus", "ko": "초음파 진단기"},
         {"code": "901813", "en": "Magnetic resonance imaging (MRI) apparatus", "ko": "MRI 장치"},
         {"code": "901814", "en": "Scintigraphic apparatus", "ko": "신티그래피 장치"}]},
    {"id": "consumables", "name": {"en": "Syringes, catheters & consumables", "ko": "주사기·카테터·소모품"},
     "kw": ["syringe", "syringes", "catheter", "catheters", "consumables", "gauze", "suture", "주사기", "카테터", "소모품", "거즈", "봉합"],
     "news": ["syringes", "catheters", "\"medical consumables\""],
     "hs": [
         {"code": "901831", "en": "Syringes", "ko": "주사기"},
         {"code": "901832", "en": "Tubular metal needles and suture needles", "ko": "금속 주사침, 봉합침"},
         {"code": "901839", "en": "Catheters, cannulae and similar", "ko": "카테터, 캐뉼라 등"},
         {"code": "3005", "en": "Wadding, gauze and bandages for medical use", "ko": "의료용 탈지면, 거즈, 붕대"},
         {"code": "300610", "en": "Sterile sutures and tissue adhesives", "ko": "멸균 봉합사, 조직 접착제"}]},
    {"id": "implants", "name": {"en": "Orthopedic, implants & hearing aids", "ko": "정형·이식형·보청기"},
     "kw": ["orthopedic", "orthopaedic", "hearing aid", "hearing aids", "pacemaker", "prosthetics", "정형", "보청기", "심박동기", "인공관절", "보철"],
     "news": ["orthopedic", "\"hearing aids\"", "\"medical implants\""],
     "hs": [
         {"code": "902110", "en": "Orthopaedic and fracture appliances", "ko": "정형외과, 골절 치료 기기"},
         {"code": "902131", "en": "Artificial joints", "ko": "인공관절"},
         {"code": "902139", "en": "Other artificial body parts", "ko": "기타 인공 신체 부위"},
         {"code": "902140", "en": "Hearing aids", "ko": "보청기"},
         {"code": "902150", "en": "Pacemakers", "ko": "심박동기"},
         {"code": "902190", "en": "Other appliances worn or implanted in the body", "ko": "기타 착용형·이식형 기기"}]},
]

WB_INDICATORS = {
    "gdp": "NY.GDP.MKTP.CD",
    "gdp_pc": "NY.GDP.PCAP.CD",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "pop": "SP.POP.TOTL",
    "pop65": "SP.POP.65UP.TO.ZS",
    "health_gdp": "SH.XPD.CHEX.GD.ZS",
    "health_pc": "SH.XPD.CHEX.PC.CD",
}

NAME_FIX = {
    "Korea, Rep.": "South Korea", "Korea, Dem. People's Rep.": "North Korea",
    "Egypt, Arab Rep.": "Egypt", "Russian Federation": "Russia", "Iran, Islamic Rep.": "Iran",
    "Venezuela, RB": "Venezuela", "Slovak Republic": "Slovakia", "Hong Kong SAR, China": "Hong Kong",
    "Macao SAR, China": "Macao", "Lao PDR": "Laos", "Gambia, The": "Gambia", "Bahamas, The": "Bahamas",
    "Yemen, Rep.": "Yemen", "Congo, Dem. Rep.": "DR Congo", "Congo, Rep.": "Congo", "Kyrgyz Republic": "Kyrgyzstan",
    "Turkiye": "Türkiye", "Viet Nam": "Vietnam", "Syrian Arab Republic": "Syria", "Brunei Darussalam": "Brunei",
    "Cabo Verde": "Cape Verde", "Micronesia, Fed. Sts.": "Micronesia", "St. Lucia": "Saint Lucia",
}
EXTRA_ALIASES = {
    "USA": ["us", "usa", "u.s.", "america", "united states of america", "미국", "아메리카"],
    "GBR": ["uk", "u.k.", "britain", "great britain", "england", "영국"],
    "ARE": ["uae", "emirates", "아랍에미리트", "아랍에미레이트", "두바이"],
    "KOR": ["korea", "한국", "대한민국"],
    "RUS": ["러시아"], "CHN": ["중국"], "JPN": ["일본"], "DEU": ["독일", "germany"], "FRA": ["프랑스"],
    "ITA": ["이탈리아"], "ESP": ["스페인"], "IND": ["인도"], "BRA": ["브라질", "brasil"], "MEX": ["멕시코", "méxico"],
    "COL": ["콜롬비아"], "CHL": ["칠레"], "ARG": ["아르헨티나"], "PER": ["페루"], "CAN": ["캐나다"], "AUS": ["호주", "오스트레일리아"],
    "NZL": ["뉴질랜드"], "TUR": ["튀르키예", "터키", "turkey", "türkiye"], "SAU": ["사우디아라비아", "사우디"],
    "EGY": ["이집트"], "ZAF": ["남아프리카공화국", "남아공", "south africa"], "NGA": ["나이지리아"], "KEN": ["케냐"],
    "IDN": ["인도네시아"], "MYS": ["말레이시아"], "SGP": ["싱가포르"], "THA": ["태국"], "VNM": ["베트남", "viet nam"],
    "PHL": ["필리핀"], "PAK": ["파키스탄"], "BGD": ["방글라데시"], "ISR": ["이스라엘"], "IRN": ["이란"], "IRQ": ["이라크"],
    "POL": ["폴란드"], "NLD": ["네덜란드", "holland"], "BEL": ["벨기에"], "CHE": ["스위스"], "SWE": ["스웨덴"],
    "NOR": ["노르웨이"], "DNK": ["덴마크"], "FIN": ["핀란드"], "AUT": ["오스트리아"], "PRT": ["포르투갈"],
    "GRC": ["그리스"], "CZE": ["체코", "czech republic"], "HUN": ["헝가리"], "ROU": ["루마니아"], "UKR": ["우크라이나"],
    "KAZ": ["카자흐스탄"], "UZB": ["우즈베키스탄"], "MNG": ["몽골"], "HKG": ["홍콩"], "ECU": ["에콰도르"],
    "VEN": ["베네수엘라"], "URY": ["우루과이"], "PRY": ["파라과이"], "BOL": ["볼리비아"], "CRI": ["코스타리카"],
    "PAN": ["파나마"], "DOM": ["도미니카공화국", "도미니카"], "GTM": ["과테말라"], "MAR": ["모로코"], "DZA": ["알제리"],
    "TUN": ["튀니지"], "ETH": ["에티오피아"], "GHA": ["가나"], "TZA": ["탄자니아"], "QAT": ["카타르"],
    "KWT": ["쿠웨이트"], "OMN": ["오만"], "JOR": ["요르단"], "LBN": ["레바논"], "LKA": ["스리랑카"], "NPL": ["네팔"],
    "MMR": ["미얀마"], "KHM": ["캄보디아"], "LAO": ["라오스"], "IRL": ["아일랜드"], "SVK": ["슬로바키아"],
    "BGR": ["불가리아"], "HRV": ["크로아티아"], "SRB": ["세르비아"], "LTU": ["리투아니아"], "LVA": ["라트비아"],
    "EST": ["에스토니아"], "ISL": ["아이슬란드"], "SVN": ["슬로베니아"], "BLR": ["벨라루스"], "AZE": ["아제르바이잔"],
    "GEO": ["조지아"], "ARM": ["아르메니아"], "BHR": ["바레인"], "UGA": ["우간다"], "SEN": ["세네갈"], "CIV": ["코트디부아르"],
    "AGO": ["앙골라"], "MOZ": ["모잠비크"], "ZMB": ["잠비아"], "COD": ["콩고민주공화국"], "CMR": ["카메룬"],
    "SLV": ["엘살바도르"], "HND": ["온두라스"], "NIC": ["니카라과"], "CUB": ["쿠바"], "JAM": ["자메이카"], "TTO": ["트리니다드토바고"],
}
KO_NAMES = {
    "USA": "미국", "GBR": "영국", "DEU": "독일", "FRA": "프랑스", "ITA": "이탈리아", "ESP": "스페인", "JPN": "일본",
    "CHN": "중국", "KOR": "한국", "IND": "인도", "BRA": "브라질", "MEX": "멕시코", "COL": "콜롬비아", "CHL": "칠레",
    "ARG": "아르헨티나", "PER": "페루", "CAN": "캐나다", "AUS": "호주", "NZL": "뉴질랜드", "RUS": "러시아",
    "TUR": "튀르키예", "SAU": "사우디아라비아", "ARE": "아랍에미리트", "EGY": "이집트", "ZAF": "남아프리카공화국",
    "NGA": "나이지리아", "KEN": "케냐", "IDN": "인도네시아", "MYS": "말레이시아", "SGP": "싱가포르", "THA": "태국",
    "VNM": "베트남", "PHL": "필리핀", "PAK": "파키스탄", "BGD": "방글라데시", "ISR": "이스라엘", "IRN": "이란",
    "IRQ": "이라크", "POL": "폴란드", "NLD": "네덜란드", "BEL": "벨기에", "CHE": "스위스", "SWE": "스웨덴",
    "NOR": "노르웨이", "DNK": "덴마크", "FIN": "핀란드", "AUT": "오스트리아", "PRT": "포르투갈", "GRC": "그리스",
    "CZE": "체코", "HUN": "헝가리", "ROU": "루마니아", "UKR": "우크라이나", "KAZ": "카자흐스탄", "UZB": "우즈베키스탄",
    "MNG": "몽골", "HKG": "홍콩", "ECU": "에콰도르", "VEN": "베네수엘라", "URY": "우루과이", "PRY": "파라과이",
    "BOL": "볼리비아", "CRI": "코스타리카", "PAN": "파나마", "DOM": "도미니카공화국", "GTM": "과테말라", "MAR": "모로코",
    "DZA": "알제리", "TUN": "튀니지", "ETH": "에티오피아", "GHA": "가나", "TZA": "탄자니아", "QAT": "카타르",
    "KWT": "쿠웨이트", "OMN": "오만", "JOR": "요르단", "LBN": "레바논", "LKA": "스리랑카", "NPL": "네팔",
    "MMR": "미얀마", "KHM": "캄보디아", "LAO": "라오스", "IRL": "아일랜드", "SVK": "슬로바키아", "BGR": "불가리아",
    "HRV": "크로아티아", "SRB": "세르비아", "LTU": "리투아니아", "LVA": "라트비아", "EST": "에스토니아", "ISL": "아이슬란드",
    "SVN": "슬로베니아", "BLR": "벨라루스", "AZE": "아제르바이잔", "GEO": "조지아", "ARM": "아르메니아", "BHR": "바레인",
    "UGA": "우간다", "SEN": "세네갈", "CIV": "코트디부아르", "AGO": "앙골라", "MOZ": "모잠비크", "ZMB": "잠비아",
    "CMR": "카메룬", "SLV": "엘살바도르", "HND": "온두라스", "NIC": "니카라과", "CUB": "쿠바", "JAM": "자메이카",
}


class ApiError(Exception):
    pass


# ----------------------------------------------------------------------------
# cache + http
# ----------------------------------------------------------------------------
_db_lock = threading.Lock()


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
    return con


def cache_get(key, ttl):
    with _db_lock:
        con = _db()
        try:
            row = con.execute("SELECT v, ts FROM cache WHERE k=?", (key,)).fetchone()
        finally:
            con.close()
    if row and (STALE_OK or time.time() - row[1] < ttl):
        return json.loads(row[0])
    return None


def cache_set(key, value):
    with _db_lock:
        con = _db()
        try:
            con.execute("INSERT OR REPLACE INTO cache (k, v, ts) VALUES (?,?,?)", (key, json.dumps(value), time.time()))
            con.commit()
        finally:
            con.close()


def http_request(url, data=None, headers=None, timeout=60):
    h = {"User-Agent": "market-intel/1.0", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ApiError(f"network error: {e}")


class Throttle:
    def __init__(self, gap):
        self.gap, self.lock, self.last = gap, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            d = self.last + self.gap - time.time()
            if d > 0:
                time.sleep(d)
            self.last = time.time()


TH_COMTRADE = Throttle(1.4)
TH_GDELT = Throttle(5.5)


# ----------------------------------------------------------------------------
# data sources
# ----------------------------------------------------------------------------
def meta():
    cached = cache_get("meta:v2", 30 * DAY)
    if cached:  # countries and names are cached; industries and flags always come from the code
        cached["industries"] = INDUSTRIES
        cached["ai"] = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")) and not PUBLIC
        cached["public"] = PUBLIC
        cached["snapshot"] = cache_get("snapshot:date", 10 ** 12)
        return cached
    st, body = http_request(COMTRADE_REPORTERS)
    if st != 200:
        raise ApiError(f"Comtrade reporters {st}")
    m49 = {}
    names49 = {}
    for r in json.loads(body)["results"]:
        iso3 = r.get("reporterCodeIsoAlpha3")
        if not iso3 or r.get("isGroup") or r.get("entryExpiredDate"):
            continue
        m49[iso3] = r["reporterCode"]
        names49[str(r["reporterCode"])] = r.get("reporterDesc") or r.get("text")
    st, body = http_request(f"{WORLDBANK}/country?format=json&per_page=400")
    if st != 200:
        raise ApiError(f"World Bank countries {st}")
    countries = []
    for c in json.loads(body)[1]:
        iso3 = c["id"]
        if c["region"]["value"] == "Aggregates" or iso3 not in m49:
            continue
        raw = c["name"]
        name = NAME_FIX.get(raw, raw.split(",")[0] if "," in raw else raw)
        aliases = {name.lower(), raw.lower()}
        aliases.update(a.lower() for a in EXTRA_ALIASES.get(iso3, []))
        ko = KO_NAMES.get(iso3)
        if ko:
            aliases.add(ko)
        countries.append({"iso3": iso3, "m49": m49[iso3], "name": name, "ko": ko, "aliases": sorted(aliases),
                          "region": c["region"]["value"].strip(), "income": c["incomeLevel"]["value"]})
    countries.sort(key=lambda c: c["name"])
    # names for trade partners: prefer the cleaned World Bank name, fall back to Comtrade's name
    names = dict(names49)
    for c in countries:
        names[str(c["m49"])] = c["name"]
    names["0"] = "World"
    out = {"countries": countries, "names": names, "industries": INDUSTRIES, "korea": KOREA,
           "ai": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")) and not PUBLIC,
           "public": PUBLIC, "snapshot": cache_get("snapshot:date", 10 ** 12)}
    cache_set("meta:v2", out)
    return out


def macro(iso3):
    key = f"macro:{iso3}"
    cached = cache_get(key, 3 * DAY)
    if cached:
        return cached
    out = {}
    this_year = datetime.now().year
    for name, code in WB_INDICATORS.items():
        url = f"{WORLDBANK}/country/{iso3}/indicator/{code}?format=json&per_page=100&date=2005:{this_year}"
        st, body = http_request(url)
        if st != 200:
            raise ApiError(f"World Bank {code}: {st}")
        payload = json.loads(body)
        rows = payload[1] if len(payload) > 1 and payload[1] else []
        series = sorted([[int(r["date"]), r["value"]] for r in rows if r["value"] is not None])
        out[name] = {"code": code, "series": series}
    cache_set(key, out)
    return out


def comtrade_get(params):
    url = COMTRADE + "?" + urllib.parse.urlencode(params, safe=",")
    for attempt in range(6):
        TH_COMTRADE.wait()
        st, body = http_request(url)
        if st == 200:
            return json.loads(body)
        if st == 429:
            time.sleep(4 + 4 * attempt)
            continue
        raise ApiError(f"Comtrade {st}: {body[:160].decode('utf8', 'replace')}")
    raise ApiError("Comtrade is rate limiting requests. Try again in a minute.")


def _clean(rows):
    return [r for r in rows if r.get("customsCode") == "C00" and r.get("motCode") == 0 and r.get("partner2Code") == 0]


def _ttl_for(year):
    return 60 * DAY if year <= datetime.now().year - 2 else 7 * DAY


def trade_year(reporter, hs, year):
    key = f"trade:{reporter}:{year}:{','.join(hs)}"
    cached = cache_get(key, _ttl_for(year))
    if cached is not None:
        return cached
    data = comtrade_get({"reporterCode": reporter, "period": year, "cmdCode": ",".join(hs), "flowCode": "M",
                         "partnerCode": f"0,{KOREA}"})
    rows = {}
    for r in _clean(data.get("data", [])):
        d = rows.setdefault(r["cmdCode"], {"hs": r["cmdCode"], "world": None, "kor": None})
        if r["partnerCode"] == 0:
            d["world"] = r["primaryValue"]
        elif r["partnerCode"] == KOREA:
            d["kor"] = r["primaryValue"]
    out = {"year": year, "rows": list(rows.values())}
    if not out["rows"]:
        # empty years are cheap to keep but may fill in later
        cache_set(key, out)
        return out
    cache_set(key, out)
    return out


def trade_partners(reporter, hs, year):
    key = f"partners:{reporter}:{year}:{','.join(hs)}"
    cached = cache_get(key, _ttl_for(year))
    if cached is not None:
        return cached
    base = {"reporterCode": reporter, "period": year, "flowCode": "M"}
    data = comtrade_get({**base, "cmdCode": ",".join(hs)})
    rows = data.get("data", [])
    truncated = len(rows) >= 490
    if truncated:  # preview API caps at 500 rows: query each HS code on its own
        rows = []
        for code in hs:
            d = comtrade_get({**base, "cmdCode": code})
            rows += d.get("data", [])
    totals = {}
    for r in _clean(rows):
        totals[r["partnerCode"]] = totals.get(r["partnerCode"], 0) + (r["primaryValue"] or 0)
    world = totals.pop(0, None)
    partners = sorted(({"code": c, "value": v} for c, v in totals.items() if v > 0), key=lambda x: -x["value"])
    out = {"year": year, "world": world, "partners": partners[:20], "n_partners": len(partners),
           "truncated_each_hs": truncated}
    cache_set(key, out)
    return out


def _news_google(country, terms, lang):
    topic = " OR ".join(terms)
    q = f'"{country}" ({topic}) (market OR industry OR imports OR regulation OR tariff OR demand OR exports) when:90d'
    hl = {"english": "en-US&gl=US&ceid=US:en", "korean": "ko&gl=KR&ceid=KR:ko"}.get(lang, "en-US&gl=US&ceid=US:en")
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=" + hl
    st, body = http_request(url, headers={"Accept": "application/rss+xml"})
    if st != 200:
        raise ApiError(f"Google News {st}")
    root = ET.fromstring(body)
    out = []
    for it in root.iter("item"):
        title = " ".join((it.findtext("title") or "").split())
        src = it.find("source")
        domain = src.text if src is not None and src.text else ""
        if domain and title.endswith(" - " + domain):
            title = title[: -len(domain) - 3]
        d = it.findtext("pubDate") or ""
        try:
            d = parsedate_to_datetime(d).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
        out.append({"title": title, "url": it.findtext("link"), "domain": domain, "date": d, "lang": lang})
    return out


def _news_gdelt(country, terms, lang):
    topic = " OR ".join(terms)
    q = f'"{country}" ({topic}) (market OR industry OR imports OR regulation OR tariff OR demand OR exports) sourcelang:{lang}'
    params = {"query": q, "mode": "artlist", "format": "json", "maxrecords": 50, "timespan": "3months", "sort": "datedesc"}
    url = GDELT + "?" + urllib.parse.urlencode(params)
    body = b""
    for attempt in range(3):
        TH_GDELT.wait()
        st, body = http_request(url, timeout=90)
        if st == 200 and body.lstrip().startswith(b"{"):
            break
        if st == 429 or body.startswith(b"Please limit"):
            time.sleep(6)
            continue
        raise ApiError(f"GDELT {st}")
    else:
        raise ApiError("GDELT is rate limiting requests.")
    out = []
    for a in json.loads(body).get("articles", []):
        d = a.get("seendate", "")
        try:
            d = datetime.strptime(d, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d")
        except ValueError:
            pass
        out.append({"title": " ".join((a.get("title") or "").split()), "url": a.get("url"),
                    "domain": a.get("domain"), "date": d, "lang": a.get("language")})
    return out


def news(country, terms, lang):
    key = "news:v2:" + json.dumps([country, terms, lang])
    cached = cache_get(key, 6 * 3600)
    if cached is not None:
        return cached
    errors, items, source = [], [], None
    for name, fn in (("Google News", _news_google), ("GDELT", _news_gdelt)):
        try:
            items = fn(country, terms, lang)
        except (ApiError, ET.ParseError, ValueError) as e:
            errors.append(f"{name}: {e}")
            continue
        if items:
            source = name
            break
    if not items:
        raise ApiError("No news found. " + " | ".join(errors))
    seen, out = set(), []
    for a in items:
        k = a["title"].lower()[:80]
        if not a["title"] or k in seen:
            continue
        seen.add(k)
        out.append(a)
        if len(out) >= 15:
            break
    result = {"source": source, "items": out}
    cache_set(key, result)
    return result


def ai_summary(payload):
    """Optional LLM summary grounded only in the numbers and headlines the page already shows."""
    lang = "Korean" if payload.get("lang") == "ko" else "English"
    prompt = (
        f"You are a market analyst helping a company decide on exporting. Write a concise summary in {lang} (6-9 sentences) "
        "using ONLY the FACTS below. Do not add numbers that are not in the facts. State the data year for every figure, "
        "mention that trade values are importer-reported UN Comtrade data in current USD, and say what is missing instead "
        "of guessing. Finish with 2-3 points to verify before entering this market.\n\nFACTS (JSON):\n"
        + json.dumps(payload.get("facts", {}), ensure_ascii=False)[:12000]
    )
    if os.environ.get("ANTHROPIC_API_KEY"):
        body = json.dumps({"model": os.environ.get("MI_MODEL", "claude-sonnet-5-5"), "max_tokens": 900,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        st, resp = http_request("https://api.anthropic.com/v1/messages", data=body, timeout=120, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if st != 200:
            raise ApiError(f"LLM {st}: {resp[:200].decode('utf8', 'replace')}")
        return "".join(b.get("text", "") for b in json.loads(resp).get("content", []))
    if os.environ.get("OPENAI_API_KEY"):
        body = json.dumps({"model": os.environ.get("MI_MODEL", "gpt-4o-mini"), "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        st, resp = http_request("https://api.openai.com/v1/chat/completions", data=body, timeout=120, headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "content-type": "application/json"})
        if st != 200:
            raise ApiError(f"LLM {st}: {resp[:200].decode('utf8', 'replace')}")
        return json.loads(resp)["choices"][0]["message"]["content"]
    raise ApiError("No LLM key configured")


def collect_market(iso3, industry_id):
    """Everything the RAG index needs for one market. Uses the same cached calls as the web UI."""
    m = meta()
    c = next(x for x in m["countries"] if x["iso3"] == iso3)
    ind = next(i for i in INDUSTRIES if i["id"] == industry_id)
    hs = [h["code"] for h in ind["hs"]]
    years, found, y = {}, 0, datetime.now().year - 1
    while found < 9 and y >= 2013:
        d = trade_year(c["m49"], hs, y)
        years[y] = d
        if d["rows"]:
            found += 1
        y -= 1
    latest = max((yy for yy, d in years.items() if d["rows"]), default=None)
    partners = trade_partners(c["m49"], hs, latest) if latest else None
    try:
        nws = news(c["name"], ind["news"], "english")
    except ApiError:
        nws = None  # headlines are optional
    return {"country": c, "industry": ind, "years": years, "partners": partners, "macro": macro(iso3), "news": nws, "names": m["names"]}


# ----------------------------------------------------------------------------
# http handler
# ----------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
HS_RE = re.compile(r"^\d{4}(\d{2})?$")


_hits = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketIntel/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_ask(self, payload):
        """Newline-delimited JSON, one event per pipeline step, so the page can show progress."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(ev):
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        try:
            for ev in rag.ask(payload.get("question", ""), meta(), collect_market, payload.get("lang", "ko")):
                emit(ev)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # report instead of dropping the stream
            try:
                emit({"error": "failed", "detail": f"{type(e).__name__}: {str(e)[:400]}"})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0])

    def _limited(self):
        """Public demo only: at most 120 data requests per minute per client."""
        if not PUBLIC:
            return False
        now, ip = time.time(), self._client_ip()
        q = [t for t in _hits.get(ip, []) if now - t < 60]
        q.append(now)
        _hits[ip] = q
        if len(_hits) > 5000:
            _hits.clear()
        return len(q) > 120

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/") and u.path != "/api/meta" and self._limited():
            return self._json({"error": "too many requests, try again in a minute"}, 429)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/meta":
                return self._json(meta())
            if u.path == "/api/rag/status":
                if PUBLIC:
                    return self._json({"available": False, "reason": "public", "detail": "disabled in the public demo"})
                return self._json(rag.status())
            if u.path == "/api/macro":
                iso3 = q.get("iso3", "").upper()
                if not re.fullmatch(r"[A-Z]{3}", iso3):
                    raise ApiError("bad iso3")
                return self._json(macro(iso3))
            if u.path in ("/api/trade", "/api/partners"):
                reporter = int(q["reporter"])
                year = int(q["year"])
                hs = [h for h in q.get("hs", "").split(",") if h]
                if not hs or len(hs) > 12 or not all(HS_RE.match(h) for h in hs):
                    raise ApiError("bad hs list")
                if PUBLIC and tuple(hs) not in {tuple(h["code"] for h in i["hs"]) for i in INDUSTRIES}:
                    raise ApiError("custom HS codes are disabled in the public demo")
                fn = trade_year if u.path == "/api/trade" else trade_partners
                return self._json(fn(reporter, hs, year))
            if u.path == "/api/news":
                terms = [t.strip() for t in q.get("terms", "").split("|") if t.strip()][:6]
                if not q.get("country") or not terms:
                    raise ApiError("country and terms required")
                return self._json(news(q["country"], terms, q.get("lang", "english")))
            if u.path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            return self._static(u.path)
        except (ApiError, KeyError, ValueError) as e:
            return self._json({"error": str(e)}, 502 if isinstance(e, ApiError) else 400)
        except Exception as e:  # keep the server alive
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if PUBLIC:
            return self._json({"error": "disabled in the public demo"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/ai":
                return self._json({"text": ai_summary(payload)})
            if self.path == "/api/rag/ask":
                return self._stream_ask(payload)
            return self._json({"error": "not found"}, 404)
        except ApiError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    port = int(os.environ.get("PORT", "8000"))
    if PUBLIC and not os.path.exists(DB_PATH) and os.path.exists(DEMO_CACHE):
        import shutil
        shutil.copyfile(DEMO_CACHE, DB_PATH)  # ship real data collected on the snapshot date
    srv = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}"
    print(f"Global Market Intelligence Navigator running at {url}  (Ctrl+C to stop)")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
