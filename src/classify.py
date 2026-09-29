"""Tweet → explicit directional calls.

Two modes:
  * API: `classify_pending(conn)` — with the Jev gate (default) every pending tweet is first scored by the TypeSafe
    decision model (src/gate.py); only passing tweets reach the text model, which produces quote / price_target.
    Labels stay under the text model's tag (FINCLATOR_GATE_TAG=1 → a separate "<model>+jev" dimension;
    FINCLATOR_GATE=0 → no gate).
  * Interactive: `export_pending()` writes a JSONL for an assistant session to label;
    `import_labels(path)` loads the result. Same schema either way.

Horizons follow the spec: SHORT 0-3 months, MEDIUM 3-12 months, LONG 1-5 years.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import models
from .db import connect, log

ROOT = Path(__file__).resolve().parent.parent
MODEL = models.classifier_model()      # storage tag for new labels (text model, "+jev" when the gate is on)
TEXT_MODEL = models.text_model()       # what the inference request names (Ollama tag / Anthropic model)
GATE_ON = models.GATE_ON
GATE_REUSE = os.environ.get("FINCLATOR_GATE_REUSE", "1") == "1"  # hybrid: reuse the text model's existing label
DIRECTIONS = ("BUY", "SELL", "NEUTRAL")
HORIZONS = ("SHORT", "MEDIUM", "LONG")  # same three keys as evaluate.MATURITY_DAYS

SYSTEM = """You extract explicit, falsifiable market calls from finance-influencer tweets.

Assets: BTC (Bitcoin), GOLD (gold, altın, XAU), SPX (S&P 500 / US equities / Nasdaq as proxy).
Tweets may be Turkish or English.

A CALL is the AUTHOR'S OWN forward-looking claim that the price of an asset will go up (BUY), down (SELL), or
that the author is explicitly neutral / two-sided / waiting (NEUTRAL). Be strict: when in doubt, is_call=false.

NOT a call (is_call=false):
- Reporting a move that already happened or is happening now: "hit a new high", "rally this morning",
  "yeni zirve yaptı", "Altın'ı coşturdu", "tarihi rekor", "%2 negatif seyrediyor", "haftayı yükselişle kapatıyor".
- A question, poll, podcast/video segment title, or headline with no answer: "Is $100k the new base?",
  "Will the drawdown get worse?", "Does bitcoin close 2025 above $100k?".
- Quoting or forwarding someone else's view (an analyst, a politician, a firm, "X says…") without the author
  adopting it. Only the author's own stance counts.
- Sentiment/positioning/indicator observations (fear & greed, ETF flows, volume, open interest, gold/silver
  ratio) with no explicit price prediction attached.
- Macro/news commentary, explanations of why a move happened, tautologies ("going down because more sellers"),
  and commentary about OTHER people's forecasts or mood ("çöküş geliyor timi", "the bears are loud") — that is
  a stance on the crowd, not on the price.
- Sarcasm or irony you cannot resolve.
- A bare caption for an image/chart with no words of opinion: "Bitcoin https://…", "Current Bitcoin Chart",
  "We are here (orange circle), we will be there (green circle)" — the claim lives in an image you cannot see.
- A stance on a different asset (an altcoin like $THETA, a miner, MSTR, a stock) that merely carries a #BTC/
  #gold/#nasdaq hashtag or a mention of the tracked asset — only label the tracked asset if the author
  takes a stance on IT.
- Pure chart-structure narration with only a conditional outcome: "if it closes above the diagonal it may
  continue higher", "if this retest succeeds it would form a higher low" — no base case stated → not a call.
  A stated base case ("bull market peak in Sept-Oct 2025", "getting closer and closer to $93000",
  "could trend-continue from here as long as $73k holds") IS a call.

Sales pitches still count when the stance is explicit: "switch from fool's gold into the real thing" →
BTC SELL + GOLD BUY (LONG). "Bullish for gold", "we continue to hold our $SPY short", "still in the
awareness phase of the bull cycle" are calls even inside news recaps.

IS a call even when idiomatic — read the author's stance, not the vocabulary:
- Bubble talk is a SELL call: "balon", "bubble", "aşırı değerli", "pornografik derecede pahalı" → SELL.
- Exclamatory Turkish conviction is a call: "Pat-la-ya-cak!" (will explode) → BUY; "Devam…!" (continues) →
  BUY; "yol uzun" (long way to go) → BUY; "bir dönem kapandı" (an era is over) → SELL; "köprü korkuluklarına
  yanaşmaca" (heading for the bridge railing) → SELL; "kaygıya gerek yok, henüz zirve yapmadı" → BUY.
- "Bears will never win", "anyone bearish doesn't understand", "daha yüksek seviyeler görülecek" → BUY.
- The author's stance may sit inside a video title or hashtag list; that still counts — but only when the title
  itself names the asset and a direction ("Altın Pat-la-ya-cak!"). A mood-only title with an asset hashtag list
  ("Sıkıntı Derinleşiyor!", "Kasırga Çok Yakın!", "Eylül Sıcaktı, Ekim Ateşşş!" + #altın #dolar #borsa) is NOT
  a call: which asset, which way is unknowable from the text.

Direction rules:
- Two-sided level conditionals ("above X positive, below X negative", "üzeri pozitif aşağısı negatifim",
  "holds → bulls rest, breaks → deeper") → NEUTRAL, confidence ≤ 0.5. Do not pick one side. But if one side
  is the author's base case and the other a mere hope ("aşağısı düşüşü derinleştirir, üzerinde tutunabilirse
  bir umudu olabilir") → the base case (SELL).
- Waiting to buy only after a crash the author expects ("MSTR batınca bolca alıcam") → NEUTRAL, not BUY.
- A pullback the author calls a correction inside an intact trend ("teknik düzeltmeden ibaret, orta-uzun vade
  trend değişmez") → the trend direction (BUY), horizon LONG, not NEUTRAL.
- A short-term bounce mentioned inside a larger opposite thesis ("büyük düşüş öncesi 119k mümkün") → label
  the dominant thesis (SELL), not the bounce.
- Resolve sarcasm from context: "müjdeniz yolda… bunlar girdikleri yeri kuruturlar" (your good news is coming…
  they dry up wherever they enter) → SELL; "boş yükseliş" (hollow rally) → SELL.
- One call per asset; only assets the author takes a stance on. A hashtag alone is not a stance.

Horizon (SHORT = 0-3 months, MEDIUM = 3-12 months, LONG = 1-5 years). Decide from the author's frame:
- SHORT: technical/level trading of any kind — "trend takip seviyem", daily-close conditions ("gün kapanışları",
  "üzeri pozitif aşağısı negatif"), chart patterns (TOBO/OBO, head-and-shoulders, "sağ omuz", "dönüş mumu"),
  nearby levels ("$3370 üzerinde, $3500 hedef"), indicator-implied pullback to a level, "yıkım olasılığı",
  intraday, "today", "this week", "en kısa vade", "stop", futures expiry, "hedge" positions, "getting closer
  to $X". A one-line exclamatory reaction to the current move ("köprü korkuluklarına yanaşmaca") is SHORT.
  If the author names a date, use it: a peak "in Sept-Oct 2025" written in early 2025 → MEDIUM, not LONG.
- MEDIUM: undated directional expectations with no level game and no structural argument — "daha yüksek
  seviyeler görülecek", "yukarı yolu var", "S&P 6200'e gider", "henüz zirve yapmadı", a price target with no
  date, "this year", "coming months", "if you liked it at 125k you should love it at 89k".
- LONG: valuation / structural / cycle / generational theses — every bubble/balon/"aşırı değerli"/"boş
  yükseliş" call, "orta-uzun vade", "uzun vadeciler için", "ana yükselişine vakit var", "yol uzun", "bir dönem
  kapandı", "Altın kalır siz gidersiniz", "kimsenin borcu değil", debasement, decoupling, dollar liquidity
  crisis, "torunlarınız", "sitting" (hold for years), "bears will never win", "revolutionize the economy".

price_target: a numeric level the author expects the asset to reach, in USD (BTC per coin, gold per troy oz,
SPX index points). Convert "100k" → 100000, "$4,500" → 4500. null if no explicit level. A target implies the
direction (target above current price → BUY, below → SELL) unless the author says otherwise. A level used only
as a conditional trigger ("above 116333 positive") is not a target.

Respond with JSON only:
{"is_call": bool, "calls": [{"asset": "BTC|GOLD|SPX", "direction": "BUY|SELL|NEUTRAL",
 "horizon": "SHORT|MEDIUM|LONG", "confidence": 0.0-1.0, "price_target": number|null,
 "quote": "<exact span from the tweet in its original language that justifies the label>"}]}
calls=[] when is_call=false."""

# Terse output contract for the local model: decode tokens are the whole cost on Apple silicon (a non-call answer
# is 12 tokens of JSON), so non-calls become one token and call JSON drops whitespace. Same rules, same fields.
TERSE_SUFFIX = """

OUTPUT FORMAT: apply every rule above exactly as written; only the output shape changes. Decide first whether the
tweet is a call (be as generous to calls as the rules allow). If it is NOT a call, reply with exactly {} .
If it IS a call, reply with compact JSON on one line, no spaces or newlines:
{"calls":[{"asset":"BTC","direction":"BUY","horizon":"SHORT","confidence":0.8,"price_target":null,"quote":"..."}]}"""
TERSE = os.environ.get("FINCLATOR_TERSE", "1") == "1"
THINK = os.environ.get("FINCLATOR_THINK", "0") == "1"   # Ollama native path: let Qwen3.6 reason before answering (slow)
THINK_PREDICT = int(os.environ.get("FINCLATOR_THINK_PREDICT", "1500"))  # num_predict budget incl. reasoning tokens


def pending(conn: sqlite3.Connection, limit: int | None = None, model: str | None = None) -> list[sqlite3.Row]:
    """Relevant tweets not yet classified by `model`."""
    model = model or MODEL
    q = """SELECT tweet_id AS id, handle, created_at, text, assets_hint FROM tweets
           WHERE relevant=1 AND tweet_id NOT IN (SELECT tweet_id FROM classified_by WHERE model=?)
           ORDER BY created_at DESC"""  # newest first: a usable matrix + matured recent trust arrive early
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, (model,)).fetchall()


def store_result(conn: sqlite3.Connection, tweet: sqlite3.Row | dict, result: dict, model: str,
                 gate_p: float | None = None) -> int:
    n = 0
    tid, handle, created = tweet["id"], tweet["handle"], tweet["created_at"]
    if result.get("is_call"):
        for c in result.get("calls", []):
            if c.get("asset") not in ("BTC", "GOLD", "SPX"):
                continue
            if c.get("direction") not in DIRECTIONS or c.get("horizon") not in HORIZONS:
                # DB gate: a model that emits horizon="NEUTRAL" or direction="HOLD" must not poison
                # evaluate/audit (both index MATURITY_DAYS[horizon]). Logged, tweet still marked classified.
                log(f"classify: dropped malformed call on {tid} ({model}): {json.dumps(c)[:200]}")
                continue
            pt = c.get("price_target")
            try:
                pt = float(pt) if pt not in (None, "", "null") else None
            except (TypeError, ValueError):
                pt = None
            conn.execute(
                """INSERT OR REPLACE INTO calls(tweet_id, handle, asset, direction, horizon, confidence, price_target,
                   quote, called_at, model, gate_p) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",  # unique per (tweet, asset, model)
                (tid, handle, c["asset"], c["direction"], c["horizon"], float(c.get("confidence", 0.5)), pt,
                 c.get("quote"), created, model, gate_p),
            )
            n += 1
    conn.execute("INSERT OR REPLACE INTO classified_by(tweet_id, model, at) VALUES(?,?,datetime('now'))", (tid, model))
    conn.execute("UPDATE tweets SET classified=1 WHERE tweet_id=?", (tid,))
    return n


# ---------- API mode ----------

BASE_URL = models.classifier_base_url()  # Ollama by default; None → Anthropic (claude-* models)
NUM_CTX = int(os.environ.get("FINCLATOR_NUM_CTX", "6144"))      # Ollama context per slot (default 262K → 32 GB KV). Keep ONE
# value for every request: a different num_ctx reloads the model and drops the prefix cache (2 s vs 0.15 s prefill).
WORKERS = int(os.environ.get("FINCLATOR_WORKERS", "1"))         # match OLLAMA_NUM_PARALLEL for the local path
MAX_TEXT = 3000  # X Premium long posts reach 25K chars; the call is in the head, and 3K keeps us inside NUM_CTX


def _user_msg(t) -> str:
    text = t["text"]
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + " […truncated]"
    return f"@{t['handle']} ({t['created_at'][:10]}), assets mentioned: {t['assets_hint']}\n\n{text}"


def _parse(raw: str) -> dict:
    s = raw.strip()
    if s in ("0", "\"0\"", "{}") or s.startswith("0\n") or s.startswith("0 "):  # terse non-call sentinel
        return {"is_call": False, "calls": []}
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        r = json.loads(raw)
        if not isinstance(r, dict):
            return {"is_call": False, "calls": []}
        if "is_call" not in r:  # terse call JSON carries only `calls`
            r["is_call"] = bool(r.get("calls"))
        return r
    except json.JSONDecodeError:
        return {"is_call": False, "calls": []}


def _ollama_chat(user: str, num_predict: int, num_ctx: int | None = None, json_mode: bool = True) -> str:
    """One Ollama /api/chat call with the shared SYSTEM prompt (prefix-cached per slot). Returns raw content."""
    import urllib.request

    url = BASE_URL.split("/v1")[0].rstrip("/") + "/api/chat"
    body = {
        "model": TEXT_MODEL, "stream": False, "keep_alive": "1h", "think": THINK,  # Qwen3.5+/3.6 ignore the /no_think tag
        "options": {"temperature": 0, "num_ctx": num_ctx or NUM_CTX,
                    "num_predict": max(num_predict, THINK_PREDICT) if THINK else num_predict},
        "messages": [{"role": "system", "content": SYSTEM + (TERSE_SUFFIX if TERSE else "") + ("" if THINK else "\n/no_think")},
                     {"role": "user", "content": user}],
    }
    if json_mode:  # JSON mode: with TERSE the non-call sentinel is `{}` (2 tokens), still valid JSON
        body["format"] = "json"
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["message"]["content"] or ""


# ---------- batched mode (local model only) ----------

BATCH_SIZE = int(os.environ.get("FINCLATOR_BATCH_SIZE", "4"))  # >1 → several tweets per request. 4 = variant C
# (holdout 2026-09-21: is_call .97, F1 .82, hor .88 vs .92/.67/.67 single-tweet); production default.
BATCH_MAX_TEXT = 1200  # per-tweet clip inside a batch; longer tweets are sent alone
BATCH_SUFFIX = """

You will receive several INDEPENDENT tweets, each with an integer id. Judge each one on its own text only —
never let one tweet's stance, asset or horizon influence another. Respond with compact JSON on one line, no
whitespace or newlines: {"results":[{"id":<id>,"is_call":bool,"calls":[...]},...]} with exactly one entry per id.
The `calls` entries use the same schema as above."""


def _batch_user_msg(rows) -> str:
    parts = []
    for i, t in enumerate(rows):
        text = t["text"]
        if len(text) > BATCH_MAX_TEXT:
            text = text[:BATCH_MAX_TEXT] + " […truncated]"
        parts.append(f"### id={i} @{t['handle']} ({t['created_at'][:10]}), assets mentioned: {t['assets_hint']}\n{text}")
    return "\n\n".join(parts)


def make_batch_classifier():
    """Return (fn(list[tweet_row]) -> list[result dict], model). Packs up to BATCH_SIZE tweets per Ollama request;
    any tweet missing from / malformed in the batch answer, or longer than BATCH_MAX_TEXT, falls back to a
    single-tweet call so batching can only lose accuracy through cross-tweet contamination, never through parsing."""
    single, model = make_classifier()

    def run_batch(rows) -> list[dict]:
        rows = list(rows)
        out: list = [None] * len(rows)
        small = [i for i, t in enumerate(rows) if len(t["text"]) <= BATCH_MAX_TEXT]
        if len(small) >= 2:
            sub = [rows[i] for i in small]
            try:
                raw = _ollama_chat(_batch_user_msg(sub) + BATCH_SUFFIX, 400 + 200 * len(sub))
                r = _parse(raw)
                for item in r.get("results", []) if isinstance(r.get("results"), list) else []:
                    try:
                        k = int(item.get("id"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if 0 <= k < len(sub) and isinstance(item.get("is_call"), bool):
                        out[small[k]] = {"is_call": item["is_call"], "calls": item.get("calls") or []}
            except Exception:  # noqa: BLE001 — whole batch failed: everything falls back to single mode below
                pass
        for i, t in enumerate(rows):
            if out[i] is None:
                out[i] = single(t)
        return out
    return run_batch, model


def make_classifier():
    """Return (fn(tweet_row) -> result dict, model_name). Anthropic by default; Ollama native API if BASE_URL points
    at :11434 (the only way to pass num_ctx — Ollama's /v1 endpoint silently drops `options`); otherwise any
    OpenAI-compatible server. Same SYSTEM prompt and parser on every path."""
    if BASE_URL and ":11434" in BASE_URL:
        def run(t):
            return _parse(_ollama_chat(_user_msg(t), 600))
        return run, MODEL

    if BASE_URL:
        from openai import OpenAI

        client = OpenAI(base_url=BASE_URL, api_key=os.environ.get("FINCLATOR_MODEL_API_KEY", "local"))

        def run(t):
            r = client.chat.completions.create(
                model=TEXT_MODEL, temperature=0, max_tokens=600,
                messages=[{"role": "system", "content": SYSTEM + "\n/no_think"},
                          {"role": "user", "content": _user_msg(t)}],
                response_format={"type": "json_object"},
            )
            return _parse(r.choices[0].message.content or "")
        return run, MODEL

    from anthropic import Anthropic

    client = Anthropic()

    def run(t):
        msg = client.messages.create(
            model=TEXT_MODEL, max_tokens=600, system=SYSTEM, temperature=0,
            messages=[{"role": "user", "content": _user_msg(t)}],
        )
        return _parse("".join(getattr(b, "text", "") for b in msg.content))
    return run, MODEL


def _reusable(conn, tweet_ids: list[str], base: str) -> tuple[dict[str, list[dict]], set[str]]:
    """Existing labels of the plain text-model dimension: {tweet_id: [call dicts]} for its calls, and the set of
    tweet ids it has already classified (with or without a call). Lets the hybrid dimension skip the GPU for tweets
    the same text model already labeled — temperature 0, same prompt, so the label is the one it would emit again."""
    calls: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for i in range(0, len(tweet_ids), 500):
        chunk = tweet_ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT tweet_id FROM classified_by WHERE model=? AND tweet_id IN ({ph})", [base, *chunk]):
            seen.add(r["tweet_id"])
        for r in conn.execute(f"""SELECT tweet_id, asset, direction, horizon, confidence, price_target, quote
                                  FROM calls WHERE model=? AND tweet_id IN ({ph})""", [base, *chunk]):
            calls.setdefault(r["tweet_id"], []).append(
                {"asset": r["asset"], "direction": r["direction"], "horizon": r["horizon"],
                 "confidence": r["confidence"], "price_target": r["price_target"], "quote": r["quote"]})
    return calls, seen


def classify_pending(conn: sqlite3.Connection, limit: int | None = None) -> tuple[int, int]:
    """Classify pending tweets. With the Jev gate on (models.GATE_ON), every pending tweet is gated first
    (src/gate.py, stored once per Jev version): failing tweets are stored as non-calls without touching the text
    model; passing tweets take the text model's existing label when it already classified them (GATE_REUSE) and
    otherwise go through the text model. On the local Ollama path with BATCH_SIZE > 1, BATCH_SIZE tweets go in one
    request (variant C in data/tune_variants.txt; make_batch_classifier falls back per tweet on a malformed answer);
    otherwise one request per tweet. WORKERS requests in flight; a failed request is logged and its tweets stay
    pending, never aborting the run. Only the main thread touches the connection."""
    from concurrent.futures import ThreadPoolExecutor

    batched = BATCH_SIZE > 1 and bool(BASE_URL) and ":11434" in BASE_URL
    model = MODEL
    rows = pending(conn, limit, model)
    tweets_done = calls_made = failed = 0
    gate_p: dict[str, float] = {}
    if GATE_ON and rows:
        from . import gate

        g = gate.ensure(conn, rows)
        gated = [t for t in rows if t["id"] in g]           # ungated (Jev failed) stay pending
        gate_p = {tid: x["p_call"] for tid, x in g.items()}
        passing, blocked = [], []
        for t in gated:
            (passing if gate.passes(g[t["id"]]["p_call"], g[t["id"]]["stances"]) else blocked).append(t)
        for t in blocked:
            store_result(conn, t, {"is_call": False, "calls": []}, model, gate_p[t["id"]])
            tweets_done += 1
        reused = 0
        if GATE_REUSE and passing and models.base_model(model) != model:
            old_calls, seen = _reusable(conn, [t["id"] for t in passing], models.base_model(model))
            rest = []
            for t in passing:
                if t["id"] in seen:
                    cs = old_calls.get(t["id"], [])
                    calls_made += store_result(conn, t, {"is_call": bool(cs), "calls": cs}, model, gate_p[t["id"]])
                    tweets_done += 1
                    reused += 1
                else:
                    rest.append(t)
            passing = rest
        conn.commit()
        log(f"classify [{model}]: gate blocked {len(blocked):,}, reused {reused:,} text-model labels, "
            f"{len(passing):,} → text model, {len(rows) - len(gated)} ungated")
        rows = passing
    if not rows:
        conn.commit()
        return tweets_done, calls_made

    if batched:
        run_batch, _ = make_batch_classifier()
    else:
        run_single, _ = make_classifier()

        def run_batch(chunk):
            return [run_single(t) for t in chunk]
    size = BATCH_SIZE if batched else 1
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)]

    def safe(chunk):
        try:
            return chunk, run_batch(chunk), None
        except Exception as e:  # noqa: BLE001 — any transport/model error: skip these tweets, keep going
            return chunk, None, e

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for chunk, results, err in pool.map(safe, chunks):
            if err is not None:
                failed += len(chunk)
                log(f"classify: {','.join(t['id'] for t in chunk)} failed: {type(err).__name__}: {str(err)[:120]}")
                continue
            for t, result in zip(chunk, results, strict=True):
                calls_made += store_result(conn, t, result, model, gate_p.get(t["id"]))
                tweets_done += 1
                if tweets_done % 50 == 0:
                    conn.commit()
                    log(f"classify [{model}{f' batch{size}' if batched else ''}]: {tweets_done}/{len(rows)} ({calls_made} calls, {failed} failed)")
    conn.commit()
    if failed:
        log(f"classify: {failed} tweets failed and remain pending")
    return tweets_done, calls_made


# ---------- interactive mode ----------

def export_pending(conn: sqlite3.Connection, path: Path, limit: int | None = None, model: str | None = None) -> int:
    rows = pending(conn, limit, model)
    with open(path, "w") as f:
        for t in rows:
            f.write(json.dumps({"id": t["id"], "handle": t["handle"], "created_at": t["created_at"],
                                "assets_hint": t["assets_hint"], "text": t["text"]}, ensure_ascii=False) + "\n")
    return len(rows)


def import_labels(conn: sqlite3.Connection, path: Path, model: str) -> tuple[int, int]:
    """JSONL lines: {"id": ..., "is_call": ..., "calls": [...]}"""
    tweets_done = calls_made = 0
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        t = conn.execute("SELECT tweet_id AS id, handle, created_at FROM tweets WHERE tweet_id=?", (r["id"],)).fetchone()
        if not t:
            continue
        calls_made += store_result(conn, t, r, model)
        tweets_done += 1
    conn.commit()
    return tweets_done, calls_made


if __name__ == "__main__":
    import sys
    conn = connect()
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        print(export_pending(conn, Path(sys.argv[2])), "exported")
    elif len(sys.argv) > 1 and sys.argv[1] == "import":
        print(import_labels(conn, Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "manual"))
    else:
        print(classify_pending(conn))
