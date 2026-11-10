"""Agent layer for Ask AI: planner, tools (regulatory, currency, internal notes), answer checks, offline sample mode.

The planner is rule based on purpose: it is free, fast, testable and shows the user what will be looked up.
Tools return numbered sources that the answer must cite; `verify` then checks the finished answer against them.
Offline mode (MI_LLM=offline) swaps the two paid parts, embeddings and the answer writer, for local ones so the whole
pipeline can be demonstrated and tested without an API key. Its answers are written from the same data, and are labelled
as sample answers in the interface.
"""
import json
import os
import re
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_PATH = os.environ.get("MI_NOTES_FILE", os.path.join(HERE, "data", "notes.json"))
REG_PATH = os.path.join(HERE, "regulatory.json")
FX_SNAPSHOT = os.path.join(HERE, "data", "fx.json")
TEAMS = ("sales", "cs", "lab", "other")
_lock = threading.Lock()


def offline():
    return os.environ.get("MI_LLM") == "offline"


# ----------------------------------------------------------------------------
# offline embeddings (hashing bag of words), only used when MI_LLM=offline
# ----------------------------------------------------------------------------
def hash_embeddings():
    import hashlib
    import math
    from langchain_core.embeddings import Embeddings

    class HashEmbeddings(Embeddings):
        DIM = 512

        def _vec(self, text):
            v = [0.0] * self.DIM
            low = text.lower()
            toks = re.findall(r"[a-z0-9]+", low) + [low[i:i + 2] for i in range(len(low) - 1) if "가" <= low[i] <= "힣"]
            for t in toks:
                h = int(hashlib.md5(t.encode()).hexdigest(), 16)
                v[h % self.DIM] += 1.0 if (h >> 100) & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / n for x in v]

        def embed_documents(self, texts):
            return [self._vec(t) for t in texts]

        def embed_query(self, text):
            return self._vec(text)

    return HashEmbeddings()


# ----------------------------------------------------------------------------
# internal notes: what sales, CS and the lab know that no public statistic has
# ----------------------------------------------------------------------------
def _read_notes():
    try:
        with open(NOTES_PATH, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def list_notes(market=None):
    notes = _read_notes()
    return [n for n in notes if market is None or n["market"] == market]


def add_note(market, team, text, author=""):
    text = (text or "").strip()
    if not re.fullmatch(r"[A-Z]{3}/[a-z0-9_-]+", market or ""):
        raise ValueError("market must look like BRA/dental")
    if team not in TEAMS:
        raise ValueError("team must be one of " + ", ".join(TEAMS))
    if not 5 <= len(text) <= 600:
        raise ValueError("note must be 5 to 600 characters")
    note = {"id": uuid.uuid4().hex[:8], "market": market, "team": team, "text": text,
            "author": (author or "")[:40], "ts": int(time.time())}
    with _lock:
        notes = _read_notes()
        notes.append(note)
        os.makedirs(os.path.dirname(NOTES_PATH), exist_ok=True)
        with open(NOTES_PATH, "w", encoding="utf8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=1)
    return note


def delete_note(note_id):
    with _lock:
        notes = _read_notes()
        keep = [n for n in notes if n["id"] != note_id]
        if len(keep) == len(notes):
            return False
        with open(NOTES_PATH, "w", encoding="utf8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=1)
    return True


# ----------------------------------------------------------------------------
# regulatory + currency tools
# ----------------------------------------------------------------------------
def regulatory(iso3):
    try:
        with open(REG_PATH, encoding="utf8") as f:
            return json.load(f).get(iso3)
    except (OSError, ValueError):
        return None


_fx_cache = {}


def fx_rate(currency, _fetch=None):
    """Units of `currency` per 1 USD: (rate, date, source) or None. Live ECB reference rate, snapshot file as fallback."""
    if currency == "USD":
        return 1.0, time.strftime("%Y-%m-%d"), "USD"
    hit = _fx_cache.get(currency)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    res = None
    try:
        url = f"https://api.frankfurter.dev/v1/latest?base=USD&symbols={currency}"
        if _fetch:
            d = _fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=5) as r:
                d = json.loads(r.read())
        if d.get("rates", {}).get(currency):
            res = (float(d["rates"][currency]), d["date"], "ECB reference rate")
    except Exception:
        res = None
    if res is None:
        try:
            with open(FX_SNAPSHOT, encoding="utf8") as f:
                snap = json.load(f)
            if snap.get("rates", {}).get(currency):
                res = (float(snap["rates"][currency]), snap["date"], "saved rate")
        except (OSError, ValueError):
            res = None
    if res:
        _fx_cache[currency] = (time.time(), res)
    return res


# ----------------------------------------------------------------------------
# planner
# ----------------------------------------------------------------------------
CERT_RE = re.compile(r"certif|regist|approv|licen|regulat|fda|\bce\b|mdr|anvisa|nmpa|pmda|cofepris|mfds|인증|허가|등록|규제|승인", re.I)
FX_RE = re.compile(r"currenc|exchange rate|\bfx\b|local price|기타|환율|화폐|통화|현지 ?가격|원화", re.I)
ENTRY_RE = re.compile(r"enter|entry|expand|launch|should|진출|진입|확장", re.I)


def make_plan(question, countries, industry):
    """Ordered list of steps: [{id, tools:[...]}]. ids are translated by the interface."""
    steps = [{"id": "facts", "tools": ["trade", "partners", "macro", "news"]}]
    if len(countries) > 1:
        steps.append({"id": "compare", "tools": ["compare"]})
    if CERT_RE.search(question) or ENTRY_RE.search(question):
        steps.append({"id": "regulatory", "tools": ["regulatory"]})
    if FX_RE.search(question):
        steps.append({"id": "fx", "tools": ["fx"]})
    steps.append({"id": "internal", "tools": ["internal_notes"]})
    return steps


def run_tools(plan, markets, total_by_market, lang="en", _fetch=None):
    """markets: [(key, country dict)]. Returns (calls, sources) where sources are dicts without ids."""
    ids = {t for st in plan for t in st["tools"]}
    calls, sources = [], []
    for key, c in markets:
        iso = c["iso3"]
        if "regulatory" in ids:
            r = regulatory(iso)
            calls.append({"tool": "regulatory", "market": key, "found": bool(r)})
            if r:
                body = r.get("text_ko") if lang == "ko" and r.get("text_ko") else r["text"]
                tail = " 출발점일 뿐이니 현지에서 확인하세요." if lang == "ko" and r.get("text_ko") else " Starting point only; confirm locally."
                head = "규제" if lang == "ko" and r.get("text_ko") else "Regulation"
                sources.append({"kind": "regulatory", "market": key, "source": r["authority"], "url": None,
                                "text": f"{head}, {c['name']}: {r['authority']}. {body}{tail}"})
        if "fx" in ids:
            r = regulatory(iso)
            cur = r["currency"] if r else None
            fx = fx_rate(cur, _fetch) if cur else None
            calls.append({"tool": "fx", "market": key, "found": bool(fx), "currency": cur})
            if fx:
                rate, date, src = fx
                txt = f"Currency, {c['name']}: {cur}. 1 USD = {rate:,.4g} {cur} ({src}, {date})."
                tot = total_by_market.get(key)
                if tot and cur != "USD":
                    txt += f" The latest import total of {tot['usd']} is about {tot['usd_raw'] * rate:,.3g} {cur}."
                sources.append({"kind": "fx", "market": key, "source": src, "url": None, "text": txt})
        if "internal_notes" in ids:
            ns = list_notes(key)[-5:]
            calls.append({"tool": "internal_notes", "market": key, "found": bool(ns), "n": len(ns)})
            for n in ns:
                sources.append({"kind": "internal", "market": key, "source": n["team"], "url": None,
                                "text": f"Internal note ({n['team']}), {c['name']}: {n['text']}"})
    return calls, sources


# ----------------------------------------------------------------------------
# answer checks
# ----------------------------------------------------------------------------
NUM_RE = re.compile(r"\$?\d[\d,]*\.?\d*\s?(?:%|[KMBT]\b)?")


def _norm(s):
    return re.sub(r"[\s$,+\-−]", "", s)


def verify(text, sources):
    """Check every [S#] exists, every figure is in the sources, and every sentence with a figure is cited."""
    ids = {s["id"] for s in sources}
    cited = set(re.findall(r"\[(S\d+)\]", text))
    bad = sorted(c for c in cited if c not in ids)
    context = _norm(" ".join(s["text"] for s in sources))
    body = re.sub(r"\[S\d+\]", " ", text)
    ungrounded, seen = [], set()
    for m in NUM_RE.finditer(body):
        raw = m.group(0).strip()
        n = _norm(raw).rstrip(".")
        if not n or n in seen:
            continue
        seen.add(n)
        if re.fullmatch(r"\d", n):  # list numbers and "1 USD"
            continue
        if n not in context:
            ungrounded.append(raw)
    uncited = 0
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        if re.search(r"\d{2,}|\d%", re.sub(r"\[S\d+\]", "", sent)) and not re.search(r"\[S\d+\]", sent):
            uncited += 1
    return {"cites": len(cited), "bad_cites": bad, "ungrounded": ungrounded[:8], "uncited": uncited,
            "ok": not bad and not ungrounded and uncited == 0}


# ----------------------------------------------------------------------------
# offline answer writer: same data, fixed wording, ko and en
# ----------------------------------------------------------------------------
L = {
    "en": {
        "ans": "{c} {i}: import demand was {tot} in {y}, {yoy} on the prior year and {cagr} a year on average over {n} years {r1}.",
        "kor": "Korea supplied {k}, {sh} of the total {r}.",
        "sup": "Largest suppliers: {top} {r}.",
        "reg": "Regulation: {t} {r}",
        "fx": "Currency: {t} {r}",
        "int": "Internal note from {team}: {t} {r}",
        "news": "Recent headline: {t} {r}",
        "cmp": "Korea's share of imports compared: {items}.",
        "h_att": "Market attractiveness", "h_gap": "Risks and gaps in the data", "h_ver": "What to verify next",
        "gap": "Import value shows import demand, not domestic market size, and the latest statistics lag by one to two years.",
        "ver": "Confirm the registration route with a local distributor, and check the internal notes against current quotes.",
        "ver_noint": "Confirm the registration route with a local distributor. No internal notes are saved for this market yet, so add what sales or CS know.",
        "up": "up", "down": "down",
        "yoy_fmt": "{d}",
    },
    "ko": {
        "ans": "{c} {i}: {y}년 수입 수요는 {tot}로 전년 대비 {yoy}, 최근 {n}년 연평균 {cagr}했습니다 {r1}.",
        "kor": "한국산은 {k}로 전체의 {sh}입니다 {r}.",
        "sup": "주요 공급국: {top} {r}.",
        "reg": "규제: {t} {r}",
        "fx": "통화: {t} {r}",
        "int": "내부 메모({team}): {t} {r}",
        "news": "최근 기사: {t} {r}",
        "cmp": "한국산 수입 비중 비교: {items}.",
        "h_att": "시장 매력도", "h_gap": "데이터의 한계", "h_ver": "다음에 확인할 것",
        "gap": "수입액은 수입 수요를 보여 줄 뿐 내수 시장 규모가 아니며, 최신 통계는 1~2년 늦습니다.",
        "ver": "현지 유통사와 등록 경로를 확인하고, 내부 메모는 최신 견적과 대조하세요.",
        "ver_noint": "현지 유통사와 등록 경로를 확인하세요. 이 시장에 저장된 내부 메모가 아직 없으니 영업·CS가 아는 내용을 추가하세요.",
        "up": "증가", "down": "감소",
        "yoy_fmt": "{d}",
    },
}


def offline_answer(markets, datas, sources, lang, rag):
    """Sample answer written from the collected data. `rag` supplies derive/metrics/usd/pct so the figures match the index."""
    T = L["ko" if lang == "ko" else "en"]

    def sid(key, kind):
        return next((s["id"] for s in sources if s["market"] == key and s["kind"] == kind), None)

    def ref(i):
        return f"[{i}]" if i else ""

    out = []
    sections = {"att": [], "gap": [T["gap"]], "ver": []}
    any_notes = False
    for key, c in markets:
        data = datas[key]
        m = rag.metrics(rag.derive(data["years"]))
        if not m:
            continue
        ind = data["industry"]["name"]["en"]
        s_tr, s_kr, s_pt = sid(key, "trade_summary"), sid(key, "korea"), sid(key, "partners")

        def signed(x):
            if x is None:
                return "n/a"
            return (rag.pct(abs(x)) + " " + T["up"] if lang == "ko" else T["up"] + " " + rag.pct(abs(x))) if x >= 0 else (rag.pct(abs(x)) + " " + T["down"] if lang == "ko" else T["down"] + " " + rag.pct(abs(x)))
        line = T["ans"].format(c=c["name"], i=ind, tot=rag.usd(m["last"]["total"]), y=m["last"]["year"], yoy=signed(m["yoy"]),
                               cagr=signed(m["cagr"]), n=m["span"], r1=ref(s_tr))
        sections["att"].append(line)
        sections["att"].append(T["kor"].format(k=rag.usd(m["last"]["kor"]), sh=rag.pct(m["kor_share"], d=2), r=ref(s_kr)))
        p = data.get("partners")
        if p and p.get("partners"):
            names, total = data.get("names", {}), p.get("world") or sum(x["value"] for x in p["partners"])
            top = "; ".join(f"{names.get(str(x['code']), x['code'])} {rag.usd(x['value'])} ({rag.pct(x['value'] / total)})" for x in p["partners"][:3])
            sections["att"].append(T["sup"].format(top=top, r=ref(s_pt)))
        for s in sources:
            if s["market"] != key:
                continue
            if s["kind"] == "regulatory":
                sections["ver"].append(T["reg"].format(t=s["text"].split(". ", 1)[1].replace(" Starting point only; confirm locally.", "").replace(" 출발점일 뿐이니 현지에서 확인하세요.", ""), r=ref(s["id"])))
            elif s["kind"] == "fx":
                sections["ver"].append(T["fx"].format(t=s["text"].split(". ", 1)[1], r=ref(s["id"])))
            elif s["kind"] == "internal":
                any_notes = True
                sections["ver"].append(T["int"].format(team=s["source"], t=s["text"].split(": ", 1)[1], r=ref(s["id"])))
    if len(markets) > 1:
        shares = []
        for key, c in markets:
            m = rag.metrics(rag.derive(datas[key]["years"]))
            if m:
                shares.append((c["name"], rag.pct(m["kor_share"], d=2), sid(key, "korea")))
        if len(shares) > 1:
            sections["att"].append(T["cmp"].format(items="; ".join(f"{n} {sh} {ref(r)}" for n, sh, r in shares)))
    news = next((s for s in sources if s["kind"] == "news"), None)
    if news:
        sections["att"].append(T["news"].format(t=news["text"].split("): ", 1)[1] if "): " in news["text"] else news["text"], r=ref(news["id"])))
    sections["ver"].append(T["ver"] if any_notes else T["ver_noint"])
    out.append(sections["att"][0])
    for h, k in (("h_att", "att"), ("h_gap", "gap"), ("h_ver", "ver")):
        body = sections[k][1:] if k == "att" else sections[k]
        if body:
            out.append(T[h] + "\n" + "\n".join(body))
    return "\n\n".join(out)
