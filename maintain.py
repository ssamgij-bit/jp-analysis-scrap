"""주간 소스 관리 — 매주 일요일 실행.
1) 종목코드 목록 갱신  2) 기존 소스 활동 점검(휴면/재활성)
3) 신규 소스 자동 발굴(Substack 검색·추천, note 발굴 작성자, はてな 그룹)  4) 채널에 주간 리포트"""
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

from common import S, load_yaml, save_yaml, load_json, save_json, tg_send, esc
from note_quality import note_author_ok, author_profile
from evaluate import evaluate, evaluate_rss, decide_mode, ACTIVE_DAYS

SUBSTACK_QUERIES = ["japan stocks", "japanese stocks", "japanese equities", "japan small cap", "japan investing",
                    "japan value investing", "tokyo stock exchange", "japan net net", "japan deep value",
                    "日本株", "日本株 分析"]
HATENA_GROUPS = ["17680117127169959644"]  # 日本株☆個別銘柄専門
MIN_AVG_LEN = 2000
REJECT_RECHECK_DAYS = 90
ENABLE_SUBSTACK_DISCOVERY = False
NOTE_MIN_QUALITY = 6.0  # 초기 선별 때 상위 절반 경계 수준
MAX_NEW_PER_WEEK = 10
MAX_EVAL_PER_WEEK = 80


def today():
    return datetime.now(timezone.utc).date().isoformat()


def days_since(d):
    if not d:
        return 9999
    return (datetime.now(timezone.utc).date() - datetime.fromisoformat(d).date()).days


# ---------------- 후보 수집 ----------------
def substack_candidates(known_subs, active_subs):
    """검색 결과(일본 관련 소개글)를 앞에, 추천 목록을 뒤에 둔 후보 리스트"""
    subs, rec = [], []
    for q in SUBSTACK_QUERIES:
        try:
            d = S.get(f"https://substack.com/api/v1/top/search?query={urllib.parse.quote(q)}", timeout=20).json()
        except Exception:
            continue
        stack = [d]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                sd = o.get("subdomain")
                blob = " ".join(str(o.get(k) or "") for k in ("name", "bio", "hero_text", "description"))
                if sd and re.search(r"japan|japanese|tokyo|日本|東証|nikkei|topix", blob, re.I) and sd not in subs:
                    subs.append(sd)
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
        time.sleep(1.5)
    for sd in active_subs:  # 활성 소스의 추천 목록
        try:
            h = S.get(f"https://{sd}.substack.com/recommendations", timeout=20).text
            rec += re.findall(r"https://([a-z0-9-]+)\.substack\.com", h)
        except Exception:
            pass
        time.sleep(1.5)
    out = []
    for s in subs + rec:
        if s not in known_subs and s != "support" and s not in out:
            out.append(s)
    return out


def hatena_candidates(known_urls):
    out = set()
    for g in HATENA_GROUPS:
        for p in range(1, 4):
            try:
                h = S.get(f"https://hatena.blog/g/{g}/blogs?page={p}", timeout=20).text
            except Exception:
                break
            out.update(re.findall(r"https://[a-z0-9-]+\.(?:hatenablog\.(?:com|jp)|hateblo\.jp|hatenadiary\.(?:com|jp|org))", h))
    return [u for u in out if u + "/feed" not in known_urls]


# ---------------- 메인 ----------------
def main():
    data = load_yaml("sources.yaml", {"sources": []})
    sources = data["sources"]
    rejected = load_yaml("rejected.yaml", {}) or {}
    state = load_json("state.json", {"seen": {}, "init": {}})
    report = {"added": [], "dormant": [], "revived": [], "errors": []}

    # 1) 종목코드
    try:
        from codes import refresh_codes
        refresh_codes()
    except Exception as e:
        report["errors"].append(f"codes: {e}")

    # 2) 기존 소스 점검
    for src in sources:
        if src.get("status") == "blocked" or src.get("mode") == "discover":
            continue
        ev = evaluate(src)
        time.sleep(2)
        if not ev.get("ok"):
            src["fail"] = src.get("fail", 0) + 1
            if src["fail"] >= 3 and src["status"] == "active":
                src["status"] = "dormant"
                report["dormant"].append(f"{src['name']} (피드 오류)")
            continue
        src["fail"] = 0
        src["last_post"] = ev["last_post"]
        src["checked"] = today()
        stale = days_since(ev["last_post"]) > ACTIVE_DAYS
        if src["status"] == "active" and stale:
            src["status"] = "dormant"
            report["dormant"].append(f"{src['name']} (마지막 글 {ev['last_post']})")
        elif src["status"] == "dormant" and not stale:
            src["status"] = "active"
            report["revived"].append(src["name"])
        # note 작성자: 유료 위주로 바뀌면 제외(사용자 기준)
        if src.get("platform") == "note" and src["status"] == "active":
            prof = author_profile(src["url"].split("/")[3])
            if prof and prof["paid"] >= 0.5:
                src["status"] = "blocked"
                src["note"] = f"자동 제외: 월 {prof['n30']}건 / 유료 {int(prof['paid']*100)}%"
                report["dormant"].append(f"{src['name']} (제외: 월 {prof['n30']}건, 유료 {int(prof['paid']*100)}%)")

    known_urls = {s.get("url") for s in sources} | set(rejected.keys())
    known_subs = {re.sub(r"https://([^.]+)\.substack\.com.*", r"\1", u) for u in known_urls if u and "substack.com" in u}
    # 거절 후 90일 지난 후보는 재평가 허용
    for u, d in list(rejected.items()):
        if days_since(d) > REJECT_RECHECK_DAYS:
            rejected.pop(u)
            known_urls.discard(u)

    candidates = []  # (url, platform, lang)
    active_subs = [re.sub(r"https://([^.]+)\.substack\.com.*", r"\1", s["url"]) for s in sources
                   if s.get("platform") == "substack" and s.get("status") == "active" and "substack.com" in s.get("url", "")]
    if ENABLE_SUBSTACK_DISCOVERY:  # 사용자 요청으로 Substack은 수집·발굴 제외(2026-09-25)
        for sd in substack_candidates(known_subs, active_subs):
            candidates.append((f"https://{sd}.substack.com/feed", "substack", None))
    for u in hatena_candidates(known_urls):
        candidates.append((u + "/feed", "hatena", "ja"))

    # note: 발굴 게시가 최근 30일 2회 이상인 작성자 → 정식 소스
    disc = state.get("discovered", {})
    for user, dates in disc.items():
        recent = [d for d in dates if days_since(d[:10]) <= 30]
        url = f"https://note.com/{user}/rss"
        if len(recent) >= 2 and url not in known_urls:
            candidates.append((url, "note", "ja"))

    # 3) 후보 평가·편입
    added = 0
    # note 후보 → はてな → Substack 순으로 평가(최대 MAX_EVAL_PER_WEEK)
    order = {"note": 0, "hatena": 1, "substack": 2}
    # note 후보는 팔로워 많은 순으로 먼저 평가
    fol = {}
    for c in candidates:
        if c[1] == "note":
            try:
                fol[c[0]] = S.get(f"https://note.com/api/v2/creators/{c[0].split('/')[3]}", timeout=20).json()["data"].get("followerCount") or 0
            except Exception:
                fol[c[0]] = 0
    candidates.sort(key=lambda c: (order.get(c[1], 9), -fol.get(c[0], 0)))
    for url, platform, lang in candidates[:MAX_EVAL_PER_WEEK]:
        if added >= MAX_NEW_PER_WEEK:
            break
        ev = evaluate_rss(url)
        time.sleep(2.5)
        if not ev.get("ok") and ("429" in str(ev.get("error")) or "timed out" in str(ev.get("error"))):
            continue  # 일시 오류는 다음 주 재시도
        mode = decide_mode(ev)
        ok = (ev.get("ok") and mode and ev["n_recent"] >= 2 and days_since(ev["last_post"]) <= ACTIVE_DAYS)
        if platform != "note":  # note RSS는 발췌만 있어 길이 기준 제외(발굴 단계에서 이미 검증)
            ok = ok and ev.get("avg_len", 0) >= MIN_AVG_LEN
        if platform == "note":  # note 작성자: 유료 위주 아님·AI 작성 아님·품질 기준(팔로워 많은 순 우선)
            ok = False
            if ev.get("ok"):
                ok, prof = note_author_ok(url.split("/")[3], NOTE_MIN_QUALITY)
                mode = "jpstock"
        if not ok:
            rejected[url] = today()
            continue
        sid = re.sub(r"[^a-z0-9]+", "-", url.split("//")[1].split("/feed")[0].split("/rss")[0].lower()).strip("-")
        sources.append({
            "id": sid, "name": ev.get("title") or sid, "type": "rss", "platform": platform, "url": url,
            "lang": lang, "mode": mode, "status": "active", "origin": "auto", "added": today(),
            "last_post": ev["last_post"],
        })
        report["added"].append(f"{ev.get('title') or sid} [{platform}·{mode}]")
        added += 1

    save_yaml("sources.yaml", data)
    save_yaml("rejected.yaml", rejected)

    # 4) 리포트
    n_active = sum(1 for s in sources if s.get("status") == "active")
    lines = [f"🗂 <b>주간 소스 업데이트</b> ({today()})", f"활성 소스 {n_active}개 · 후보 {min(len(candidates), MAX_EVAL_PER_WEEK)}개 평가"]
    for key, label in (("added", "➕ 추가"), ("revived", "♻️ 재활성"), ("dormant", "💤 휴면 전환"), ("errors", "⚠️ 오류")):
        if report[key]:
            lines.append(f"\n{label} {len(report[key])}")
            lines += [f"• {esc(x)}" for x in report[key][:20]]
    if not any(report[k] for k in ("added", "revived", "dormant")):
        lines.append("\n변경 없음")
    tg_send("\n".join(lines), preview=False)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
