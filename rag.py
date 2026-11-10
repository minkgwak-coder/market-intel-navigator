"""Phase 3: RAG over the market statistics and news this app already collects.

Pipeline (each step is reported to the UI as it happens):
  1. question input     parse country / industry from the question
  2. vector search      OpenAI embeddings + FAISS (LangChain), filtered to the markets in the question
  3. relevant data      retrieved passages + the core statistics of each market, numbered [S1], [S2], ...
  4. GPT summary        ChatOpenAI answers only from that context and cites [S#]

Needs:  pip install -r requirements.txt   and   OPENAI_API_KEY in the environment.
"""
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.environ.get("MI_FAISS_DIR", os.path.join(HERE, "data", "faiss"))
EMBED_MODEL = os.environ.get("MI_EMBED_MODEL", "text-embedding-3-small")
CHAT_MODEL = os.environ.get("MI_CHAT_MODEL", "gpt-4o-mini")
TOP_K = int(os.environ.get("MI_TOP_K", "8"))
STALE_AFTER = 7 * 86400

_lock = threading.Lock()


class RagError(Exception):
    pass


# ----------------------------------------------------------------------------
# environment
# ----------------------------------------------------------------------------
def status():
    try:
        import faiss  # noqa: F401
        import langchain_community.vectorstores  # noqa: F401
        import langchain_openai  # noqa: F401
    except Exception as e:  # missing packages
        return {"available": False, "reason": "packages",
                "detail": f"{type(e).__name__}: {e}"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"available": False, "reason": "key", "detail": "OPENAI_API_KEY is not set"}
    return {"available": True, "embed_model": EMBED_MODEL, "chat_model": CHAT_MODEL, "markets": _markets()}


def get_embeddings():
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(model=EMBED_MODEL)


def get_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=CHAT_MODEL, temperature=0)


def _markets():
    try:
        with open(os.path.join(INDEX_DIR, "markets.json"), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_markets(m):
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(os.path.join(INDEX_DIR, "markets.json"), "w", encoding="utf8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------------------
# formatting + series math (mirrors the web UI so the numbers agree)
# ----------------------------------------------------------------------------
def usd(n):
    if n is None:
        return "n/a"
    a = abs(n)
    if a >= 1e12:
        return f"${n / 1e12:.2f}T"
    if a >= 1e9:
        return f"${n / 1e9:.2f}B"
    if a >= 1e6:
        return f"${n / 1e6:.1f}M"
    if a >= 1e3:
        return f"${n / 1e3:.0f}K"
    return f"${n:.0f}"


def pct(x, signed=False, d=1):
    if x is None:
        return "n/a"
    return f"{x * 100:+.{d}f}%" if signed else f"{x * 100:.{d}f}%"


def derive(years):
    out = []
    for y in sorted(years):
        rows = years[y].get("rows", [])
        if not rows:
            continue
        by = {r["hs"]: r["world"] for r in rows if r.get("world") is not None}
        byk = {r["hs"]: r["kor"] for r in rows if r.get("kor") is not None}
        out.append({"year": y, "total": sum(by.values()), "kor": sum(byk.values()), "by": by, "byK": byk})
    return out


def metrics(series):
    if not series:
        return None
    last = series[-1]
    prev = next((s for s in series if s["year"] == last["year"] - 1), None)
    yoy = last["total"] / prev["total"] - 1 if prev and prev["total"] > 0 else None
    base_year = max(series[0]["year"], last["year"] - 5)
    base = next((s for s in series if s["year"] == base_year), None)
    span = last["year"] - base["year"] if base else 0
    cagr = (last["total"] / base["total"]) ** (1 / span) - 1 if base and span > 0 and base["total"] > 0 and last["total"] > 0 else None
    kor_share = last["kor"] / last["total"] if last["total"] > 0 else None
    return {"last": last, "prev": prev, "base": base, "yoy": yoy, "cagr": cagr, "span": span, "kor_share": kor_share}


# ----------------------------------------------------------------------------
# documents: one fact per document so retrieval can pick the right ones
# ----------------------------------------------------------------------------
def build_docs(data):
    """data: {country:{iso3,name}, industry:{id,name,hs:[{code,en}]}, years, partners, macro, news, names}"""
    from langchain_core.documents import Document
    c, ind = data["country"], data["industry"]
    mk = f"{c['iso3']}/{ind['id']}"
    cname, iname = c["name"], ind["name"]["en"]
    hs_list = ", ".join(h["code"] for h in ind["hs"])
    series = derive(data["years"])
    m = metrics(series)
    docs = []

    def add(kind, key, text, **meta):
        docs.append(Document(page_content=text, id=f"{mk}|{kind}|{key}",
                             metadata={"market": mk, "iso3": c["iso3"], "industry": ind["id"], "kind": kind, "country": cname, **meta}))

    if m:
        yearly = "; ".join(f"{s['year']} {usd(s['total'])}" for s in series)
        add("trade_summary", "all",
            f"{cname}: imports of {iname} products (HS {hs_list}) by year, UN Comtrade, importer-reported, current USD: {yearly}. "
            f"Latest year {m['last']['year']}: {usd(m['last']['total'])}, "
            f"{pct(m['yoy'], True)} versus {m['last']['year'] - 1}, {m['span']}-year average annual growth (CAGR) {pct(m['cagr'], True)}.",
            source="UN Comtrade", year=m["last"]["year"])
        kor = "; ".join(f"{s['year']} {usd(s['kor'])} ({pct(s['kor'] / s['total']) if s['total'] else 'n/a'})" for s in series)
        add("korea", "all",
            f"{cname}: imports of {iname} products from South Korea by year (share of total imports): {kor}. "
            f"In {m['last']['year']} Korea supplied {usd(m['last']['kor'])}, {pct(m['kor_share'], d=2)} of total.",
            source="UN Comtrade", year=m["last"]["year"])
        last, prev, base = m["last"], m["prev"], m["base"]
        for h in ind["hs"]:
            v = last["by"].get(h["code"])
            if v is None:
                continue
            pv = prev["by"].get(h["code"]) if prev else None
            bv = base["by"].get(h["code"]) if base else None
            yoy = v / pv - 1 if pv else None
            cg = (v / bv) ** (1 / m["span"]) - 1 if bv and m["span"] > 0 and v > 0 else None
            k = last["byK"].get(h["code"])
            add("trade_product", h["code"],
                f"{cname}: imports of HS {h['code']} ({h['en']}) in {last['year']}: {usd(v)}, {pct(v / last['total'])} of the {iname} group, "
                f"{pct(yoy, True)} versus the prior year, {m['span']}-year CAGR {pct(cg, True)}. From Korea: {usd(k) if k is not None else 'none reported'}.",
                source="UN Comtrade", year=last["year"], hs=h["code"])
    p = data.get("partners")
    if p and p.get("partners"):
        names = data.get("names", {})
        total = p.get("world") or sum(x["value"] for x in p["partners"])
        lst = "; ".join(f"{names.get(str(x['code']), x['code'])} {usd(x['value'])} ({pct(x['value'] / total)})" for x in p["partners"][:10])
        kor_rank = next((i + 1 for i, x in enumerate(p["partners"]) if x["code"] == 410), None)
        add("partners", str(p["year"]),
            f"{cname}: top supplier countries for {iname} imports in {p['year']} (of {p.get('n_partners', len(p['partners']))} suppliers): {lst}. "
            + (f"South Korea ranks #{kor_rank} among the top suppliers." if kor_rank else "South Korea is not among the top 20 suppliers."),
            source="UN Comtrade", year=p["year"])
    note = (ind.get("note") or {}).get("en")
    if note:
        add("note", "all", f"Data note for {iname} in {cname}: {note}", source="data note")
    mac = data.get("macro") or {}
    labels = {"gdp": ("GDP", usd), "gdp_pc": ("GDP per capita", lambda v: f"${v:,.0f}"), "gdp_growth": ("GDP growth", lambda v: f"{v:.1f}%"),
              "pop": ("population", lambda v: f"{v / 1e6:.1f}M"), "pop65": ("population aged 65+", lambda v: f"{v:.1f}% of total"),
              "health_gdp": ("health expenditure", lambda v: f"{v:.1f}% of GDP"), "health_pc": ("health expenditure per capita", lambda v: f"${v:,.0f}")}
    parts = []
    for k, (label, f) in labels.items():
        s = (mac.get(k) or {}).get("series") or []
        if s:
            parts.append(f"{label} {f(s[-1][1])} ({s[-1][0]})")
    if parts:
        add("macro", "all", f"{cname}: macroeconomic and health indicators, World Bank: " + "; ".join(parts) + ".", source="World Bank")
    for i, a in enumerate((data.get("news") or {}).get("items", [])):
        add("news", str(i), f"News ({a.get('date')}, {a.get('domain')}): {a['title']}", source=a.get("domain") or "news",
            url=a.get("url"), date=a.get("date"))
    return docs


# ----------------------------------------------------------------------------
# index (FAISS via LangChain)
# ----------------------------------------------------------------------------
def _load(emb):
    from langchain_community.vectorstores import FAISS
    if os.path.exists(os.path.join(INDEX_DIR, "index.faiss")):
        return FAISS.load_local(INDEX_DIR, emb, allow_dangerous_deserialization=True)  # our own file
    return None


def ingest(data, emb):
    """Embed this market's documents and (re)place them in the FAISS index. Returns the document count."""
    from langchain_community.vectorstores import FAISS
    docs = build_docs(data)
    if not docs:
        raise RagError("no data to index for this market")
    mk = docs[0].metadata["market"]
    ids = [d.id for d in docs]
    with _lock:
        meta = _markets()
        if meta.get("_embed_model") not in (None, EMBED_MODEL):  # embeddings changed: start over
            for f in ("index.faiss", "index.pkl"):
                try:
                    os.remove(os.path.join(INDEX_DIR, f))
                except OSError:
                    pass
            meta = {}
        vs = _load(emb)
        if vs is None:
            vs = FAISS.from_documents(docs, emb, ids=ids)
        else:
            old = [i for i in vs.index_to_docstore_id.values() if i.startswith(mk + "|")]
            if old:
                vs.delete(old)
            vs.add_documents(docs, ids=ids)
        os.makedirs(INDEX_DIR, exist_ok=True)
        vs.save_local(INDEX_DIR)
        meta["_embed_model"] = EMBED_MODEL
        meta[mk] = {"ts": time.time(), "docs": len(docs)}
        _save_markets(meta)
    return len(docs)


def is_fresh(mk):
    m = _markets()
    return m.get("_embed_model") == EMBED_MODEL and mk in m and time.time() - m[mk]["ts"] < STALE_AFTER


# ----------------------------------------------------------------------------
# question parsing
# ----------------------------------------------------------------------------
def _has(low, alias):
    if re.fullmatch(r"[\x00-\x7f]+", alias):
        return re.search(r"(^|[^a-z0-9])" + re.escape(alias) + r"($|[^a-z0-9])", low) is not None
    return alias in low


def parse_question(question, meta):
    low = " " + question.lower() + " "
    found = {}  # iso3 -> (matched alias, country)
    for c in meta["countries"]:
        hits = [a for a in c["aliases"] if len(a) >= 2 and _has(low, a)]
        if hits:
            found[c["iso3"]] = (max(hits, key=len), c)
    # a match inside a longer match belongs to the longer one (Korean "인도" inside "인도네시아")
    for iso, (alias, _) in list(found.items()):
        if any(alias != o and alias in o for o, _c in found.values()):
            del found[iso]
    # "Should Korean companies enter Brazil": Korea is the exporter, not the market, when another country is named
    if len(found) > 1:
        found.pop("KOR", None)
    countries = [c for _a, c in sorted(found.values(), key=lambda x: -len(x[0]))][:3]
    ind, ib = None, 0
    for i in meta["industries"]:
        for k in i["kw"]:
            k = k.lower()
            if len(k) > ib and _has(low, k):
                ind, ib = i, len(k)
    return countries, ind


# ----------------------------------------------------------------------------
# the four steps
# ----------------------------------------------------------------------------
SYSTEM = (
    "You are a market analyst helping an exporter decide whether and how to enter a foreign market. "
    "Answer ONLY from the numbered CONTEXT. Cite every factual sentence with its source number like [S2]. "
    "Never use a number that is not in the context. Trade values are importer-reported UN Comtrade data in current USD; "
    "import value shows import demand, not domestic market size, so say so when you discuss market size. "
    "If the context cannot answer part of the question, say what is missing instead of guessing. "
    "Format: a one-sentence answer first, then short sections: Market attractiveness, Growth, Suppliers and Korea's position, "
    "Risks and gaps in the data, What to verify next. Keep it under 250 words. Reply in the language of the question."
)


def ask(question, meta, collect, lang="ko"):
    """Generator of progress events. `collect(iso3, industry_id)` returns the market data dict (cached by the server)."""
    st = status()
    if not st["available"]:
        yield {"error": "rag_unavailable", "detail": st}
        return
    question = (question or "").strip()
    if len(question) < 3:
        yield {"error": "empty_question"}
        return
    t0 = time.time()

    # 1. question input
    countries, ind = parse_question(question, meta)
    if not countries or not ind:
        yield {"error": "no_market", "countries": [c["iso3"] for c in countries], "industry": ind["id"] if ind else None}
        return
    keys = [f"{c['iso3']}/{ind['id']}" for c in countries]
    yield {"step": "question", "markets": [{"key": k, "country": c["name"], "industry": ind["name"]["en"]} for k, c in zip(keys, countries)]}

    # index the markets (loads statistics + news the first time, then embeds them)
    emb = get_embeddings()
    for k, c in zip(keys, countries):
        if is_fresh(k):
            yield {"step": "index", "market": k, "status": "cached"}
            continue
        yield {"step": "index", "market": k, "status": "loading"}
        data = collect(c["iso3"], ind["id"])
        n = ingest(data, emb)
        yield {"step": "index", "market": k, "status": "done", "docs": n}

    # 2. vector search
    vs = _load(emb)
    if vs is None:
        raise RagError("index is empty")
    wanted = set(keys)
    hits = vs.similarity_search_with_score(question, k=TOP_K, fetch_k=max(vs.index.ntotal, TOP_K),
                                           filter=lambda md: md.get("market") in wanted)
    retrieved = [{"doc": d, "score": float(s)} for d, s in hits]
    yield {"step": "search", "k": TOP_K, "found": len(retrieved), "seconds": round(time.time() - t0, 1)}

    # 3. relevant data: retrieved passages plus each market's core statistics, numbered for citation
    chosen, seen = [], set()
    for r in retrieved:
        if r["doc"].id not in seen:
            seen.add(r["doc"].id)
            chosen.append({"doc": r["doc"], "score": r["score"], "pinned": False})
    for k in keys:
        for kind in ("trade_summary", "partners", "macro"):
            for did in list(vs.index_to_docstore_id.values()):
                if did.startswith(f"{k}|{kind}|") and did not in seen:
                    seen.add(did)
                    chosen.append({"doc": vs.docstore.search(did), "score": None, "pinned": True})
    sources = []
    for i, ch in enumerate(chosen, 1):
        d = ch["doc"]
        sources.append({"id": f"S{i}", "kind": d.metadata.get("kind"), "market": d.metadata.get("market"), "text": d.page_content,
                        "url": d.metadata.get("url"), "source": d.metadata.get("source"),
                        "relevance": None if ch["score"] is None else round(max(0.0, 1 - ch["score"] / 2), 3), "pinned": ch["pinned"]})
    yield {"step": "extract", "sources": sources}

    # 4. GPT summary
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    prompt = ChatPromptTemplate.from_messages([("system", SYSTEM),
                                               ("human", "CONTEXT:\n{context}\n\nQUESTION: {question}")])
    context = "\n".join(f"[{s['id']}] {s['text']}" for s in sources)
    chain = prompt | get_llm() | StrOutputParser()
    text = chain.invoke({"context": context, "question": question})
    yield {"step": "answer", "text": text, "model": CHAT_MODEL, "seconds": round(time.time() - t0, 1)}
