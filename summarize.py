"""글 전체 요약·결론 생성.
1순위: Google Gemini 무료 API(GitHub Secret GEMINI_API_KEY, 비용 없음)
2순위(한도 초과·오류 시): 규칙 기반 추출 요약(핵심 문장 + 결론부) → 무료 번역"""
import json
import os
import re
import time

from common import S, translate, fin_score, find_tickers

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = [m for m in os.environ.get("SUMMARY_MODEL", "gemini-flash-latest,gemini-flash-lite-latest").split(",") if m]
MAX_INPUT_CHARS = 12000
DAILY_LIMIT = 900       # Gemini 무료 한도(하루 1,000~1,500회) 안쪽

PROMPT = """너는 한국 자산운용사 애널리스트를 돕는 리서치 보조다. 아래 일본 주식 분석글을 읽고 한국어로 정리해라.
규칙:
- 글에 있는 내용만 쓴다. 숫자는 원문 그대로(엔, %, 배 등 단위 유지). 추측·보탬 금지.
- summary: 글 전체의 핵심 논지를 3개 항목으로. 각 항목 한 문장, 60자 이내. 실적 수치·밸류에이션·사업 구조·리스크 중 글이 강조한 것 위주.
- conclusion: 글쓴이의 최종 판단(매수/보유/관망/매도 의견, 목표·적정 가치, 주목 포인트, 핵심 리스크)을 한두 문장으로. 글에 명확한 결론이 없으면 "명시적 결론 없음"이라고 쓰고 글이 향하는 방향만 짧게.
- title_ko: 원제목의 자연스러운 한국어 번역.
- 모든 값은 반드시 한국어로 쓴다(일본어·영어 문장 금지, 고유명사는 한국어 표기 후 필요하면 괄호로 원문).
JSON으로만 답해라: {"title_ko": "...", "summary": ["...", "...", "..."], "conclusion": "..."}"""
KANA = re.compile("[" + chr(0x3040) + "-" + chr(0x30FF) + "]")


def _ko(x):
    """모델이 일본어로 답한 경우 무료 번역으로 보정"""
    x = str(x).strip()
    return (translate(x) or x) if len(KANA.findall(x)) >= 3 else x


def _llm(title, text):
    """Google Gemini 무료 API(키: GitHub Secret GEMINI_API_KEY). 키가 없거나 실패하면 None"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + "\n\n제목: " + title + "\n\n본문:\n" + text[:MAX_INPUT_CHARS]}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024, "responseMimeType": "application/json"},
    }
    for model in MODELS:
        try:
            r = S.post(GEMINI_URL.format(model=model.strip()), params={"key": key}, json=body, timeout=60)
            if r.status_code in (429, 503):  # 일시 과부하 → 한 번 더
                time.sleep(8)
                r = S.post(GEMINI_URL.format(model=model.strip()), params={"key": key}, json=body, timeout=60)
            if r.status_code != 200:
                print("llm error", model, r.status_code, r.text[:200])
                continue
            parts = r.json()["candidates"][0]["content"]["parts"]
            c = "".join(p.get("text", "") for p in parts).strip()
            fence = chr(96) * 3  # 코드블록 표시 제거
            c = re.sub("^" + fence + "(?:json)?|" + fence + "$", "", c).strip()
            m = re.search(r"\{.*\}", c, re.S)
            d = json.loads(m.group(0) if m else c)
            if not d.get("summary"):
                continue
            d["summary"] = [_ko(x) for x in d["summary"] if x and str(x).strip()][:3]
            d["conclusion"] = _ko(d.get("conclusion", ""))
            d["title_ko"] = _ko(d.get("title_ko", "")) or title
            d["via"] = "llm:" + model
            return d
        except Exception as e:
            print("llm exception", model, e)
    return None


# ---------------- 규칙 기반(대체) ----------------
_CONCL_HEAD = re.compile(r"(まとめ|結論|おわりに|終わりに|最後に|総括|投資判断|私の考え|所感|Conclusion|Summary|Bottom line|Takeaway|결론|정리)", re.I)
_JUDGE = re.compile(r"(と考え|と判断|買い|売り|保有|注目|割安|割高|目標株価|適正|リスク|懸念|期待|recommend|target|undervalued|overvalued|risk|매수|매도|목표|리스크)", re.I)


def _sentences(text):
    t = re.sub(r"[ \t]+", " ", text or "")
    parts = re.split(r"(?<=[。．！？!?])\s*|\n+|(?<=[.])\s+(?=[A-Z])", t)
    junk = re.compile(r"https?://|www\.|リンク|링크|^\s*[QA]\d+\s*[:：]|フォロー|スキ|메일|Subscribe", re.I)
    return [p.strip() for p in parts if 15 <= len(p.strip()) <= 220 and not junk.search(p)]


def _extractive(title, text):
    sents = _sentences(text)
    if not sents:
        return None
    # 결론부: 마지막 소제목(まとめ 등) 이후 문장, 없으면 뒤쪽 25%에서 판단 표현이 있는 문장
    concl = []
    m = None
    for mm in _CONCL_HEAD.finditer(text):
        m = mm
    if m and m.start() > len(text) * 0.4:
        concl = [s for s in _sentences(text[m.end():]) if _JUDGE.search(s)][:2] or _sentences(text[m.end():])[:2]
    if not concl:
        tail = sents[int(len(sents) * 0.75):]
        concl = [s for s in tail if _JUDGE.search(s)][-2:]
    # 요약: 본문 전체에서 숫자·재무용어·종목 언급이 많은 문장을 고르게(앞/중/뒤) 선택
    n = len(sents)

    def score(i, s):
        return (len(re.findall(r"\d", s)) > 0) * 2 + fin_score(s) * 1.5 + bool(find_tickers(s)) + (0.5 if i < n * 0.2 else 0)

    picked = []
    for lo, hi in ((0, 0.34), (0.34, 0.67), (0.67, 1.0)):
        seg = [(score(i, s), i, s) for i, s in enumerate(sents) if lo * n <= i < hi * n and s not in concl]
        if seg:
            picked.append(max(seg)[1:])
    picked = [s for _, s in sorted(picked)]
    tr = lambda x: translate(x) or x  # noqa: E731
    return {
        "title_ko": translate(title) or title,
        "summary": [tr(s)[:120] for s in picked[:3]],
        "conclusion": " ".join(tr(s) for s in concl)[:220] if concl else "명시적 결론 없음",
        "via": "rule",
    }


def summarize(title, text, lang, state):
    """반환: {title_ko, summary[list], conclusion, via}"""
    today = state.setdefault("llm", {})
    from datetime import datetime, timezone, timedelta
    d = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    used = today.get(d, 0)
    res = None
    if used < DAILY_LIMIT and len(text or "") >= 300 and os.environ.get("GEMINI_API_KEY"):
        res = _llm(title, text)
        today.clear()
        today[d] = used + 1
    if not res:
        if lang == "ko":
            sents = _sentences(text)
            res = {"title_ko": title, "summary": sents[:3], "conclusion": "", "via": "rule"}
        else:
            res = _extractive(title, text) or {"title_ko": translate(title) or title, "summary": [], "conclusion": "", "via": "none"}
    return res
