"""
섹터 로테이션 브리핑 수집기
- 미국(SPDR 11섹터) + 한국(KODEX/TIGER 섹터 ETF) + 거시지표 수집
- 20SMA 위치 / 기울기 / 상태(Strong·OK·Watch·Avoid) 계산
- Supabase 저장 + 텔레그램 발송
하루 2회(한국장 마감 후 / 미국장 마감 후) GitHub Actions로 실행
"""

import os
import time
import datetime as dt
from zoneinfo import ZoneInfo

import yfinance as yf
import requests

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

# 한국: KODEX/TIGER 섹터 ETF (.KS)  — 코드가 안 맞으면 자동 스킵되고 로그 남음
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
# 저장 & 전송
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
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("[INFO] 텔레그램 환경변수 없음 — 전송 건너뜀")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=20,
        )
        if resp.ok:
            print("[OK] 텔레그램 전송 완료")
        else:
            print(f"[WARN] 텔레그램 전송 실패: {resp.text}")
    except Exception as e:
        print(f"[WARN] 텔레그램 전송 예외: {e}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    now = dt.datetime.now(KST)
    captured_at = now.isoformat()
    wd = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    header_date = now.strftime("%m/%d") + f"({wd})"

    us = collect("US", US_SECTORS, "sector")
    kr = collect("KR", KR_SECTORS, "sector")
    mc = collect("MACRO", MACRO, "macro")

    us_sum = summarize("US", us)
    kr_sum = summarize("KR", kr)

    snapshots = us + kr + mc
    briefings = [b for b in [us_sum, kr_sum] if b]

    save_supabase(snapshots, briefings, captured_at)

    blocks = [
        ("🇺🇸 US SECTORS", us, us_sum),
        ("🇰🇷 KR SECTORS", kr, kr_sum),
        ("🌐 MACRO", mc, None),
    ]
    text = build_message(header_date, blocks)
    print("\n" + text)
    send_telegram(text)


if __name__ == "__main__":
    main()
