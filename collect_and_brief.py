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
        if os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                name = font_manager.FontProperties(fname=path).get_name()
                plt.rcParams["font.family"] = name
                print(f"[INFO] 폰트 사용: {name}")
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False


def _cell(ax, x0, x1, yc, label, sc):
    h = 0.66
    box = FancyBboxPatch(
        (x0, yc - h / 2), x1 - x0, h,
        boxstyle="round,pad=0.0,rounding_size=0.12",
        linewidth=0, facecolor=sc["bg"],
    )
    ax.add_patch(box)
    ax.text((x0 + x1) / 2, yc, label, color=sc["fg"],
            fontsize=10, ha="center", va="center", fontweight="bold")


def render_table_image(title, subtitle, rows, rotation=None):
    rows = sorted(rows, key=lambda x: (STATUS_ORDER[x["status"]], -x["change_pct"]))
    n = len(rows)
    footer_units = 1.9 if rotation else 1.0
    H = n * 0.9 + 3.2 + footer_units
    fig, ax = plt.subplots(figsize=(10.5, H * 0.5), dpi=140)
    fig.patch.set_facecolor(IMG_BG)
    ax.set_facecolor(IMG_BG)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.add_patch(FancyBboxPatch(
        (0.1, 0.1), 9.8, H - 0.2,
        boxstyle="round,pad=0.0,rounding_size=0.25",
        linewidth=1, edgecolor=IMG_LINE, facecolor=IMG_PANEL,
    ))

    y = H - 0.6
    ax.text(0.4, y, title, color=IMG_TEXT, fontsize=15, fontweight="bold", va="center")
    ax.text(9.6, y, subtitle, color=IMG_SUB, fontsize=10, ha="right", va="center")

    y -= 0.95
    ax.text(0.4, y, "지표", color=IMG_SUB, fontsize=9, va="center")
    ax.text(4.35, y, "가격", color=IMG_SUB, fontsize=9, ha="right", va="center")
    ax.text(5.6, y, "20SMA", color=IMG_SUB, fontsize=9, ha="center", va="center")
    ax.text(7.35, y, "Slope", color=IMG_SUB, fontsize=9, ha="center", va="center")
    ax.text(8.92, y, "Status", color=IMG_SUB, fontsize=9, ha="center", va="center")

    y -= 0.35
    for r in rows:
        y -= 0.9
        sc = SCOL[r["status"]]
        ax.text(0.4, y, r["name"], color=IMG_TEXT, fontsize=11, fontweight="bold", va="center")
        ax.text(4.35, y + 0.16, fmt_price(r["price"]), color=IMG_SUB, fontsize=8.5, ha="right", va="center")
        ax.text(4.35, y - 0.17, f"{r['change_pct']:+.2f}%",
                color=(IMG_POS if r["change_pct"] >= 0 else IMG_NEG),
                fontsize=8.5, ha="right", va="center")
        _cell(ax, 4.7, 6.45, y, "Above" if r["above_sma"] else "Below", sc)
        _cell(ax, 6.5, 8.2, y, "Up" if r["slope_up"] else "Down", sc)
        _cell(ax, 8.25, 9.6, y, r["status"], sc)

    above = sum(r["above_sma"] for r in rows)
    tot = len(rows)
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUS_ORDER}

    y -= 1.0
    if rotation:
        rc = ROT_BG.get(rotation, "#2a2a2a")
        rt = ROT_FG.get(rotation, "#dddddd")
        ax.add_patch(FancyBboxPatch(
            (0.4, y - 0.05), 9.2, 0.7,
            boxstyle="round,pad=0.0,rounding_size=0.12",
            linewidth=0, facecolor=rc,
        ))
        ax.text(0.7, y + 0.3, "Rotation", color=rt, fontsize=10, fontweight="bold", va="center")
        ax.text(9.3, y + 0.3, f"{rotation}  ·  20SMA 위 {above}/{tot}",
                color=rt, fontsize=10, ha="right", va="center")
        y -= 0.75

    ax.text(0.4, y,
            f"Strong {counts['Strong']}    OK {counts['OK']}    "
            f"Watch {counts['Watch']}    Avoid {counts['Avoid']}",
            color=IMG_SUB, fontsize=9, va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=IMG_BG, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ─────────────────────────────────────────────
# 텔레그램 전송
# ─────────────────────────────────────────────
def send_telegram_text(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("[INFO] 텔레그램 환경변수 없음 — 텍스트 전송 건너뜀")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=20,
        )
        print("[OK] 텔레그램 텍스트 전송 완료" if resp.ok else f"[WARN] 텍스트 실패: {resp.text}")
    except Exception as e:
        print(f"[WARN] 텍스트 전송 예외: {e}")


def send_telegram_photos(images):
    """images: [(png_bytes, caption_or_None), ...]  2장 이상이면 mediaGroup."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("[INFO] 텔레그램 환경변수 없음 — 이미지 전송 건너뜀")
        return
    images = [im for im in images if im and im[0]]
    if not images:
        return
    try:
        if len(images) == 1:
            png, cap = images[0]
            data = {"chat_id": chat_id}
            if cap:
                data["caption"] = cap
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data=data, files={"photo": ("brief.png", png, "image/png")}, timeout=60,
            )
        else:
            files, media = {}, []
            for i, (png, cap) in enumerate(images):
                key = f"photo{i}"
                files[key] = (f"{key}.png", png, "image/png")
                item = {"type": "photo", "media": f"attach://{key}"}
                if cap:
                    item["caption"] = cap
                media.append(item)
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media)}, files=files, timeout=90,
            )
        print("[OK] 텔레그램 이미지 전송 완료" if resp.ok else f"[WARN] 이미지 실패: {resp.text}")
    except Exception as e:
        print(f"[WARN] 이미지 전송 예외: {e}")


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

    # 텍스트
    blocks = [
        ("🇺🇸 US SECTORS", us, us_sum),
        ("🇰🇷 KR SECTORS", kr, kr_sum),
        ("🌐 MACRO", mc, None),
    ]
    text = build_message(header_date, blocks)
    print("\n" + text)
    send_telegram_text(text)

    # 이미지
    setup_font()
    images = []
    if us:
        images.append((render_table_image("US SECTORS", f"{header_date} 마감", us,
                                           rotation=(us_sum or {}).get("rotation")),
                       f"섹터 로테이션 · {header_date} 마감"))
    if kr:
        images.append((render_table_image("KR SECTORS", f"{header_date} 마감", kr,
                                           rotation=(kr_sum or {}).get("rotation")), None))
    if mc:
        images.append((render_table_image("MACRO", f"{header_date} 마감", mc, rotation=None), None))
    send_telegram_photos(images)


if __name__ == "__main__":
    main()
