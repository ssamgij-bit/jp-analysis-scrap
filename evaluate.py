"""소스 평가: 최근 게시 여부와 일본 개별주 분석 비중."""
from datetime import datetime, timezone, timedelta

from common import (fetch_feed, entry_time, entry_text, find_tickers, fin_score, has_japan,
                    fetch_tg_channel)

ACTIVE_DAYS = 60


def classify_post(title, text):
    """소스 평가용 글 분류(보수적): 강한 종목 표기, 또는 약한 표기+재무용어 2개 이상."""
    blob = f"{title}\n{text}"
    strong = find_tickers(blob, strong_only=True)
    weak = find_tickers(blob, title=title)
    fs = fin_score(blob)
    jp = has_japan(blob) or bool(strong)
    jpstock = bool(strong) or (bool(weak) and fs >= 2)
    return {"tickers": weak, "fin": fs, "jp": jp, "stock": jpstock or fs >= 4, "jpstock": jpstock}


def evaluate_rss(url, days=ACTIVE_DAYS):
    f, err = fetch_feed(url)
    if not f:
        return {"ok": False, "error": err}
    now = datetime.now(timezone.utc)
    posts = []
    for e in f.entries[:30]:
        t = entry_time(e)
        txt = entry_text(e)
        c = classify_post(e.get("title", ""), txt[:20000])
        c["len"] = len(txt)
        posts.append((t, c))
    dated = [p for p in posts if p[0] and p[0] <= now + timedelta(days=1)]
    last = max((p[0] for p in dated), default=None)
    recent = [p for p in dated if p[0] >= now - timedelta(days=days)]
    base = recent if len(recent) >= 3 else posts[:10]
    n = max(len(base), 1)
    return {
        "ok": True,
        "last_post": last.date().isoformat() if last else None,
        "n_recent": len(recent),
        "stock_ratio": round(sum(1 for _, c in base if c["stock"]) / n, 2),
        "jpstock_ratio": round(sum(1 for _, c in base if c["jpstock"]) / n, 2),
        "avg_len": int(sum(c["len"] for _, c in base) / n),
        "title": f.feed.get("title", ""),
    }


def evaluate_tg(handle, days=ACTIVE_DAYS):
    msgs = fetch_tg_channel(handle)
    if not msgs:
        return {"ok": False, "error": "no messages"}
    now = datetime.now(timezone.utc)
    last = max((m[1] for m in msgs if m[1]), default=None)
    cs = [classify_post("", m[2]) for m in msgs]
    n = max(len(cs), 1)
    return {
        "ok": True,
        "last_post": last.date().isoformat() if last else None,
        "n_recent": sum(1 for m in msgs if m[1] and m[1] >= now - timedelta(days=days)),
        "stock_ratio": round(sum(1 for c in cs if c["stock"]) / n, 2),
        "jpstock_ratio": round(sum(1 for c in cs if c["jpstock"]) / n, 2),
        "title": handle,
    }


def evaluate(src):
    if src["type"] == "telegram":
        return evaluate_tg(src["handle"])
    return evaluate_rss(src["url"])


def decide_mode(ev):
    """평가 결과로 게시 모드 결정. None이면 편입 부적합."""
    if not ev.get("ok"):
        return None
    if ev["jpstock_ratio"] >= 0.6:
        return "all"        # 일본 개별주 전문: 전부 게시
    if ev["jpstock_ratio"] >= 0.35:
        return "jpstock"    # 혼합: 일본 종목 신호 있는 글만
    return None
