"""일본 주식 분석글 모음 봇 — 30분마다 실행.
sources.yaml의 active 소스에서 새 글을 가져와 필터 → 번역 → 텔레그램 게시."""
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta

from note_quality import ai_written
from common import (load_yaml, load_json, save_json, fetch_feed, entry_time, entry_text, substack_locked,
                    note_key, note_detail, fetch_tg_channel, find_tickers, ticker_label, fin_score, has_japan,
                    banned, detect_lang, translate, lead_sentences, tg_send, esc, KST)

MAX_AGE = timedelta(days=3)          # 이보다 오래된 글은 게시하지 않음
FIRST_RUN_WINDOW = timedelta(hours=int(os.environ.get("FIRST_RUN_HOURS", "24")))  # 새로 편입된 소스는 최근 24시간 글만 게시
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS", "25"))
MAX_PER_SOURCE_PER_DAY = 0   # 소스당 하루 게시 한도(0 = 제한 없음, 사용자 요청 2026-09-25)
PLATFORM = {"substack": "Substack", "note": "note", "hatena": "はてな", "naver": "네이버", "blog": "Blog",
            "telegram": "Telegram", "note_tag": "note"}
FLAG = {"en": "🇺🇸", "ja": "🇯🇵", "ko": "🇰🇷"}


def get_items(src):
    """소스별 새 글 후보 [{id,title,text,link,time,locked,price,author}]"""
    items = []
    t = src["type"]
    if t == "telegram":
        for pid, ts, txt, link in fetch_tg_channel(src["handle"]):
            first = txt.split("\n")[0][:120]
            items.append({"id": link, "title": first, "text": txt, "link": link, "time": ts})
        return items
    f, err = fetch_feed(src["url"])
    if not f:
        print(f"  ! {src['id']}: {err}")
        return items
    for e in f.entries[:30]:
        link = e.get("link") or e.get("id")
        items.append({
            "id": link,
            "title": (e.get("title") or "").strip(),
            "text": entry_text(e),
            "link": link,
            "time": entry_time(e),
            "locked": src.get("platform") == "substack" and substack_locked(e),
            "author": e.get("note_creatorname") or e.get("author") or "",
        })
    return items


def enrich_note(it):
    k = note_key(it["link"])
    d = note_detail(k) if k else None
    if d:
        it["text"] = d["body"] or it["text"]
        it["price"] = d["price"]
        it["likes"] = d["likes"]
        it["len"] = d["len"]
        it["author"] = d["nick"] or it.get("author")
        it["user"] = d["user"]
    return it


GLOBAL_EXCLUDE = re.compile(
    r"相場振り返り|日本株動向|大引け|前場|後場|市況|朝刊|夕刊|今日動いた|本日の|週間振り返り|週報|運用報告|運用成績|資産推移|"
    r"ポートフォリオ公開|収益公開|アドセンス|優待到着|ビットコイン|イーサリアム|仮想通貨|暗号資産|ドル円|Market Wrap|Daily Wrap|Week in Review|Weekly Recap|시황|마감", re.I)
FOREIGN_TICKER = re.compile(r"[（(]\s*[A-Z]{1,5}(?:\.[A-Z])?\s*[)）]|\$[A-Z]{1,5}\b")


def judge(src, it):
    """게시 여부와 사유. 반환: (bool, tickers)"""
    title, text = it["title"], it["text"]
    blob = f"{title}\n{text}"
    for pat in src.get("exclude_title", []) or []:
        if re.search(pat, title, re.I):
            return False, []
    if GLOBAL_EXCLUDE.search(title):
        return False, []
    tick = find_tickers(blob, title=title)
    title_tick = find_tickers(title, title=title)
    # 제목에 미국식 티커만 있고 일본 종목이 없으면 해외주 글로 판단
    if FOREIGN_TICKER.search(title) and not title_tick:
        return False, []
    fs = fin_score(blob)
    mode = src.get("mode", "all")
    full_text = src.get("platform") not in ("naver",)  # 네이버 RSS는 발췌만 제공
    n = it.get("len") or len(text)

    if mode == "discover":  # note 해시태그: 엄격한 품질 기준
        if banned(blob) or ai_written(title, text) or it.get("price"):  # 유료 글 제외
            return False, tick
        long_enough = n >= 3000 or (it.get("price") and n >= 800)
        return bool(tick) and fs >= 4 and long_enough, tick
    if mode == "all":
        return bool(tick) or fs >= 3 or not full_text, tick
    if mode == "jpstock":
        if not full_text:  # 발췌만 있는 소스(네이버 등): 일본 관련 + 종목 신호
            return (bool(tick) or (has_japan(blob) and fs >= 1)), tick
        return bool(tick) and (fs >= 2 or bool(title_tick)) and n >= 1200, tick
    return False, tick


_name_cache = None


def ko_name(code):
    global _name_cache
    if _name_cache is None:
        _name_cache = load_json("names_ko.json", {})
    lab = ticker_label(code)
    if code in _name_cache:
        return _name_cache[code]
    ja = unicodedata.normalize("NFKC", lab.split("(")[0])
    ko = translate(ja) if ja != code else ""
    val = f"{ko}({code})" if ko and ko != ja else lab
    _name_cache[code] = val
    return val


def build_message(src, it, tick):
    lang = src.get("lang") or detect_lang(it["title"] + it["text"][:300])
    title = it["title"] or "(제목 없음)"
    summary_src = lead_sentences(it["text"], 260)
    if lang != "ko":
        t_title = translate(title) or title
        t_sum = translate(summary_src) if summary_src else ""
    else:
        t_title, t_sum = title, summary_src
    lines = [f"{FLAG.get(lang, '🌐')} <b>{esc(t_title)}</b>"]
    if lang != "ko" and t_title != title:
        lines.append(f"<i>{esc(title)}</i>")
    tags = []
    if tick:
        tags.append("🏷 " + ", ".join(ko_name(c) for c in tick[:4]))
    if it.get("locked"):
        tags.append("🔒유료")
    if it.get("price"):
        tags.append(f"🔒¥{it['price']:,}")
    if src.get("mode") == "discover":
        tags.append("🔎발굴")
    if tags:
        lines.append(" · ".join(tags))
    if t_sum:
        lines.append("")
        lines.append(esc(t_sum[:350]))
    when = it["time"].astimezone(KST).strftime("%m-%d %H:%M") if it.get("time") else ""
    who = src["name"]
    if src.get("mode") == "discover" and it.get("author"):
        who = f"{it['author']} (#{src['name'].split('#')[-1]})"
    lines.append("")
    lines.append(f"— {esc(who)} · {PLATFORM.get(src.get('platform'), '')} · {when}")
    lines.append(it["link"])
    return "\n".join(lines)


def main():
    sources = load_yaml("sources.yaml", {}).get("sources", [])
    state = load_json("state.json", {"seen": {}, "init": {}})
    seen, init = state.setdefault("seen", {}), state.setdefault("init", {})
    now = datetime.now(timezone.utc)
    posted = 0
    for src in sources:
        if src.get("status") != "active":
            continue
        first = src["id"] not in init
        try:
            items = get_items(src)
        except Exception as e:
            print(f"  ! {src['id']} error {e}")
            continue
        new, texts = [], set()
        for it in items:
            if not it["id"] or it["id"] in seen:
                continue
            h = "h:" + str(abs(hash(it["title"] + it["text"][:200])))
            if src["type"] == "telegram":
                h = "t:" + re.sub(r"\W", "", it["text"])[:80]
                if h in seen or h in texts:
                    seen[it["id"]] = now.isoformat()
                    continue
                texts.add(h)
                seen[h] = now.isoformat()
            tkey = "T:" + re.sub(r"\W", "", it["title"].lower())[:60]
            if len(tkey) > 12 and tkey in seen:
                seen[it["id"]] = now.isoformat()
                continue
            if len(tkey) > 12:
                seen[tkey] = now.isoformat()
            new.append(it)
        # 오래된 순으로 게시
        new.sort(key=lambda x: x.get("time") or now)
        for it in new:
            seen[it["id"]] = now.isoformat()
            ts = it.get("time")
            window = FIRST_RUN_WINDOW if first else MAX_AGE
            if ts and (now - ts > window or ts > now + timedelta(days=1)):
                continue
            today_kst = now.astimezone(KST).date().isoformat()
            daily = state.setdefault("daily", {}).setdefault(src["id"], {})
            if posted >= MAX_POSTS_PER_RUN or (MAX_PER_SOURCE_PER_DAY and src.get("mode") != "discover"
                                                and daily.get(today_kst, 0) >= MAX_PER_SOURCE_PER_DAY):
                del seen[it["id"]]  # 다음 실행(다음 날)에서 처리
                continue
            if src.get("platform") in ("note", "note_tag"):
                it = enrich_note(it)
                time.sleep(0.5)
            ok, tick = judge(src, it)
            if not ok:
                continue
            msg = build_message(src, it, tick)
            if tg_send(msg):
                posted += 1
                daily[today_kst] = daily.get(today_kst, 0) + 1
                print(f"  + [{src['id']}] {it['title'][:60]}")
                if src.get("mode") == "discover" and it.get("user"):
                    state.setdefault("discovered", {}).setdefault(it["user"], []).append(now.isoformat())
                if os.environ.get("TELEGRAM_BOT_TOKEN"):
                    time.sleep(3)
        init[src["id"]] = now.isoformat()
        time.sleep(1.0)  # Substack 429 방지
    # seen 정리(120일 초과 삭제)
    cutoff = (now - timedelta(days=120)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["discovered"] = {u: [d for d in ds if d >= cutoff] for u, ds in state.get("discovered", {}).items()}
    keep = (now - timedelta(days=3)).astimezone(KST).date().isoformat()
    state["daily"] = {k: {d: n for d, n in v.items() if d >= keep} for k, v in state.get("daily", {}).items()}
    state["last_run"] = now.isoformat()
    save_json("state.json", state)
    if _name_cache is not None:
        save_json("names_ko.json", _name_cache)
    print(f"posted {posted}")


if __name__ == "__main__":
    main()
