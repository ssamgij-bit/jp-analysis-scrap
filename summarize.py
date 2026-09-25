"""글 전체 요약·결론 생성.
1순위: GitHub Models(무료, Actions의 GITHUB_TOKEN 사용, 키·비용 없음)
2순위(한도 초과·오류 시): 규칙 기반 추출 요약(핵심 문장 + 결론부) → 무료 번역"""
import json
import os
import re

from common import S, translate, fin_score, find_tickers

ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODEL = os.environ.get("SUMMARY_MODEL", "openai/gpt-4o-mini")
MAX_INPUT_CHARS = 6000  # 무료 한도(요청당 입력 8,000토큰) 안쪽
DAILY_LIMIT = 140       # 무료 한도(하루 150회) 안쪽

PROMPT = """너는 한국 자산운용사 애널리스트를 돕는 리서치 보조다. 아래 일본 주식 분석글을 읽고 한국어로 정리해라.
규칙:
- 글에 있는 내용만 쓴다. 숫자는 원문 그대로(엔, %, 배 등 단위 유지). 추측·보탬 금지.
- summary: 글 전체의 핵심 논지를 3개 항목으로. 각 항목 한 문장, 60자 이내. 실적 수치·밸류에이션·사업 구조·리스크 중 글이 강조한 것 위주.
- conclusion: 글쓴이의 최종 판단(매수/보유/관망/매도 의견, 목표·적정 가치, 주목 포인트, 핵심 리스크)을 한두 문장으로. 글에 명확한 결론이 없으면 "명시적 결론 없음"이라고 쓰고 글이 향하는 방향만 짧게.
- title_ko: 원제목의 자연스러운 한국어 번역.
JSON으로만 답해라: {"title_ko": "...", "summary": ["...", "...", "..."], "conclusion": "..."}"""


def _llm(title, text):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"제목: {title}\n\n본문:\n{text[:MAX_INPUT_CHARS]}"},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    try:
        r = S.post(ENDPOINT, json=body, timeout=60,
                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if r.status_code != 200:
            print("llm error", r.status_code, r.text[:200])
            return None
        c = r.json()["choices"][0]["message"]["content"]
        fence = chr(96) * 3  # 코드블록 표시 제거
        d = json.loads(re.sub("^" + fence + "(?:json)?|" + fence + "$", "", c.strip()))
        if not d.get("summary"):
            return None
        d["summary"] = [s.strip() for s in d["summary"] if s and s.strip()][:3]
        d["via"] = "llm"
        return d
    except Exception as e:
        print("llm exception", e)
        return None


# ---------------- 규칙 기반(대체) ----------------
_CONCL_HEAD = re.compile(r"(まとめ|結論|おわりに|終わりに|最後に|総括|投資判断|私の考え|所感|Conclusion|Summary|Bottom line|Takeaway|결론|정리)", re.I)
_JUDGE = re.compile(r"(と考え|と判断|買い|売り|保有|注目|割安|割高|目標株価|適正|リスク|懸念|期待|recommend|target|undervalued|overvalued|risk|매수|매도|목표|리스크)", re.I)


def _sentences(text):
    t = re.sub(r"[ \t]+", " ", text or "")
    parts = re.split(r"(?<=[。．！？!?])\s*|\n+|(?<=[.])\s+(?=[A-Z])", t)
    return [p.strip() for p in parts if 15 <= len(p.strip()) <= 220]


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
    if used < DAILY_LIMIT and len(text or "") >= 300:
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
