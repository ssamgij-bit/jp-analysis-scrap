"""공통 유틸: 피드 수집, 종목코드 탐지, 품질 판정, 번역, 텔레그램 전송."""
import html
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import feedparser
import requests
import yaml

UA = "Mozilla/5.0 (compatible; jp-analysis-scrap/1.0)"
S = requests.Session()
S.headers.update({"User-Agent": UA})
KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------------- 파일 입출력 ----------------
def load_yaml(name, default=None):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def save_yaml(name, data):
    p = os.path.join(ROOT, name)
    header = ""
    if os.path.exists(p):  # 파일 맨 위 주석 유지
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                header += line
    with open(p, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=200)


def load_json(name, default):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, data):
    with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------------- 텍스트 ----------------
def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def detect_lang(s):
    s = s or ""
    if re.search(r"[가-힣]", s):
        return "ko"
    if re.search(r"[぀-ヿ]", s):
        return "ja"
    if re.search(r"[一-鿿]", s) and not re.search(r"[A-Za-z]{4,}", s[:200]):
        return "ja"
    return "en"


# ---------------- 일본 종목코드 탐지 ----------------
_CODE = r"([1-9][0-9]{2}[0-9A-Z])"
_PATTERNS = [
    re.compile(r"(?:TYO|TSE|JPX|OTCMKTS?|東証[^\d\s]{0,6})\s*[:：]?\s*" + _CODE + r"(?![0-9])"),
    re.compile(r"(?<![0-9A-Za-z])" + _CODE + r"(?:\s?(?:JP|JT)\b|\.T\b|\.JP\b|\s?\.?\s?Tokyo)"),
    re.compile(r"(?:証券コード|銘柄コード|コード|code|Code|종목코드)\s*[:：]?\s*" + _CODE + r"(?![0-9])"),
    re.compile(r"[【\[［]\s*" + _CODE + r"\s*[】\]］]"),
    re.compile(r"[（(]\s*" + _CODE + r"\s*[）)]"),
    re.compile(r"(?<![0-9A-Za-z/])" + _CODE + r"\s*[/／]\s*(?=[^\d\s])"),
]
_YEAR = re.compile(r"^(19[5-9][0-9]|20[0-4][0-9])$")


_CODES = None
_NAMES = None


def codes():
    global _CODES, _NAMES
    if _CODES is None:
        _CODES = load_json("codes.json", {})
        # 종목명 매칭용(4자 이상, 일반명사 오탐 방지)
        _NAMES = sorted(((n, c) for c, n in _CODES.items() if len(n) >= 4), key=lambda x: -len(x[0]))
    return _CODES


def find_tickers(text, title=None, strong_only=False):
    """일본 종목코드 추출. codes.json이 있으면 실제 상장 코드만 인정.
    title이 주어지면 제목 속 일본어 종목명도 코드로 변환.
    strong_only=True면 TYO/JP/.T/証券コード 등 문맥이 명확한 표기만 인정."""
    cs = codes()
    found = []
    pats = _PATTERNS[:3] if strong_only else _PATTERNS
    for i, p in enumerate(pats):
        for m in p.finditer(text or ""):
            c = m.group(1)
            # 괄호/대괄호 단독 표기는 연도(2026 등)와 혼동되므로 제외
            if i >= 3 and _YEAR.match(c):
                continue
            if cs and c not in cs:
                continue
            if c not in found:
                found.append(c)
    if title and _NAMES and not strong_only:
        for n, c in _NAMES:
            if n in title and c not in found:
                found.append(c)
                if len(found) >= 5:
                    break
    return found[:5]


def ticker_label(c):
    n = codes().get(c)
    return f"{n}({c})" if n else c


FIN_WORDS = [
    # ja
    "決算", "営業利益", "経常利益", "純利益", "売上高", "粗利", "PER", "PBR", "ROE", "ROIC", "EV/EBIT",
    "配当", "自社株買", "中期経営計画", "中計", "業績予想", "上方修正", "下方修正", "受注", "セグメント",
    "バリュエーション", "時価総額", "有報", "有価証券報告書", "決算短信", "ガイダンス", "TOB", "MBO",
    # en
    "earnings", "revenue", "operating profit", "operating income", "EBIT", "EBITDA", "P/E", "valuation",
    "guidance", "buyback", "market cap", "free cash flow", "FCF", "margin", "net cash", "dividend",
    "tender offer", "activist", "mid-term plan", "segment",
    # ko
    "실적", "영업이익", "매출", "밸류에이션", "시가총액", "배당", "가이던스", "자사주",
]
JP_WORDS = ["Japan", "Japanese", "Tokyo", "TSE", "TOPIX", "Nikkei", "日本", "東証", "일본", "도쿄"]

BAN_WORDS = [
    "就活", "ES添削", "模擬面接", "志望動機", "新卒", "生成AIで作成", "AIで作成", "AIが作成", "AI生成",
    "初心者向け", "入門", "ポイ活", "懸賞", "株主優待到着", "優待到着", "優待が届", "運用成績", "今月の資産",
    "資産推移", "月次報告", "家計簿", "Excel", "エクセル", "ツール", "テンプレート", "スコアリング解説",
]


def fin_score(text):
    t = text or ""
    tl = t.lower()
    return sum(1 for w in FIN_WORDS if (w.lower() in tl))


def has_japan(text):
    return any(w in (text or "") for w in JP_WORDS)


def banned(text):
    return [w for w in BAN_WORDS if w in (text or "")]


# ---------------- 피드 ----------------
def fetch_feed(url, timeout=25):
    try:
        r = S.get(url, timeout=timeout)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        f = feedparser.parse(r.content)
        if not f.entries and f.bozo:
            return None, "parse error"
        return f, None
    except Exception as e:  # noqa
        return None, str(e)[:80]


def entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        v = e.get(k)
        if v:
            return datetime(*v[:6], tzinfo=timezone.utc)
    return None


def entry_text(e):
    if e.get("content"):
        return strip_html(" ".join(c.get("value", "") for c in e.content))
    return strip_html(e.get("summary", ""))


def substack_locked(e):
    raw = " ".join(c.get("value", "") for c in e.get("content", [])) if e.get("content") else e.get("summary", "")
    return bool(re.search(r"paywall|subscribe to (read|continue|keep reading)|This post is for paid subscribers", raw, re.I))


# ---------------- note ----------------
def note_detail(key):
    """note 글 상세(본문 길이·가격). key 예: na9a1a7fdd693"""
    try:
        r = S.get(f"https://note.com/api/v3/notes/{key}", timeout=20)
        if r.status_code != 200:
            return None
        d = r.json().get("data", {})
        body = strip_html(d.get("body") or "")
        return {
            "body": body,
            "len": len(body),
            "price": d.get("price") or 0,
            "likes": d.get("like_count") or 0,
            "user": (d.get("user") or {}).get("urlname"),
            "nick": (d.get("user") or {}).get("nickname"),
        }
    except Exception:
        return None


def note_key(url):
    m = re.search(r"/n/(n[0-9a-f]+)", url or "")
    return m.group(1) if m else None


# ---------------- 텔레그램 웹 미리보기 ----------------
def fetch_tg_channel(handle):
    """공개 채널 t.me/s/<handle> 최근 글 목록 [(id, time, text, link)]"""
    out = []
    try:
        r = S.get(f"https://t.me/s/{handle}", timeout=25)
        if r.status_code != 200:
            return out
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for m in soup.select("div.tgme_widget_message"):
            pid = m.get("data-post")
            t = m.select_one("time")
            txt = m.select_one("div.tgme_widget_message_text")
            if not pid or not txt:
                continue
            ts = None
            if t and t.get("datetime"):
                ts = datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
            out.append((pid, ts, txt.get_text("\n").strip(), f"https://t.me/{pid}"))
    except Exception:
        pass
    return out


# ---------------- 번역 (무료, 키 없음) ----------------
_SPLIT1 = re.compile(r"([｜|│。！？!?…\n―—]+|[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2049\u203C]+)")
_SPLIT2 = re.compile(r"([、，,（）()【】「」『』［］\[\]–]+)")


def _gtrans_batch(parts, tl):
    """여러 조각을 한 번에 번역. 실패 시 None"""
    try:
        r = S.post(f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl={tl}",
                   data={"q": parts}, timeout=20)
        if r.status_code == 200:
            d = r.json()
            out = []
            for x in d:
                out.append(x[0] if isinstance(x, list) else x)
            if len(out) == len(parts):
                return out
    except Exception:
        pass
    out = []
    for p in parts:  # gtx 개별 호출
        try:
            r = S.get("https://translate.googleapis.com/translate_a/single",
                      params={"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": p}, timeout=15)
            out.append("".join(seg[0] for seg in r.json()[0] if seg and seg[0]))
        except Exception:
            return None
    return out


def _translate_pieces(text, tl, splitter):
    toks = [t for t in splitter.split(text) if t is not None]
    idx = [i for i, t in enumerate(toks) if t.strip() and not splitter.fullmatch(t)]
    if not idx:
        return text
    res = _gtrans_batch([toks[i].strip() for i in idx], tl)
    if res is None:
        return None
    for i, t in zip(idx, res):
        toks[i] = t
    return "".join(toks)


def translate(text, tl="ko"):
    try:
        return _translate(text, tl)
    except Exception as e:  # 번역 실패해도 게시는 계속
        print("translate error", e)
        return ""


def _translate(text, tl="ko"):
    text = (text or "").strip()[:1500]
    if not text:
        return ""
    # 구글 엔드포인트가 긴 문장을 중간에서 잘라 번역하는 문제 → 구두점·이모지 단위로 쪼개서 번역
    out = _translate_pieces(text, tl, _SPLIT1)
    if out is not None:
        src_len = len(re.sub(r"\s", "", text))
        if len(re.sub(r"\s", "", out)) < src_len * 0.45:  # 여전히 잘렸으면 더 잘게
            finer = _translate_pieces(text, tl, re.compile(_SPLIT1.pattern + "|" + _SPLIT2.pattern))
            if finer:
                out = finer
        return out.replace("。", ". ").replace("、", ", ").strip()
    # MyMemory (일 5,000자 무료) 최후 수단
    try:
        src = detect_lang(text)
        r = S.get("https://api.mymemory.translated.net/get", params={"q": text[:500], "langpair": f"{src}|{tl}"}, timeout=15)
        t = r.json().get("responseData", {}).get("translatedText")
        if t and "MYMEMORY WARNING" not in t:
            return html.unescape(t)
    except Exception:
        pass
    return ""


_JUNK = re.compile(r"https?://|www\.|Subscribe|subscribe now|購読|広告|スキ|この記事は約|読めます|目次|Share|Leave a comment|"
                   r"Thanks for reading|Disclaimer|免責|フォロー|メンバーシップ|구독", re.I)


def lead_sentences(text, max_chars=260):
    t = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[。．！？])|(?<=[.!?])\s+(?=[A-Z0-9\"'“(])", t)
    out = ""
    for p in parts:
        if len(p) < 8 or _JUNK.search(p):
            continue
        if len(out) + len(p) > max_chars:
            break
        out += p + " "
    return (out or t[:max_chars]).strip()


# ---------------- 텔레그램 ----------------
def tg_send(text, preview=True):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[DRY]", text.replace("\n", " | ")[:300])
        return True
    for _ in range(4):
        r = S.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "false" if preview else "true",
            },
            timeout=20,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        print("telegram error", r.status_code, r.text[:200])
        return False
    return False


def esc(s):
    return html.escape(s or "", quote=False)
