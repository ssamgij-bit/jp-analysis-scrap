"""note 작성자 품질 점수: 최근 글 6편 본문을 분석."""
import re, json, time, sys
from statistics import mean
from common import S, note_detail, find_tickers, fin_score

AI_PAT = re.compile(r"生成AI|AIによる|AIで(作成|分析|生成|書)|AIが(作成|分析|生成)|ChatGPT|Gemini|Claude|GPT|Perplexity|AI分析|AI予測|AIレポート|AIスコア|AIに(聞|分析|書)", re.I)
HUMAN_PAT = re.compile(r"私は|僕は|僕が|私が|自分は|個人的に|保有(して|中|株)|買い増し|購入しました|売却しました|損切り|ポジション|私見|と考えます|と思います|だと思う|と判断|注目している|気になった|懸念して|説明会に(参加|出席)|社長に|IR担当|質問した|質疑")
PRIMARY_PAT = re.compile(r"決算短信|有価証券報告書|有報|決算説明資料|説明会|中期経営計画|適時開示|IR資料|四半期報告|統合報告書|質疑応答")
TEMPLATE_PAT = re.compile(r"^#{1,3}\s|^\d+\.\s|【結論】|まとめ|投資判断：|総合評価|スコア", re.M)


GEMINI_SPACING = re.compile(r"[^\s] [。、]")  # Deep Research 복사 흔적("です 。")
EXPLICIT_AI = re.compile(r"生成AIで(作成|執筆|書)|AIで(作成|執筆|生成)|AIが(作成|執筆|生成)|AIによる(分析|決算評価|予測|レポート)|AI分析|AIレポート|AIリサーチ|"
                         r"ChatGPTで|Geminiで|Claudeで|Deep ?Research")


def ai_written(title, body):
    """글 단위 AI 작성 판정(주제로 AI를 다루는 글은 제외하도록 제목+도입부만 봄)"""
    head = (title or "") + "\n" + (body or "")[:600]
    if EXPLICIT_AI.search(head):
        return True
    return len(GEMINI_SPACING.findall((body or "")[:5000])) >= 5


def author_posts(u, n=6):
    try:
        d = S.get(f"https://note.com/api/v2/creators/{u}/contents?kind=note&page=1", timeout=20).json()["data"]["contents"]
    except Exception:
        return []
    out = []
    for c in d[:n]:
        det = note_detail(c["key"]); time.sleep(0.4)
        if det:
            out.append({"title": c["name"], "len": det["len"], "body": det["body"], "price": det["price"],
                        "likes": c.get("likeCount", 0), "at": c.get("publishAt")})
    return out


def score_author(posts):
    if not posts:
        return None
    ai = sum(1 for p in posts if ai_written(p["title"], p["body"]))
    human = mean(len(HUMAN_PAT.findall(p["body"])) / max(p["len"], 1) * 1000 for p in posts)  # 1,000자당
    primary = mean(min(len(PRIMARY_PAT.findall(p["body"])), 10) for p in posts)
    tick = sum(1 for p in posts if find_tickers(p["title"] + p["body"], title=p["title"])) / len(posts)
    L = mean(min(p["len"], 15000) for p in posts)
    likes = mean(p["likes"] or 0 for p in posts)
    titles = [re.sub(r"[0-9０-９A-Za-z（）()【】\s]", "", p["title"])[:8] for p in posts]
    template = 1 - len(set(titles)) / len(titles)  # 제목 앞부분이 같은 비율(연재·양산형)
    paid_short = sum(1 for p in posts if p["price"] and p["len"] < 1500) / len(posts)
    return {"ai_posts": ai, "human_per_1k": round(human, 2), "primary": round(primary, 1), "tick_ratio": round(tick, 2),
            "avg_len": int(L), "likes": round(likes, 1), "template": round(template, 2), "paid_short": round(paid_short, 2),
            "n": len(posts)}


def quality(m):
    if not m:
        return -99
    s = 0
    s += min(m["human_per_1k"], 3) * 2.0          # 본인 판단·경험 서술
    s += min(m["primary"], 6) * 0.5               # 1차 자료 인용
    s += m["tick_ratio"] * 2                      # 개별주 중심
    s += min(m["avg_len"], 10000) / 2500          # 분량
    s += min(m["likes"], 60) / 20                 # 독자 반응
    s -= m["ai_posts"] / m["n"] * 5               # AI 생성·분석 명시
    s -= m["template"] * 2                        # 양산형 템플릿
    s -= m["paid_short"] * 2                      # 유료 단문
    return round(s, 2)


if __name__ == "__main__":
    users = sys.argv[1:]
    res = {}
    for u in users:
        ps = author_posts(u)
        m = score_author(ps)
        res[u] = {"metrics": m, "score": quality(m), "titles": [p["title"][:70] for p in ps],
                  "samples": [p["body"][:600] for p in ps[:2]]}
        print(u, res[u]["score"], m, flush=True)
    json.dump(res, open("note_quality.json", "w"), ensure_ascii=False, indent=1)


# ---------------- 작성자 프로필·게시 빈도·유료 비중 ----------------
def author_profile(u):
    """(팔로워 수, 최근 30일 글 수, 마지막 글 날짜, 최근 10편 중 유료 비율)"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    try:
        fol = S.get(f"https://note.com/api/v2/creators/{u}", timeout=20).json()["data"].get("followerCount") or 0
    except Exception:
        return None
    items = []
    for page in range(1, 6):
        try:
            c = S.get(f"https://note.com/api/v2/creators/{u}/contents?kind=note&page={page}", timeout=20).json()["data"]
        except Exception:
            break
        cs = c.get("contents", [])
        if not cs:
            break
        for x in cs:
            items.append((datetime.fromisoformat(x["publishAt"]), (x.get("price") or 0) > 0 or bool(x.get("isLimited"))))
        if items[-1][0] < now - timedelta(days=30) or c.get("isLastPage"):
            break
        time.sleep(0.3)
    if not items:
        return None
    last10 = sorted(items, reverse=True)[:10]
    return {"followers": fol, "n30": sum(1 for t, _ in items if t >= now - timedelta(days=30)),
            "last": max(t for t, _ in items), "paid": sum(1 for _, p in last10 if p) / len(last10)}


def note_author_ok(u, min_quality=6.0):
    """사용자 기준: 유료 위주 아님, AI 작성 1편 이하, 개별주 비중 50% 이상, 품질 점수 기준 이상"""
    from datetime import datetime, timezone
    p = author_profile(u)
    if not p or p["paid"] >= 0.5 or (datetime.now(timezone.utc) - p["last"]).days > 60:
        return False, p
    m = score_author(author_posts(u))
    if not m or m["ai_posts"] > 1 or m["tick_ratio"] < 0.5 or quality(m) < min_quality:
        return False, p
    return True, p
