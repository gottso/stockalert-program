"""
섹터 로테이션 브리핑 수집기
- 미국(SPDR 11섹터) + 한국(KODEX/TIGER 섹터 ETF) + 거시지표 수집
- 20SMA 위치 / 기울기 / 상태(Strong·OK·Watch·Avoid) 계산
- Supabase 저장 + 텔레그램 발송(텍스트 + 이미지 표)
하루 2회(한국장 마감 후 / 미국장 마감 후) GitHub Actions로 실행
"""

import os
import io
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo

import yfinance as yf
import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib import font_manager

try:
    from supabase import create_client
except Exception:
    create_client = None

KST = ZoneInfo("Asia/Seoul")

# ─────────────────────────────────────────────
# 수집 대상 설정  (여기만 고치면 종목 추가/교체 가능)
# ─────────────────────────────────────────────
US_SECTORS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLI":  "Industrials",
    "XLY":  "Cons. Disc.",
    "XLP":  "Cons. Staples",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLC":  "Comm. Svcs",
}

# 한국: KODEX/TIGER 섹터 ETF (.KS) — 코드가 안 맞으면 자동 스킵되고 로그 남음
KR_SECTORS = {
    "091160.KS": "반도체",
    "091170.KS": "은행",
    "091180.KS": "자동차",
    "102960.KS": "기계장비",
    "102970.KS": "증권",
    "117460.KS": "에너지화학",
    "117680.KS": "철강",
    "117700.KS": "건설",
    "140700.KS": "보험",
    "140710.KS": "운송",
    "266360.KS": "미디어·엔터",
    "305720.KS": "2차전지",
    "143860.KS": "헬스케어",
}

# 거시지표 (첫 번째 매크로 스냅샷 사진 기준)
MACRO = {
    "DX-Y.NYB": "달러(DXY)",
    "^TNX":     "美 10년물",
    "^VIX":     "VIX",
    "GC=F":     "금",
    "CL=F":     "WTI 유가",
    "HG=F":     "구리",
    "BTC-USD":  "비트코인",
    "KRW=X":    "원/달러",
}

SMA_WINDOW = 20        # 20일 이동평균
SLOPE_LOOKBACK = 5     # 기울기 판정용 lookback (SMA가 5일 전보다 높으면 Up)

STATUS_EMOJI = {"Strong": "🟢", "OK": "🟦", "Watch": "🟧", "Avoid": "🔴"}
STATUS_ORDER = {"Strong": 0, "OK": 1, "Watch": 2, "Avoid": 3}

# 텔레그램 메시지 하단에 붙는 해석 가이드
LEGEND = (
    "━━━━━━━━━━━━━━━━\n"
    "📖 읽는 법\n"
    "· 20SMA  Above=20일선 위(강세궤도) / Below=아래(약세)\n"
    "· Slope  Up=추세 상승전환 / Down=추세 하락\n"
    "· 🟢 Strong  주도섹터 (매수·보유)\n"
    "· 🟦 OK  위지만 힘빠짐 (경계·익절고민)\n"
    "· 🟧 Watch  바닥 반등초입 (관찰대상)\n"
    "· 🔴 Avoid  추세약세 (신규진입 회피)\n"
    "· Rotation  RISK-ON 광범위강세 / SELECTIVE 선별장 / "
    "CAUTION 약화 / RISK-OFF 광범위약세\n"
    "· MACRO는 지표 방향일 뿐 — 달러·금리·VIX 상승은 증시엔 보통 역풍"
)

# 이미지 색상 (대시보드와 동일 팔레트)
IMG_BG = "#0b0b0d"
IMG_PANEL = "#121216"
IMG_LINE = "#1e1e22"
IMG_TEXT = "#e8e8ea"
IMG_SUB = "#8a8a92"
IMG_POS = "#3fb36b"
IMG_NEG = "#e06a6a"
SCOL = {
    "Strong": {"bg": "#1f7a3d", "fg": "#d6ffe3"},
    "OK":     {"bg": "#0f6b6b", "fg": "#d2ffff"},
    "Watch":  {"bg": "#b4740c", "fg": "#ffeccb"},
    "Avoid":  {"bg": "#992222", "fg": "#ffdada"},
}
ROT_BG = {"RISK-ON": "#14331f", "SELECTIVE": "#3a2a08", "CAUTION": "#3a2408", "RISK-OFF": "#331414"}
ROT_FG = {"RISK-ON": "#8ff0b0", "SELECTIVE": "#ffd487", "CAUTION": "#ffbe73", "RISK-OFF": "#ff9a9a"}


# ─────────────────────────────────────────────
# 계산 로직
# ─────────────────────────────────────────────
def classify(above: bool, slope_up: bool) -> str:
    """2×2 매트릭스: 위치(위/아래) × 기울기(상승/하락)"""
    if above and slope_up:
        return "Strong"
    if above and not slope_up:
        return "OK"
    if not above and slope_up:
        return "Watch"
    return "Avoid"


def rotation_label(above_ratio: float) -> str:
    if above_ratio >= 0.75:
        return "RISK-ON"
    if above_ratio >= 0.50:
        return "SELECTIVE"
    if above_ratio >= 0.25:
        return "CAUTION"
    return "RISK-OFF"


def fetch_metrics(ticker: str):
    """단일 티커의 지표 계산. 실패/데이터부족이면 None."""
    try:
        hist = yf.Ticker(ticker).history(period="4mo", interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"  [WARN] {ticker} fetch 실패: {e}")
        return None

    if hist is None or hist.empty:
        print(f"  [WARN] {ticker} 데이터 없음")
        return None

    close = hist["Close"].dropna()
    if len(close) < SMA_WINDOW + SLOPE_LOOKBACK:
        print(f"  [WARN] {ticker} 데이터 부족({len(close)}개)")
        return None

    sma = close.rolling(SMA_WINDOW).mean()

    price = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    sma_now = float(sma.iloc[-1])
    sma_past = float(sma.iloc[-1 - SLOPE_LOOKBACK])

    above = price >= sma_now
    slope_up = sma_now >= sma_past
    change_pct = (price / prev - 1) * 100 if prev else 0.0

    return {
        "ticker": ticker,
        "price": round(price, 4),
        "change_pct": round(change_pct, 2),
        "sma20": round(sma_now, 4),
        "above_sma": above,
        "slope_up": slope_up,
        "status": classify(above, slope_up),
        "session_date": close.index[-1].date().isoformat(),
    }


def collect(market: str, mapping: dict, category: str):
    print(f"\n[{market}] 수집 시작 ({len(mapping)}개)")
    rows = []
    for ticker, name in mapping.items():
        m = fetch_metrics(ticker)
        if not m:
            continue
        m.update({"market": market, "category": category, "name": name})
        rows.append(m)
        print(f"  {name} ({ticker}): {m['status']} {m['change_pct']:+.2f}%")
        time.sleep(0.4)  # Yahoo 레이트리밋 완화
    return rows


def summarize(market: str, rows: list):
    total = len(rows)
    if total == 0:
        return None
    above = sum(r["above_sma"] for r in rows)
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUS_ORDER}
    return {
        "market": market,
        "session_date": rows[0]["session_date"],
        "above_count": above,
        "total_count": total,
        "strong_count": counts["Strong"],
        "ok_count": counts["OK"],
        "watch_count": counts["Watch"],
        "avoid_count": counts["Avoid"],
        "rotation": rotation_label(above / total),
    }


# ─────────────────────────────────────────────
# Supabase 저장
# ─────────────────────────────────────────────
def save_supabase(snapshots, briefings, captured_at):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key and create_client):
        print("[INFO] Supabase 환경변수 없음 — 저장 건너뜀")
        return
    try:
        sb = create_client(url, key)
        snaps = []
        for r in snapshots:
            snaps.append({
                "captured_at": captured_at,
                "session_date": r["session_date"],
                "market": r["market"],
                "category": r["category"],
                "name": r["name"],
                "ticker": r["ticker"],
                "price": r["price"],
                "change_pct": r["change_pct"],
                "sma20": r["sma20"],
                "above_sma": r["above_sma"],
                "slope_up": r["slope_up"],
                "status": r["status"],
            })
        briefs = []
        for b in briefings:
            bb = dict(b)
            bb["captured_at"] = captured_at
            briefs.append(bb)

        if snaps:
            sb.table("sector_snapshots").insert(snaps).execute()
        if briefs:
            sb.table("briefings").insert(briefs).execute()
        print("[OK] Supabase 저장 완료")
    except Exception as e:
        print(f"[WARN] Supabase 저장 실패: {e}")


# ─────────────────────────────────────────────
# 텍스트 메시지
# ─────────────────────────────────────────────
def fmt_price(v):
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def build_message(header_date, blocks):
    lines = [f"📊 섹터 로테이션 · {header_date} 마감", "━━━━━━━━━━━━━━━━"]
    for title, rows, summ in blocks:
        if not rows:
            continue
        lines.append("")
        lines.append(title)
        if summ:
            lines.append(
                f"Rotation: {summ['rotation']}  ·  20SMA 위 {summ['above_count']}/{summ['total_count']}"
            )
            lines.append(
                f"🟢{summ['strong_count']} 🟦{summ['ok_count']} 🟧{summ['watch_count']} 🔴{summ['avoid_count']}"
            )
        ordered = sorted(rows, key=lambda x: (STATUS_ORDER[x["status"]], -x["change_pct"]))
        for r in ordered:
            e = STATUS_EMOJI[r["status"]]
            lines.append(f"{e} {r['name']}  {r['change_pct']:+.2f}%")
    lines.append("")
    lines.append(LEGEND)
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 이미지 표 렌더링
# ─────────────────────────────────────────────
def setup_font():
    """GitHub Actions에서 apt로 설치한 나눔고딕 등록. 없으면 기본 폰트로 폴백."""
    candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
