"""Audit tab: every call with tweet, highlighted quote, prices, outcome, verify links, review flags and raw JSON.
Plus debug samples (classifier "not a call", prefilter rejects, price gaps, models). Rendered inside the admin chrome
(`admin._page`) at /audit and also written to data/audit.html as a static copy by src.run.
"""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .db import connect
from .evaluate import MATURITY_DAYS, TARGET_RATIO, TARGET_RATIO_BY_ASSET
from .models import active_model, list_models

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "audit.html"

TV = {"BTC": "BITSTAMP:BTCUSD", "GOLD": "COMEX:GC1!", "SPX": "SP:SPX"}
YH = {"BTC": "BTC-USD", "GOLD": "GC=F", "SPX": "%5EGSPC"}
HZ = {"SHORT": "0–3 mo · eval 90d", "MEDIUM": "3–12 mo · eval 365d", "LONG": "1–5 y · eval 730d"}

# Extra CSS/JS for this tab only (admin CSS provides the base: dark theme, nav, cards, tables, .BUY/.SELL, sortable).
CSS = """
#calls td{vertical-align:top}.tweet{max-width:520px;white-space:pre-wrap;word-break:break-word}
mark{background:#6b5a00;color:#fff;padding:0 2px;border-radius:2px}
tr.CORRECT>td{background:#17301f}tr.WRONG>td{background:#3a1b18}tr.PARTIAL>td{background:#3a3212}
.dir.BUY,.dir.SELL,.dir.NEUTRAL{background:none;font-weight:700}.dir.BUY{color:#7ddc8a}.dir.SELL{color:#ff7b6b}.dir.NEUTRAL{color:#aaa}
.hit1{color:#7ddc8a}.hit0{color:#ff7b6b}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px;align-items:center}
select,input{font:inherit;padding:3px 6px;background:#181b22;color:#e6e6e6;border:1px solid #2a2f3a;border-radius:4px}
.pill{background:#2a2f3a;border-radius:10px;padding:1px 8px;font-size:12px;white-space:nowrap}
.flag{display:inline-block;background:#4a1f1a;color:#ff9b8b;border-radius:4px;padding:0 5px;font-size:11px;margin:2px 4px 0 0}
.x{cursor:pointer;color:#9ecbff;font-size:11px}tr.raw>td{background:#0a0c10;padding:6px 10px}
tr.raw pre{margin:0;max-height:none}small{color:#9aa}a{color:#9ecbff}.hidden{display:none!important}
th.sa:after{content:" ▲"}th.sd:after{content:" ▼"}.help{color:#9aa;font-size:12px;margin:0 0 8px}
.tweet.clamp{max-height:11em;overflow:hidden;position:relative;cursor:pointer}.tweet.clamp:after{content:"… show all";position:absolute;right:0;bottom:0;background:#181b22;color:#9ecbff;padding:0 6px;font-size:11px}
"""

JS = """
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
function v(id){return $('#'+id).value}
function filt(){const a=v('f-acc'),s=v('f-asset'),h=v('f-hz'),r=v('f-res'),d=v('f-dir'),m=v('f-flag'),q=v('f-q').toLowerCase();let n=0;
 $$('#calls tbody tr.row').forEach(tr=>{const D=tr.dataset;
 const ok=(!a||D.acc===a)&&(!s||D.asset===s)&&(!h||D.hz===h)&&(!r||D.res===r)&&(!d||D.dir===d)&&(!m||D.flags.split(' ').includes(m))&&(!q||tr.textContent.toLowerCase().includes(q));
 tr.classList.toggle('hidden',!ok);if(!ok)$('#raw-'+D.id).classList.add('hidden');if(ok)n++;});$('#f-n').textContent=n+' shown';
 const p=new URLSearchParams();for(const [k,id] of [['acc','f-acc'],['asset','f-asset'],['hz','f-hz'],['res','f-res'],['dir','f-dir'],['flag','f-flag'],['q','f-q']]){if(v(id))p.set(k,v(id))}
 history.replaceState(null,'',location.pathname+(p.toString()?'?'+p:''))}
function toggleRaw(id){$('#raw-'+id).classList.toggle('hidden')}
function unclamp(td){td.classList.remove('clamp')}
function copyRow(id){navigator.clipboard.writeText($('#raw-'+id+' pre').textContent)}
function setFlag(f){$('#f-flag').value=f;filt();$('#calls').scrollIntoView()}
document.addEventListener('DOMContentLoaded',()=>{
 const p=new URLSearchParams(location.search);for(const [k,id] of [['acc','f-acc'],['asset','f-asset'],['hz','f-hz'],['res','f-res'],['dir','f-dir'],['flag','f-flag'],['q','f-q']]){if(p.get(k))$('#'+id).value=p.get(k)}
 ['f-acc','f-asset','f-hz','f-res','f-dir','f-flag'].forEach(id=>$('#'+id).onchange=filt);$('#f-q').oninput=filt;filt();
 document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement.tagName!=='INPUT'){e.preventDefault();$('#f-q').focus()}});
 // keep raw rows attached to their call row after admin's sortable reorders tbody
 new MutationObserver(()=>{$$('#calls tbody tr.row').forEach(r=>{const raw=$('#raw-'+r.dataset.id);if(raw&&r.nextElementSibling!==raw)r.after(raw)})}).observe($('#calls tbody'),{childList:true});
});
"""


def _yahoo(asset: str, d: str) -> str:
    ts = int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())
    return f"https://finance.yahoo.com/quote/{YH[asset]}/history/?period1={ts - 5 * 86400}&period2={ts + 5 * 86400}"


def _tv(asset: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={TV[asset]}"


def _hl(text: str, quote: str | None) -> str:
    t = html.escape(text)
    if quote and quote in text:
        q = html.escape(quote)
        t = t.replace(q, f"<mark>{q}</mark>", 1)
    return t


def _fmt(x, nd=2):
    return "" if x is None else f"{x:,.{nd}f}"


def _flags(r) -> list[str]:
    """Automatic review hints — where a human should look first."""
    f = []
    if r["horizon"] not in MATURITY_DAYS:
        f.append("bad-horizon")
    if r["quote"] and r["quote"] not in r["text"]:
        f.append("quote-mismatch")
    if r["confidence"] < 0.5:
        f.append("low-conf")
    if "gate_p" in r.keys() and r["gate_p"] is not None and r["gate_p"] < 0.5:
        f.append("low-gate")
    if r["direction"] == "NEUTRAL":
        f.append("neutral")
    if r["price_target"] and r["entry_close"]:
        ratio = r["price_target"] / r["entry_close"]
        lo, hi = TARGET_RATIO_BY_ASSET.get(r["asset"], TARGET_RATIO)
        if not (lo <= ratio <= hi):
            f.append("target-units")
        elif (r["direction"] == "BUY" and ratio <= 1) or (r["direction"] == "SELL" and ratio >= 1):
            f.append("target-vs-direction")
    if r["result"] == "WRONG" and r["confidence"] >= 0.7:
        f.append("confident-wrong")
    if r["result"] and r["actual"] == "NEUTRAL" and r["result"] == "PARTIAL":
        f.append("flat-market")
    return f


def body(conn, model: str | None = None, limit: int | None = None, account: str | None = None) -> str:
    """HTML fragment for the audit tab (no chrome). `limit`/`account` cap the rendered rows (hosted panel) — result
    counts and the account list still cover every call of the model; flag pills count the rendered rows only."""
    model = model or active_model()
    e = html.escape
    # Pass 1 — every call of the model, no tweet text: result counts, account list, and which ids to render.
    idx = conn.execute("""
        SELECT c.id, c.handle, o.result FROM calls c LEFT JOIN outcomes o ON o.call_id = c.id
        WHERE c.model = ? ORDER BY c.called_at DESC""", (model,)).fetchall()
    res_counts = {k: 0 for k in ("CORRECT", "PARTIAL", "WRONG", "PENDING")}
    for r in idx:
        res_counts[r["result"] or "PENDING"] += 1
    others = [m for m in list_models(conn) if m != model]
    handles = sorted({r["handle"] for r in idx})
    total = len(idx)
    if account:
        idx = [r for r in idx if r["handle"] == account]
    if limit and len(idx) > limit:
        idx = idx[:limit]
    capped = len(idx) < total
    # Pass 2 — full rows (with tweet text) only for what is rendered; the whole backlog's text is ~25 MB over the wire.
    rows = []
    ids = [r["id"] for r in idx]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows += conn.execute(f"""
            SELECT c.id, c.handle, c.asset, c.direction, c.horizon, c.confidence, c.price_target, c.quote, c.called_at,
                   c.tweet_id, c.model, c.gate_p, t.text, t.assets_hint, o.entry_date, o.exit_date, o.entry_close,
                   o.exit_close, o.return_pct, o.threshold_pct, o.actual, o.result, o.target_hit, o.extreme
            FROM calls c JOIN tweets t ON t.id = c.tweet_id LEFT JOIN outcomes o ON o.call_id = c.id
            WHERE c.id IN ({','.join('?' * len(chunk))})""", chunk).fetchall()
    rows.sort(key=lambda r: r["called_at"], reverse=True)
    flag_counts: dict[str, int] = {}   # over the rendered rows — the pills filter the rendered table
    row_flags: dict[int, list[str]] = {}
    for r in rows:
        fl = _flags(r)
        row_flags[r["id"]] = fl
        for x in fl:
            flag_counts[x] = flag_counts.get(x, 0) + 1

    B = [f"<style>{CSS}</style><script>{JS}</script>"]
    B.append(f"<h2>Calls <small>{total} · model <b>{e(model)}</b>{' · also in DB: ' + ', '.join(e(m) for m in others) if others else ''}</small></h2>")
    if capped:
        from .admin import _u
        opts = "".join(f"<option value='{e(h)}'{' selected' if h == account else ''}>@{e(h)}</option>" for h in handles)
        B.append(f"<p class=help><b>Showing {len(rows)} of {total} calls</b> (newest first{', @' + e(account) if account else ''}). "
                 f"Pick an account to see its full history: <select onchange=\"location.search='?account='+encodeURIComponent(this.value)\">"
                 f"<option value=''>all accounts (newest {limit})</option>{opts}</select>"
                 f" · <a href='{_u('/audit')}?all=1'>render all {total}</a></p>")
    B.append("<div class=filters>")
    B.append(f"<span class=pill><span class=hit1>CORRECT {res_counts['CORRECT']}</span> · <span style='color:#f0b64c'>PARTIAL {res_counts['PARTIAL']}</span> · "
             f"<span class=hit0>WRONG {res_counts['WRONG']}</span> · pending {res_counts['PENDING']}</span>")
    for k, n in sorted(flag_counts.items(), key=lambda x: -x[1]):
        B.append(f"<span class='pill flagpill' style='cursor:pointer' onclick=\"setFlag('{k}')\">{k} <b>{n}</b></span>")
    B.append("</div>")
    B.append(f"""<div class=filters>
<select id=f-acc><option value="">all accounts</option>{''.join(f'<option>{e(h)}</option>' for h in handles)}</select>
<select id=f-asset><option value="">all assets</option><option>BTC</option><option>GOLD</option><option>SPX</option></select>
<select id=f-hz><option value="">all horizons</option><option>SHORT</option><option>MEDIUM</option><option>LONG</option></select>
<select id=f-dir><option value="">all directions</option><option>BUY</option><option>SELL</option><option>NEUTRAL</option></select>
<select id=f-res><option value="">all results</option><option>CORRECT</option><option>PARTIAL</option><option>WRONG</option><option value=PENDING>pending</option></select>
<select id=f-flag><option value="">all flags</option>{''.join(f'<option value="{k}">{k} ({n})</option>' for k, n in sorted(flag_counts.items()))}</select>
<input id=f-q placeholder="search text… ( / )" size=26> <span id=f-n class=pill></span></div>
<p class=help><b>Verify a row:</b> <i>tweet ↗</i> → read the highlighted quote → judge asset / direction / horizon / target.
<i>entry</i> / <i>exit</i> open Yahoo history (±5 d) to check closes; <i>TV</i> opens the chart. <i>raw</i> shows the stored record as JSON.
<i>market did</i> = realised direction vs the flat band; a NEUTRAL call scores WRONG when the market moved beyond it either way.
<i>target</i>: HIT / miss earn ±0.25; <i>ignored</i> = wrong side of the entry or a unit error, no credit either way.
<i>gate</i> = Jev is_call probability; <code>low-gate</code> flags &lt; 0.5.
Flags are automatic review hints, not errors. Filters are kept in the URL — share it.</p>
<table id=calls class=sortable><thead><tr><th>#</th><th>account · date</th><th>tweet (quote highlighted)</th><th>asset</th>
<th>call</th><th>horizon</th><th>conf</th><th title='Jev gate p_call (is_call probability, blank for calls labeled before the gate)'>gate</th><th>target</th><th>entry</th><th>exit</th><th>return</th><th>market did</th><th>result</th><th>verify</th></tr></thead><tbody>""")
    for r in rows:
        d = r["called_at"][:10]
        res = r["result"] or "PENDING"
        fl = row_flags[r["id"]]
        if r["result"]:
            entry = f"{r['entry_date']}<br>{_fmt(r['entry_close'])}"
            exit_ = f"{r['exit_date']}<br>{_fmt(r['exit_close'])}"
            ret = f"{r['return_pct']:+.1f}%<br><small>flat ±{r['threshold_pct']:.1f}%</small>"
            actual = f"<span class='dir {r['actual']}'>{r['actual']}</span>"
            result = r["result"]
            verify = (f"<a href='{_yahoo(r['asset'], r['entry_date'])}'>entry</a> · <a href='{_yahoo(r['asset'], r['exit_date'])}'>exit</a>"
                      f" · <a href='{_tv(r['asset'])}'>TV</a>")
        else:
            days = MATURITY_DAYS.get(r["horizon"])
            entry = exit_ = ret = actual = ""
            if days is None:  # malformed label (e.g. horizon="NEUTRAL") — never evaluated; flagged, not fatal
                result = "<small>invalid horizon</small>"
            else:
                result = f"<small>matures {date.fromisoformat(d) + timedelta(days=days)}</small>"
            verify = f"<a href='{_tv(r['asset'])}'>TV</a>"
        if r["price_target"]:
            tgt = _fmt(r["price_target"], 0)
            if r["target_hit"] is not None:
                tgt += f"<br><small class=hit{r['target_hit']}>{'HIT' if r['target_hit'] else 'miss'} · ext {_fmt(r['extreme'], 0)}</small>"
            elif r["result"] and ("target-units" in fl or "target-vs-direction" in fl):
                tgt += "<br><small title='wrong side of entry or implausible ratio: no ±0.25'>ignored</small>"
        else:
            tgt = "<small>–</small>"
        raw = {k: r[k] for k in r.keys() if k != "text"}
        clamp = " clamp" if len(r["text"]) > 600 else ""
        gate = f"{r['gate_p']:.2f}" if r["gate_p"] is not None else "<small>–</small>"
        B.append(f"""<tr class="row {r['result'] or ''}" data-id={r['id']} data-acc="{e(r['handle'])}" data-asset="{r['asset']}" data-hz="{r['horizon']}"
 data-res="{res}" data-dir="{r['direction']}" data-flags="{' '.join(fl)}">
<td data-v={r['id']}>{r['id']}<br><span class=x onclick="toggleRaw({r['id']})">raw</span></td>
<td>@{e(r['handle'])}<br><small>{d}</small><br><a href='https://x.com/{r['handle']}/status/{r['tweet_id']}'>tweet ↗</a></td>
<td class='tweet{clamp}' onclick="unclamp(this)">{_hl(r['text'], r['quote'])}{'<br>' if fl else ''}{''.join(f'<span class=flag>{x}</span>' for x in fl)}</td>
<td>{r['asset']}<br><small>{e(r['assets_hint'] or '')}</small></td>
<td class='dir {r['direction']}'>{r['direction']}</td><td>{r['horizon']}<br><small>{HZ.get(r['horizon'], '?')}</small></td><td class=num>{r['confidence']:.2f}</td>
<td class=num>{gate}</td><td class=num>{tgt}</td><td class=num>{entry}</td><td class=num>{exit_}</td><td class=num data-v='{r['return_pct'] or 0}'>{ret}</td>
<td>{actual}</td><td>{result}</td><td>{verify}</td></tr>
<tr id=raw-{r['id']} class="raw hidden"><td colspan=15><span class=x onclick="copyRow({r['id']})">copy JSON</span>
<pre>{e(json.dumps(raw, ensure_ascii=False, indent=1))}</pre></td></tr>""")
    B.append("</tbody></table>")

    # ---- debug samples
    rej = conn.execute("""SELECT handle, created_at, text, assets_hint, id FROM tweets WHERE relevant=1
                          AND id IN (SELECT tweet_id FROM classified_by WHERE model=?)
                          AND id NOT IN (SELECT tweet_id FROM calls WHERE model=?) ORDER BY random() LIMIT 40""",
                       (model, model)).fetchall()
    B.append("<h2>Classifier said “not a call” <small>40 random asset-mentioning tweets the active model labeled not-a-call — look for missed calls</small></h2><table>")
    for r in rej:
        B.append(f"<tr><td style='white-space:nowrap'><a href='https://x.com/{r['handle']}/status/{r['id']}'>@{e(r['handle'])}</a><br><small>{r['created_at'][:10]} · {r['assets_hint']}</small></td><td class=tweet>{e(r['text'])}</td></tr>")
    B.append("</table>")
    norel = conn.execute("SELECT handle, created_at, text, id FROM tweets WHERE relevant=0 ORDER BY random() LIMIT 40").fetchall()
    B.append("<h2>Prefilter dropped <small>40 random tweets that matched no asset regex — look for missed asset words</small></h2><table>")
    for r in norel:
        B.append(f"<tr><td style='white-space:nowrap'><a href='https://x.com/{r['handle']}/status/{r['id']}'>@{e(r['handle'])}</a><br><small>{r['created_at'][:10]}</small></td><td class=tweet>{e(r['text'])}</td></tr>")
    B.append("</table>")
    B.append("<h2>Price coverage <small>· BTC-USD · GC=F (COMEX front month, not spot) · ^GSPC · Yahoo daily close</small></h2><table><tr><th>asset</th><th>from</th><th>to</th><th>rows</th><th>last close</th><th>gaps &gt; 5 d</th></tr>")
    for r in conn.execute("SELECT asset, min(date) a, max(date) b, count(*) n FROM prices GROUP BY asset"):
        last = conn.execute("SELECT close FROM prices WHERE asset=? ORDER BY date DESC LIMIT 1", (r["asset"],)).fetchone()[0]
        dates = [x[0] for x in conn.execute("SELECT date FROM prices WHERE asset=? ORDER BY date", (r["asset"],))]
        gaps = sum(1 for x, y in zip(dates, dates[1:]) if (date.fromisoformat(y) - date.fromisoformat(x)).days > 5)
        B.append(f"<tr><td>{r['asset']}</td><td>{r['a']}</td><td>{r['b']}</td><td class=num>{r['n']}</td><td class=num>{_fmt(last)}</td><td class=num>{gaps}</td></tr>")
    B.append("</table>")
    B.append("<h2>Models in DB <small>· every count on every tab is for one model; the active one is published</small></h2><table><tr><th>model</th><th title='relevant tweets labeled by this model'>classified</th><th title='relevant tweets not yet labeled by this model'>pending</th><th>calls</th><th>evaluated</th></tr>")
    for m in list_models(conn):
        c = conn.execute("""SELECT (SELECT count(*) FROM classified_by WHERE model=?) a, (SELECT count(*) FROM calls WHERE model=?) b,
                            (SELECT count(*) FROM outcomes o JOIN calls c ON c.id=o.call_id WHERE c.model=?) d,
                            (SELECT count(*) FROM tweets t WHERE t.relevant=1
                               AND t.id NOT IN (SELECT tweet_id FROM classified_by WHERE model=?)) p""", (m, m, m, m)).fetchone()
        B.append(f"<tr><td>{e(m)}{' <b>(active)</b>' if m == model else ''}</td><td class=num>{c['a']:,}</td><td class=num>{c['p']:,}</td>"
                 f"<td class=num>{c['b']:,}</td><td class=num>{c['d']:,}</td></tr>")
    B.append("</table>")
    return "".join(B)


def render(model: str | None = None) -> str:
    """Full page (admin chrome + audit body). Also refreshes the static copy."""
    from .admin import _page
    conn = connect()
    page = _page("audit", body(conn, model), "/audit")
    OUT.write_text(page)
    return page


def build(model: str | None = None) -> Path:
    render(model)
    return OUT


if __name__ == "__main__":
    print(build())
