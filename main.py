#!/usr/bin/env python3
import time, requests, pandas as pd, yfinance as yf, concurrent.futures, threading, json, os
from datetime import datetime, timedelta

TELEGRAM_TOKEN = "8271389788:AAGjdLpIlqWOv2JdHiEQ8qBh9BHsdqQJN8Q"
TELEGRAM_CHAT  = "1466980508"

MAX_PRICE = 25.0; MIN_PRICE = 0.8; MIN_GAP_DAILY_PCT = 6.0; MIN_GAP_5M_PCT = 0.45
TOP_RUNNERS = 80; MAX_WORKERS = 8; LIVE_CHECK_SEC = 7; MONITOR_HOURS = 10
ALERT_LOG = "alerted.json"; COOLDOWN_MINUTES = 60
ACTIVE_ALERTS = {}; ALERT_LOCK = threading.Lock()

def tg(msg): 
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

def already_alerted(s, e): 
    if not os.path.exists(ALERT_LOG): return False
    try:
        with open(ALERT_LOG) as f: data = json.load(f)
        key = f"{s}_{e:.3f}"
        if key in data and (datetime.now() - datetime.fromisoformat(data[key])).total_seconds() < COOLDOWN_MINUTES*60: return True
    except: pass
    return False

def mark_alerted(s, e):
    data = {}
    if os.path.exists(ALERT_LOG):
        try: data = json.load(open(ALERT_LOG))
        except: pass
    data[f"{s}_{e:.3f}"] = datetime.now().isoformat()
    with open(ALERT_LOG, "w") as f: json.dump(data, f)

def get_live_runners():
    url = "https://finviz.com/screener.ashx?v=111&f=sh_avgvol_o500,sh_curvol_o400,sh_price_o0.8,sh_price_u25,ta_change_o5&o=-change"
    try:
        df = pd.read_html(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text)[-1]
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        return df["ticker"].dropna().astype(str).tolist()[:TOP_RUNNERS]
    except: return []

def detect_setup(sym):
    try:
        t = yf.Ticker(sym); df = t.history(period="7d", interval="5m", prepost=True)
        if len(df) < 50: return None
        price = df["Close"].iloc[-1]
        daily = t.history(period="5d", interval="1d")
        if len(daily) >= 2:
            pc, to = daily["Close"].iloc[-2], daily["Open"].iloc[-1]
            gap = abs(to - pc) / pc * 100
            if gap >= MIN_GAP_DAILY_PCT:
                dir = "LONG" if to > pc else "SHORT"
                low, high = min(pc,to), max(pc,to)
                entry = (low + high) / 2
                sl = pc * (0.98 if dir=="LONG" else 1.02)
                tp = entry + (entry - sl) * (2.5 if dir=="LONG" else -2.5)
                status = "DAILY GAP - IN ZONE" if low <= price <= high else "DAILY GAP - WATCH"
                return {"symbol":sym,"type":dir,"price":price,"entry":entry,"sl":sl,"tp":tp,"rr":2.5,"gap":round(gap,2),"status":status,"kind":"DAILY"}
        c0,c1 = df.iloc[-3],df.iloc[-2]
        if c1["Low"] > c0["High"]:
            g = (c1["Low"]-c0["High"])/price*100
            if g >= MIN_GAP_5M_PCT:
                entry = (c0["High"]+c1["Low"])/2
                return {"symbol":sym,"type":"LONG","price":price,"entry":entry,"sl":c0["Low"]*0.99,"tp":entry+(entry-c0["Low"]*0.99)*2.3,"rr":2.3,"gap":round(g,2),"status":"5M FVG LONG","kind":"5MIN"}
        if c1["High"] < c0["Low"]:
            g = (c0["Low"]-c1["High"])/price*100
            if g >= MIN_GAP_5M_PCT:
                entry = (c1["High"]+c0["Low"])/2
                return {"symbol":sym,"type":"SHORT","price":price,"entry":entry,"sl":c0["High"]*1.01,"tp":entry-(c0["High"]*1.01-entry)*2.3,"rr":2.3,"gap":round(g,2),"status":"5M FVG SHORT","kind":"5MIN"}
    except: pass
    return None

def live_monitor(setup):
    sym, entry, sl, dir = setup["symbol"], setup["entry"], setup["sl"], setup["type"]
    start = datetime.now()
    while (datetime.now()-start) < timedelta(hours=MONITOR_HOURS):
        try:
            p = yf.Ticker(sym).history(period="1d", interval="1m")["Close"].iloc[-1]
            if dir=="LONG" and p <= entry and not already_alerted(sym, entry):
                tg(f"EXECUTE LONG → {sym}\n${p:.3f} ≤ ${entry:.3f}\nSL ${sl:.3f} | RR 1:{setup['rr']}\n{setup['status']}")
                mark_alerted(sym, entry); break
            if dir=="SHORT" and p >= entry and not already_alerted(sym, entry):
                tg(f"EXECUTE SHORT → {sym}\n${p:.3f} ≥ ${entry:.3f}\nSL ${sl:.3f} | RR 1:{setup['rr']}\n{setup['status']}")
                mark_alerted(sym, entry); break
        except: pass
        time.sleep(LIVE_CHECK_SEC)

def run():
    print(f"Sniper running → {datetime.now().strftime('%H:%M:%S')}")
    runners = get_live_runners()
    if not runners: tg("No runners"); return
    setups = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for f in concurrent.futures.as_completed(pool.submit(detect_setup, s) for s in runners):
            r = f.result()
            if r:
                setups.append(r)
                if "IN ZONE" in r["status"] or r["gap"] > 8:
                    with ALERT_LOCK:
                        if r["symbol"] not in ACTIVE_ALERTS:
                            threading.Thread(target=live_monitor, args=(r,), daemon=True).start()
                            ACTIVE_ALERTS[r["symbol"]] = True
    if not setups: tg("No setups"); return
    setups.sort(key=lambda x: (-x["gap"], x["kind"]=="DAILY"))
    msg = f"<b>SNIPER • {len(setups)} SETUPS</b>\n"
    for s in setups[:12]:
        msg += f"{'>' if s['type']=='LONG' else '<'} <b>{s['symbol']}</b> ${s['price']:.2f} | Gap {s['gap']}%\nEntry <b>${s['entry']:.3f}</b> | {s['status']}\n──────────\n"
    tg(msg)

while True:
    run()
    time.sleep(300)  # Run every 5 minutes
