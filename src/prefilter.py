"""Regex prefilter: which tweets could carry a call on one of the three assets (EN/TR), so they go to the Jev gate.

Deliberately generous: recall matters more than precision here — a false positive costs a fraction of a cent at
the gate, a false negative is a call nobody ever sees. The gate and the classifier decide what is actually a call.

Two layers:
- asset vocabulary (_ASSET_PATTERNS): the tweet names BTC / gold / US equities, directly or by a close proxy
  (crypto, hard assets, stocks, equities, recession, VIX). assets_hint = the named assets.
- market-call vocabulary (_GENERIC): no asset named, but the tweet talks price direction (bull/bear, rally, bottom,
  dip, breakout, higher/lower highs, boğa, yükseliş, dip…). Measured on 35.5k regex-rejected tweets run through
  Jev: these carried a third of the gate passes the asset vocabulary missed. assets_hint stays '' and the
  gate/classifier consider all three assets (and must not guess one when the tweet leaves it unknowable).

Vocabulary was mined from gate passes among rejected tweets (scripts/prefilter_vocab.py); coverage is measured by
scripts/prefilter_stats.py against stored gate rows. English stems never take a bare \\w* (bull\\w* = bulletin,
gold\\w* = Goldman, dow\\w* = down); Turkish stems do, because suffixes are the point.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ASSETS = ("BTC", "GOLD", "SPX")

# Turkish letters that may appear inside a word; used for word boundaries instead of \b (which is ASCII-only)
_TR = "a-zA-Z0-9çğıöşüâîûÇĞİÖŞÜ"
_B0 = f"(?<![{_TR}])"   # left boundary (allows # $ @ prefixes)
_B1 = f"(?![{_TR}])"    # right boundary


def _alt(*terms: str) -> str:
    return "|".join(terms)


def _rx(*terms: str) -> re.Pattern:
    return re.compile(_B0 + "(" + _alt(*terms) + ")" + _B1, re.IGNORECASE)


_ASSET_PATTERNS = {
    "BTC": _rx(
        r"btc(usdt?|usd|eur)?", r"bitcoin\w*", r"bitcoi̇n\w*", r"xbt", r"sats", r"satoshi\w*",  # bare "sat" = TR "sell"
        r"₿\w*", r"hodl\w*", r"halving\w*", r"digital gold", r"orange coin", r"stacking sats",
        r"ibit", r"gbtc", r"fbtc", r"bitcoin etfs?", r"spot etfs?",
        r"crypto\w*", r"cryptocurrenc(y|ies)", r"alt ?coins?", r"alts", r"alt ?season", r"#?altseason",
        r"risk assets?",  # BTC as much as SPX
        r"비트코인", r"ビットコイン", r"比特币",  # KR / JP / CN spellings seen on the roster
        r"\$?\d{2,3} ?k",  # "$75k", "60K": BTC price levels (gold/SPX are never quoted in k)
        # TR: kripto / koin (crypto in general → BTC proxy), bitcoin'in etc. covered by \w*
        r"kripto\w*", r"koin\w*", r"coin\w*",
    ),
    "GOLD": _rx(
        r"golds?", r"goldbugs?", r"#gold\w*", r"xau\w*", r"gld", r"iau", r"gc1!?", r"gc=f", r"gc_f", r"comex",
        r"(metals|miners)( and (miners|metals))?", r"gdx",  # not goldman/golden
        r"precious metals?", r"bullion", r"yellow metal", r"hard assets?", r"hard money", r"sound money",
        r"commodit(y|ies)", r"debas(e|ed|es|ing|ement)", r"central banks? buying",
        # TR: altın (altının, altına, altında…), ons altın, gram altın, çeyrek, değerli/kıymetli metal
        r"alt[ıi]n\w*", r"ons ?alt[ıi]n\w*", r"gram ?alt[ıi]n\w*", r"ons", r"[çc]eyrek alt[ıi]n\w*",
        r"de[ğg]erli metal\w*", r"k[ıi]ymetli metal\w*", r"emtia\w*",
    ),
    "SPX": _rx(
        r"spx\w*", r"spy", r"s&p ?500", r"s&ps?", r"s&amp;p ?500", r"s&amp;ps?", r"sp500", r"sp ?500", r"es_f", r"es1!?",
        r"nasdaq\w*", r"nasdak\w*", r"ndx", r"qqq", r"nq_f", r"nq1!?", r"dow( ?jones)?", r"djia", r"russell\w*",
        r"iwm", r"voo", r"vti", r"smh", r"soxx?", r"vix", r"wall ?street",  # "dow", not down/downside
        r"us ?stocks?", r"us ?equit(y|ies)", r"us ?index(es)?", r"us500", r"nvidia", r"nvda", r"mag(nificent)? ?7",
        r"stocks?", r"stock ?markets?", r"equit(y|ies)", r"indices", r"index(es)?", r"large ?caps?", r"small ?caps?",
        r"mega ?caps?", r"big tech", r"tech stocks?", r"semis", r"semiconductors?", r"faang", r"ai bubble",
        r"dot ?com (bubble|crash)", r"recessions?", r"risk assets?", r"earnings season",
        # TR: hisse (stock), borsa (exchange), endeks (index) — with suffixes; ABD/Amerikan borsaları
        r"hisse\w*", r"borsa\w*", r"endeks\w*", r"abd (borsa|endeks|hisse)\w*", r"amerikan (borsa|endeks|hisse)\w*",
        r"tekno(loji)? ?hisse\w*", r"resesyon\w*",
    ),
}

# Market-call vocabulary with no asset named. English: exact forms only (no \w* — bull\w* = bulletin).
_GENERIC = _rx(
    r"bull(s|ish|ishness)?", r"bear(s|ish|ishness)?", r"bull ?run", r"bull ?market", r"bear ?market",
    r"rall(y|ies|ied|ying)", r"bottom(s|ed|ing)?", r"top(s|ped|ping)? (is|are) in", r"dips?", r"buy(ing)? the dip",
    r"corrections?", r"pullbacks?", r"breakouts?", r"breakdowns?", r"crash(es|ed|ing)?", r"capitulat(e|es|ed|ion)",
    r"parabolic", r"squeeze", r"longs", r"shorts", r"shorting", r"going (long|short)", r"moon(ing)?", r"pump(s|ed|ing)?",
    r"dump(s|ed|ing)?", r"all[- ]?time[- ]?highs?", r"aths?", r"new highs?", r"new lows?", r"(higher|lower) (highs|lows)",
    r"risk[- ]?(on|off)", r"the market", r"this market", r"markets? (will|is|are|to|should|could|heads?|headed)",
    r"higher (from here|this|by|into)", r"lower (from here|this|by|into)", r"up only", r"send it",
    r"(going|go|goes|headed|heading|move|moving|much|way|trade|trading|price) (higher|lower)",
    r"higher (imo|still|again|soon|next)", r"lower (imo|still|again|soon|next)", r"higher\W*$", r"lower\W*$",
    r"(sell|buy) everything", r"sell it all", r"only way is (up|down)", r"we go (up|down|higher|lower)", r"to the moon",
    r"(up|down) from here", r"then (up|down)", r"up and to the right", r"the (high|low|top|bottom) is in",
    r"(cycle|market|blow[- ]?off|local|macro) tops?", r"top of the cycle", r"(assets|everything) (go|going|goes) higher",
    r"back up the truck", r"(range|local) (highs|lows)", r"retest(s|ing|ed)?", r"retrace(s|ment)?", r"rip(s|ping)?",
    r"cup and handle", r"head and shoulders", r"double (top|bottom)", r"lower bound", r"upper bound",
    r"qe", r"qt", r"rate cuts?", r"cut rates", r"print(ing)? money", r"froth(y)?", r"bubbles?",
    r"consolidation", r"continuation", r"downside", r"upside", r"secular", r"cycle (top|low|bottom)",
    # TR: boğa (bull), ayı piyasası (bear market), yükseliş/düşüş, ralli, dip, destek/direnç, zirve, çöküş, vade
    r"bo[ğg]a\w*", r"ay[ıi] piyasas\w*", r"y[üu]kseli[şs]\w*", r"d[üu][şs][üu][şs]\w*", r"ralli\w*",
    r"dip(te|ten|e|i|ler\w*)?", r"destek\w*", r"diren[çc]\w*", r"zirve\w*", r"[çc][öo]k[üu][şs]\w*",
    r"(k[ıi]sa|orta|uzun) ?vade\w*", r"piyasa\w*", r"yukar[ıi]\w*", r"a[şs]a[ğg][ıi]\w*", r"bekliyorum",
    r"al[ıi]m f[ıi]rsat\w*", r"hedef\w*", r"balon\w*", r"geri ?[çc]ekilme\w*",
)

# Turkish "borsa"/"hisse" alone often means BIST; only count them as SPX if a US cue is present in the tweet.
_TR_LOCAL_ONLY = re.compile(_B0 + r"(hisse\w*|borsa\w*|endeks\w*|stocks?|index(es)?|indices|equit(y|ies))" + _B1,
                            re.IGNORECASE)
_US_CUE = re.compile(
    _B0 + r"(abd|amerika\w*|us|usa|wall ?street|s&p|s&amp;p|spx|spy|nasdaq\w*|dow( ?jones)?|nvidia|nvda|fed|fomc|"
    r"tesla|apple|microsoft|amazon|meta|google|alphabet|mag ?7|magnificent)" + _B1, re.IGNORECASE)
_BIST_CUE = re.compile(_B0 + r"(bist\w*|xu100|borsa istanbul|thy|aselsan|t[üu]pra[şs]|ko[çc]|garanti|akbank|ykb|"
                       r"ere[ğg]li|sasa|hekts|astor)" + _B1, re.IGNORECASE)
_TR_TEXT = re.compile(r"[ğışçöüİĞŞÇÖÜ]")

_URL = re.compile(r"https?://\S+")


def _clean(text: str) -> str:
    return _URL.sub("", html.unescape(text))


def detect_assets(text: str) -> list[str]:
    t = _clean(text)
    found = [a for a in ASSETS if _ASSET_PATTERNS[a].search(t)]
    if "SPX" in found:
        # if the only SPX evidence is generic market words (TR borsa/hisse, or "stocks" in a Turkish tweet),
        # require a US cue and no BIST cue
        specific = re.sub(_TR_LOCAL_ONLY, "", t)
        if not _ASSET_PATTERNS["SPX"].search(specific):
            tr_local = bool(_TR_TEXT.search(t)) or not re.search(_B0 + r"(stocks?|equit|index|indices)", t, re.I)
            if _BIST_CUE.search(t) or (tr_local and not _US_CUE.search(t)):
                found.remove("SPX")
    return found


def is_market_call_language(text: str) -> bool:
    """No asset named, but price-direction vocabulary present (second layer)."""
    return bool(_GENERIC.search(_clean(text)))


# --- third layer: vocabulary learned from Jev gate passes among regex-rejected tweets (src/prefilter_learn.py) ---
LEARNED_PATH = Path(__file__).resolve().parent.parent / "data" / "prefilter_learned.json"
_LEARNED: dict[str | None, re.Pattern | None] = {}


def load_learned() -> dict:
    if LEARNED_PATH.exists():
        return json.loads(LEARNED_PATH.read_text())
    return {"terms": [], "dropped": [], "history": []}


def save_learned(data: dict) -> None:
    LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def reload_learned() -> None:
    """(Re)compile the learned terms into one pattern per asset (None = no asset, generic market language)."""
    _LEARNED.clear()
    by: dict[str | None, list[str]] = {}
    for t in load_learned()["terms"]:
        by.setdefault(t.get("asset"), []).append(re.escape(t["term"]).replace(r"\ ", r"\s+"))
    for a, terms in by.items():
        _LEARNED[a] = _rx(*sorted(terms, key=len, reverse=True))


reload_learned()


def learned_assets(text: str) -> tuple[bool, list[str]]:
    t = _clean(text)
    hit = False
    assets = []
    for a, rx in _LEARNED.items():
        if rx is not None and rx.search(t):
            hit = True
            if a:
                assets.append(a)
    return hit, [a for a in ASSETS if a in assets]


def is_relevant(text: str, is_reply: bool = False) -> tuple[bool, list[str]]:
    """Return (relevant, assets). Relevant = names an asset, uses market-call language, or matches a learned term.
    With no asset named, assets is [] and downstream stages consider all three."""
    if is_reply:
        return False, []
    assets = detect_assets(text)
    hit, learned = learned_assets(text) if _LEARNED else (False, [])
    assets = [a for a in ASSETS if a in assets or a in learned]
    if assets:
        return True, assets
    return is_market_call_language(text) or hit, []
