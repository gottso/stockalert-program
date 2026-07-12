"""
섹터 로테이션 브리핑 수집기 (확장판)
- 미국(SPDR 11) + 한국 섹터(21 테마) + 한국 지수(Market Stance) + 거시지표
- 20SMA / Slope / Ext(이격도) / RS20(상대강도) / Stage(국면) 계산
- Market Stance 등급(Grade/Score), 1-Day 막대 랭킹 차트
- Supabase 저장 + 텔레그램 발송(텍스트 + 이미지)
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
# 수집 대상
# ─────────────────────────────────────────────
US_SECTORS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Health Care", "XLI": "Industrials", "XLY": "Cons. Disc.",
    "XLP": "Cons. Staples", "XLB": "Materials", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLC": "Comm. Svcs",
}

# 한국 섹터 21개 테마 (이미지 티커 코드 기준)
KR_SECTORS = {
    "091160.KS": "반도체전체",
    "475300.KS": "반도체전공정",
    "475310.KS": "반도체후공정",
    "305720.KS": "2차전지",
    "244580.KS": "바이오",
    "091180.KS": "자동차",
    "487240.KS": "전력설비",
    "434730.KS": "원전",
    "466920.KS": "조선",
    "014150.KS": "조선기자재",
    "449450.KS": "방산",
    "421320.KS": "우주항공",
    "117680.KS": "철강",
    "117460.KS": "에너지화학",
    "117700.KS": "건설",
    "445290.KS": "로봇",
    "228790.KS": "화장품",
    "266390.KS": "경기소비재",
    "091170.KS": "은행",
    "102970.KS": "증권",
    "140700.KS": "보험",
}

# 한국 지수 (Market Stance) — 야후에서 안 잡히는 건 자동 스킵
KR_STANCE = {
    "^KS11": "KOSPI",
    "^KS200": "KOSPI200",
    "069500.KS": "KODEX200",
    "^KQ11": "KOSDAQ",
    "229200.KS": "KOSDAQ150",
    "232080.KS": "TIGER코스닥150",
}

MACRO = {
    "DX-Y.NYB": "달러(DXY)", "^TNX": "美 10년물", "^VIX": "VIX",
    "GC=F": "금", "CL=F": "WTI 유가", "HG=F": "구리",
    "BTC-USD": "비트코인", "KRW=X": "원/달러",
}

# RS 벤치마크 (시장 대비 상대강도)
BENCH_KR = "^KS11"    # 코스피
BENCH_US = "^GSPC"    # S&P500

SMA_WINDOW = 20
SLOPE_LOOKBACK = 5
RS_WINDOW = 20        # 상대강도 계산 기간
LATE_EXT = 6.0        # 이 이상 이격되면 Late-Chase(과열/추격위험)

STATUS_EMOJI = {"Strong": "🟢", "OK": "🟦", "Watch": "🟧", "Avoid": "🔴"}
STATUS_ORDER = {"Strong": 0, "OK": 1, "Watch": 2, "Avoid": 3}

STAGE_EMOJI = {
    "Leading": "🟢", "Healthy": "🟩", "Late-Chase": "🟧",
    "OK": "🟦", "Improving": "🔷", "Repair": "⬜", "Weak": "🔴",
}
STAGE_ORDER = {
    "Leading": 0, "Late-Chase": 1, "Healthy": 2, "OK": 3,
    "Improving": 4, "Repair": 5, "Weak": 6,
}

LEGEND = (
    "━━━━━━━━━━━━━━━━\n"
    "📖 읽는 법\n"
    "· Ext(이격도)  20SMA서 벌어진 % — 음수 과매도 / +6%↑ 과열(추격위험)\n"
    "· RS20(상대강도)  섹터−시장 20일 수익률 차 — 양수=주도, 음수=소외\n"
    "· Stage  🟢Leading 신규주도 · 🟧Late-Chase 과열주도(추격위험)\n"
    "         🟩Healthy 건강강세 · 🟦OK 위지만둔화 · 🔷Improving 바닥회복\n"
    "         ⬜Repair 회복시도 · 🔴Weak 소외\n"
    "· Grade  시장국면: 공격 / 중립 / 방어 / 회피"
)

# 색상
IMG_BG = "#0b0b0d"; IMG_PANEL = "#121216"; IMG_LINE = "#1e1e22"
IMG_TEXT = "#e8e8ea"; IMG_SUB = "#8a8a92"; IMG_POS = "#3fb36b"; IMG_NEG = "#e06a6a"

SCOL = {
    "Strong": {"bg": "#1f7a3d", "fg": "#d6ffe3"},
    "OK":     {"bg": "#0f6b6b", "fg": "#d2ffff"},
    "Watch":  {"bg": "#b4740c", "fg": "#ffeccb"},
    "Avoid":  {"bg": "#992222", "fg": "#ffdada"},
}
STAGE_COL = {
    "Leading":    {"bg": "#1f7a3d", "fg": "#d6ffe3"},
    "Healthy":    {"bg": "#2e6b45", "fg": "#d6ffe3"},
    "Late-Chase": {"bg": "#b4740c", "fg": "#ffeccb"},
    "OK":         {"bg": "#0f6b6b", "fg": "#d2ffff"},
    "Improving":  {"bg": "#2f5f8a", "fg": "#d6ecff"},
    "Repair":     {"bg": "#4a4a52", "fg": "#e6e6ec"},
    "Weak":       {"bg": "#992222", "fg": "#ffdada"},
}


# ─────────────────────────────────────────────
# 계산
# ─────────────────────────────────────────────
def classify(above, slope_up):
    if above and slope_up: return "Strong"
    if above and not slope_up: return "OK"
    if not above and slope_up: return "Watch"
    return "Avoid"


def classify_stage(above, slope_up, ext, rs):
    if above and slope_up:
        if ext >= LATE_EXT: return "Late-Chase"
        if rs > 0: return "Leading"
        return "Healthy"
    if above and not slope_up:
        return "OK"
    if not above and slope_up:
        return "Improving" if rs > 0 else "Repair"
    return "Weak"


def rotation_label(above_ratio):
    if above_ratio >= 0.75: return "RISK-ON"
    if above_ratio >= 0.50: return "SELECTIVE"
    if above_ratio >= 0.25: return "CAUTION"
    return "RISK-OFF"


def fetch_metrics(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="4mo", interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"  [WARN] {ticker} fetch 실패: {e}")
        return None
    if hist is None or hist.empty:
        print(f"  [WARN] {ticker} 데이터 없음")
        return None
    close = hist["Close"].dropna()
    if len(close) < max(SMA_WINDOW + SLOPE_LOOKBACK, RS_WINDOW + 1):
        print(f"  [WARN] {ticker} 데이터 부족({len(close)}개)")
        return None

    sma = close.rolling(SMA_WINDOW).mean()
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    sma_now = float(sma.iloc[-1])
    sma_past = float(sma.iloc[-1 - SLOPE_LOOKBACK])
    base20 = float(close.iloc[-1 - RS_WINDOW])

    above = price >= sma_now
    slope_up = sma_now >= sma_past
    change_pct = (price / prev - 1) * 100 if prev else 0.0
    ext = (price / sma_now - 1) * 100 if sma_now else 0.0
    ret20 = (price / base20 - 1) * 100 if base20 else 0.0

    return {
        "ticker": ticker,
        "price": round(price, 4),
        "change_pct": round(change_pct, 2),
        "sma20": round(sma_now, 4),
        "above_sma": above,
        "slope_up": slope_up,
        "ext": round(ext, 2),
        "ret20": round(ret20, 2),
        "status": classify(above, slope_up),
        "session_date": close.index[-1].date().isoformat(),
    }


def bench_ret20(ticker):
    m = fetch_metrics(ticker)
    return m["ret20"] if m else 0.0


def collect(market, mapping, category, bench=None):
    print(f"\n[{market}] 수집 시작 ({len(mapping)}개)")
    bret = bench_ret20(bench) if bench else 0.0
    if bench:
        print(f"  벤치마크 {bench} 20일수익률: {bret:+.2f}%")
    rows = []
    for ticker, name in mapping.items():
        m = fetch_metrics(ticker)
        if not m:
            continue
        m.update({"market": market, "category": category, "name": name})
        m["rs20"] = round(m["ret20"] - bret, 1)
        m["stage"] = classify_stage(m["above_sma"], m["slope_up"], m["ext"], m["rs20"])
        rows.append(m)
        print(f"  {name} ({ticker}): {m['stage']} chg{m['change_pct']:+.2f}% "
              f"ext{m['ext']:+.1f}% RS{m['rs20']:+.1f}")
        time.sleep(0.4)
    return rows


def summarize(market, rows):
    total = len(rows)
    if total == 0:
        return None
    above = sum(r["above_sma"] for r in rows)
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUS_ORDER}
    return {
        "market": market, "session_date": rows[0]["session_date"],
        "above_count": above, "total_count": total,
        "strong_count": counts["Strong"], "ok_count": counts["OK"],
        "watch_count": counts["Watch"], "avoid_count": counts["Avoid"],
        "rotation": rotation_label(above / total),
    }


def market_grade(rows):
    n = len(rows)
    if n == 0:
        return {"score": 0.0, "grade": "회피", "sub": "Cash/Avoid", "above": 0, "total": 0}
    above = sum(r["above_sma"] for r in rows)
    up = sum(r["slope_up"] for r in rows)
    score = round(10 * (0.6 * above / n + 0.4 * up / n), 1)
    if score >= 7: g, sub = "공격", "Risk-On"
    elif score >= 5: g, sub = "중립", "Neutral"
    elif score >= 3: g, sub = "방어", "Defensive"
    else: g, sub = "회피", "Cash/Avoid"
    return {"score": score, "grade": g, "sub": sub, "above": above, "total": n}


# ─────────────────────────────────────────────
# Supabase
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
                "captured_at": captured_at, "session_date": r["session_date"],
                "market": r["market"], "category": r["category"], "name": r["name"],
                "ticker": r["ticker"], "price": r["price"], "change_pct": r["change_pct"],
                "sma20": r["sma20"], "above_sma": r["above_sma"], "slope_up": r["slope_up"],
                "ext": r.get("ext"), "rs20": r.get("rs20"),
                "stage": r.get("stage"), "status": r["status"],
            })
        briefs = [dict(b, captured_at=captured_at) for b in briefings]
        if snaps:
            sb.table("sector_snapshots").insert(snaps).execute()
        if briefs:
            sb.table("briefings").insert(briefs).execute()
        print("[OK] Supabase 저장 완료")
    except Exception as e:
        print(f"[WARN] Supabase 저장 실패: {e}")


# ─────────────────────────────────────────────
# 텍스트
# ─────────────────────────────────────────────
def fmt_price(v):
    if v is None: return "-"
    a = abs(v)
    if a >= 1000: return f"{v:,.0f}"
    if a >= 100: return f"{v:,.1f}"
    return f"{v:,.2f}"


def build_message(header_date, us, kr, mc, us_sum, kr_sum, stance_rows, grade):
    L = [f"📊 섹터 로테이션 · {header_date} 마감", "━━━━━━━━━━━━━━━━"]

    if stance_rows:
        L.append("")
        L.append("🇰🇷 KR MARKET STANCE")
        L.append(f"Grade: {grade['grade']} ({grade['sub']}) · Score {grade['score']} · "
                 f"20SMA 위 {grade['above']}/{grade['total']}")

    if kr:
        L.append("")
        L.append("🇰🇷 KR SECTORS")
        if kr_sum:
            L.append(f"Rotation: {kr_sum['rotation']} · 20SMA 위 {kr_sum['above_count']}/{kr_sum['total_count']}")
        for r in sorted(kr, key=lambda x: (STAGE_ORDER[x["stage"]], -x["rs20"])):
            e = STAGE_EMOJI[r["stage"]]
            L.append(f"{e} {r['name']} {r['change_pct']:+.1f}% (RS{r['rs20']:+.0f}, {r['stage']})")

    if us:
        L.append("")
        L.append("🇺🇸 US SECTORS")
        if us_sum:
            L.append(f"Rotation: {us_sum['rotation']} · 20SMA 위 {us_sum['above_count']}/{us_sum['total_count']}")
        for r in sorted(us, key=lambda x: (STAGE_ORDER[x["stage"]], -x["rs20"])):
            e = STAGE_EMOJI[r["stage"]]
            L.append(f"{e} {r['name']} {r['change_pct']:+.1f}% (RS{r['rs20']:+.0f})")

    if mc:
        L.append("")
        L.append("🌐 MACRO")
        for r in sorted(mc, key=lambda x: (STATUS_ORDER[x["status"]], -x["change_pct"])):
            L.append(f"{STATUS_EMOJI[r['status']]} {r['name']} {r['change_pct']:+.2f}%")

    L.append("")
    L.append(LEGEND)
    return "\n".join(L)


# ─────────────────────────────────────────────
# 이미지 렌더링
# ─────────────────────────────────────────────
def setup_font():
    for path in [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    ]:
        if os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False


def _pill(ax, x0, x1, yc, label, bg, fg, fs=9.5):
    h = 0.66
    ax.add_patch(FancyBboxPatch((x0, yc - h / 2), x1 - x0, h,
                 boxstyle="round,pad=0.0,rounding_size=0.12", linewidth=0, facecolor=bg))
    ax.text((x0 + x1) / 2, yc, label, color=fg, fontsize=fs,
            ha="center", va="center", fontweight="bold")


def ext_color(ext):
    if ext >= LATE_EXT: return {"bg": "#b4740c", "fg": "#ffeccb"}
    if ext >= 0: return {"bg": "#1f7a3d", "fg": "#d6ffe3"}
    if ext > -6: return {"bg": "#7a2b2b", "fg": "#ffdada"}
    return {"bg": "#992222", "fg": "#ffdada"}


def rs_color(rs):
    if rs >= 0:
        return {"bg": "#1f7a3d" if rs >= 8 else "#2e6b45", "fg": "#d6ffe3"}
    return {"bg": "#992222" if rs <= -8 else "#7a2b2b", "fg": "#ffdada"}


def render_sector_table(title, subtitle, rows, rotation=None):
    rows = sorted(rows, key=lambda x: (STAGE_ORDER[x["stage"]], -x["rs20"]))
    n = len(rows)
    H = n * 0.9 + (4.0 if rotation else 3.0)
    fig, ax = plt.subplots(figsize=(13, H * 0.5), dpi=135)
    fig.patch.set_facecolor(IMG_BG); ax.set_facecolor(IMG_BG)
    ax.set_xlim(0, 13); ax.set_ylim(0, H); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.1, 0.1), 12.8, H - 0.2,
                 boxstyle="round,pad=0.0,rounding_size=0.25",
                 linewidth=1, edgecolor=IMG_LINE, facecolor=IMG_PANEL))

    y = H - 0.6
    ax.text(0.4, y, title, color=IMG_TEXT, fontsize=15, fontweight="bold", va="center")
    ax.text(12.6, y, subtitle, color=IMG_SUB, fontsize=10, ha="right", va="center")

    cols = [(0.4, "지표", "left"), (3.6, "가격", "right"), (4.75, "20SMA", "c"),
            (6.35, "Slope", "c"), (7.85, "Ext", "c"), (9.35, "RS20", "c"), (11.3, "Stage", "c")]
    y -= 0.95
    for x, t, al in cols:
        ha = "left" if al == "left" else ("right" if al == "right" else "center")
        ax.text(x, y, t, color=IMG_SUB, fontsize=9, ha=ha, va="center")

    y -= 0.35
    for r in rows:
        y -= 0.9
        sc = SCOL[r["status"]]
        ax.text(0.4, y, r["name"], color=IMG_TEXT, fontsize=10.5, fontweight="bold", va="center")
        ax.text(3.6, y + 0.16, fmt_price(r["price"]), color=IMG_SUB, fontsize=8, ha="right", va="center")
        ax.text(3.6, y - 0.17, f"{r['change_pct']:+.1f}%",
                color=(IMG_POS if r["change_pct"] >= 0 else IMG_NEG),
                fontsize=8, ha="right", va="center")
        _pill(ax, 4.0, 5.5, y, "Above" if r["above_sma"] else "Below", sc["bg"], sc["fg"])
        _pill(ax, 5.6, 7.1, y, "Up" if r["slope_up"] else "Down", sc["bg"], sc["fg"])
        ec = ext_color(r["ext"]); _pill(ax, 7.2, 8.5, y, f"{r['ext']:+.1f}%", ec["bg"], ec["fg"])
        rc = rs_color(r["rs20"]); _pill(ax, 8.6, 10.1, y, f"{r['rs20']:+.1f}", rc["bg"], rc["fg"])
        st = STAGE_COL[r["stage"]]; _pill(ax, 10.2, 12.6, y, r["stage"], st["bg"], st["fg"])

    y -= 1.0
    if rotation:
        rb = {"RISK-ON": "#14331f", "SELECTIVE": "#3a2a08", "CAUTION": "#3a2408", "RISK-OFF": "#331414"}.get(rotation, "#2a2a2a")
        rf = {"RISK-ON": "#8ff0b0", "SELECTIVE": "#ffd487", "CAUTION": "#ffbe73", "RISK-OFF": "#ff9a9a"}.get(rotation, "#ddd")
        above = sum(r["above_sma"] for r in rows)
        ax.add_patch(FancyBboxPatch((0.4, y - 0.05), 12.2, 0.7,
                     boxstyle="round,pad=0.0,rounding_size=0.12", linewidth=0, facecolor=rb))
        ax.text(0.7, y + 0.3, "Rotation", color=rf, fontsize=10, fontweight="bold", va="center")
        ax.text(12.3, y + 0.3, f"{rotation} · 20SMA 위 {above}/{len(rows)}",
                color=rf, fontsize=10, ha="right", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=IMG_BG, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig); buf.seek(0)
    return buf.getvalue()


def render_stance_table(title, subtitle, rows, grade):
    rows = sorted(rows, key=lambda x: (STATUS_ORDER[x["status"]], x["ext"]))
    n = len(rows)
    H = n * 0.9 + 4.2
    fig, ax = plt.subplots(figsize=(10.5, H * 0.5), dpi=135)
    fig.patch.set_facecolor(IMG_BG); ax.set_facecolor(IMG_BG)
    ax.set_xlim(0, 10); ax.set_ylim(0, H); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.1, 0.1), 9.8, H - 0.2,
                 boxstyle="round,pad=0.0,rounding_size=0.25",
                 linewidth=1, edgecolor=IMG_LINE, facecolor=IMG_PANEL))

    y = H - 0.6
    ax.text(0.4, y, title, color=IMG_TEXT, fontsize=15, fontweight="bold", va="center")
    ax.text(9.6, y, subtitle, color=IMG_SUB, fontsize=10, ha="right", va="center")

    y -= 0.95
    for x, t, ha in [(0.4, "Ticker", "left"), (4.5, "20SMA", "center"),
                     (6.2, "Slope", "center"), (7.7, "Ext", "center"), (9.0, "Status", "center")]:
        ax.text(x, y, t, color=IMG_SUB, fontsize=9, ha=ha, va="center")

    y -= 0.35
    for r in rows:
        y -= 0.9
        sc = SCOL[r["status"]]
        ax.text(0.4, y, r["name"], color=IMG_TEXT, fontsize=10.5, fontweight="bold", va="center")
        _pill(ax, 3.7, 5.3, y, "Above" if r["above_sma"] else "Below", sc["bg"], sc["fg"])
        _pill(ax, 5.4, 7.0, y, "Up" if r["slope_up"] else "Down", sc["bg"], sc["fg"])
        ec = ext_color(r["ext"]); _pill(ax, 7.1, 8.4, y, f"{r['ext']:+.1f}%", ec["bg"], ec["fg"])
        _pill(ax, 8.5, 9.6, y, r["status"], sc["bg"], sc["fg"])

    y -= 1.05
    gb = {"공격": "#14331f", "중립": "#2a2f14", "방어": "#3a2408", "회피": "#331414"}.get(grade["grade"], "#331414")
    gf = {"공격": "#8ff0b0", "중립": "#e8e08a", "방어": "#ffbe73", "회피": "#ff9a9a"}.get(grade["grade"], "#ff9a9a")
    ax.add_patch(FancyBboxPatch((0.4, y - 0.05), 9.2, 0.7,
                 boxstyle="round,pad=0.0,rounding_size=0.12", linewidth=0, facecolor=gb))
    ax.text(0.7, y + 0.3, f"Grade  {grade['grade']}", color=gf, fontsize=11, fontweight="bold", va="center")
    ax.text(9.3, y + 0.3, f"{grade['sub']} · Score {grade['score']} · 20SMA 위 {grade['above']}/{grade['total']}",
            color=gf, fontsize=9.5, ha="right", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=IMG_BG, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig); buf.seek(0)
    return buf.getvalue()


def render_bar_chart(title, index_rows, sector_rows):
    idx = sorted(index_rows, key=lambda x: x["change_pct"], reverse=True)
    sec = sorted(sector_rows, key=lambda x: x["change_pct"], reverse=True)
    items = [(r["name"], r["change_pct"], "idx") for r in idx] + \
            [None] + \
            [(r["name"], r["change_pct"], "sec") for r in sec]

    n = len(items)
    fig, ax = plt.subplots(figsize=(8.5, n * 0.42 + 1.4), dpi=135)
    fig.patch.set_facecolor(IMG_BG); ax.set_facecolor(IMG_BG)

    ys, labels = [], []
    pos = n
    for it in items:
        pos -= 1
        if it is None:
            ax.axhline(pos + 0.5, color="#3a3a42", lw=0.8, ls="--")
            continue
        name, chg, kind = it
        col = "#2fa35a" if chg >= 0 else "#c0504d"
        ax.barh(pos, chg, color=col, height=0.62, zorder=3)
        off = 0.15 if chg >= 0 else -0.15
        ha = "left" if chg >= 0 else "right"
        ax.text(chg + off, pos, f"{chg:+.2f}%", color=IMG_TEXT, fontsize=8,
                va="center", ha=ha)
        ys.append(pos)
        labels.append(name)

    ax.set_yticks(ys); ax.set_yticklabels(labels, color=IMG_TEXT, fontsize=9)
    ax.axvline(0, color="#666", lw=1)
    ax.tick_params(axis="x", colors=IMG_SUB, labelsize=8)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=IMG_TEXT, fontsize=13, fontweight="bold", pad=12)
    ax.set_xlim(min(-1, ax.get_xlim()[0]), max(1, ax.get_xlim()[1]))
    ax.margins(y=0.01)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=IMG_BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig); buf.seek(0)
    return buf.getvalue()


# ─────────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────────
def get_subscribers():
    """활성 구독자 chat_id 목록 조회. Supabase 설정 없으면 빈 목록."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key and create_client):
        print("[INFO] Supabase 환경변수 없음 — 구독자 조회 불가")
        return []
    try:
        sb = create_client(url, key)
        res = sb.table("subscribers").select("chat_id").eq("active", True).execute()
        ids = [row["chat_id"] for row in (res.data or [])]
        print(f"[INFO] 구독자 {len(ids)}명")
        return ids
    except Exception as e:
        print(f"[WARN] 구독자 조회 실패: {e}")
        return []


def send_telegram_text(chat_id, text):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not (token and chat_id):
        print("[INFO] 텔레그램 토큰/chat_id 없음 — 텍스트 건너뜀"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=20)
        print(f"[OK] 텍스트 전송 완료 → {chat_id}" if r.ok else f"[WARN] 텍스트 실패({chat_id}): {r.text}")
    except Exception as e:
        print(f"[WARN] 텍스트 예외({chat_id}): {e}")


def send_telegram_photos(chat_id, images):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not (token and chat_id):
        print("[INFO] 텔레그램 토큰/chat_id 없음 — 이미지 건너뜀"); return
    images = [im for im in images if im and im[0]]
    if not images:
        return
    for i in range(0, len(images), 10):
        chunk = images[i:i + 10]
        try:
            if len(chunk) == 1:
                png, cap = chunk[0]
                data = {"chat_id": chat_id}
                if cap: data["caption"] = cap
                r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                  data=data, files={"photo": ("b.png", png, "image/png")}, timeout=60)
            else:
                files, media = {}, []
                for j, (png, cap) in enumerate(chunk):
                    k = f"p{j}"; files[k] = (f"{k}.png", png, "image/png")
                    item = {"type": "photo", "media": f"attach://{k}"}
                    if cap: item["caption"] = cap
                    media.append(item)
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMediaGroup",
                                  data={"chat_id": chat_id, "media": json.dumps(media)}, files=files, timeout=90)
            print(f"[OK] 이미지 전송 완료 → {chat_id}" if r.ok else f"[WARN] 이미지 실패({chat_id}): {r.text}")
        except Exception as e:
            print(f"[WARN] 이미지 예외({chat_id}): {e}")


def broadcast(chat_ids, text, images):
    """여러 구독자에게 텍스트+이미지 순차 발송 (텔레그램 rate limit 완화용 딜레이 포함)."""
    for cid in chat_ids:
        send_telegram_text(cid, text)
        send_telegram_photos(cid, images)
        time.sleep(0.3)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    now = dt.datetime.now(KST)
    captured_at = now.isoformat()
    wd = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    header_date = now.strftime("%m/%d") + f"({wd})"

    stance = collect("KR_STANCE", KR_STANCE, "index")
    kr = collect("KR", KR_SECTORS, "sector", bench=BENCH_KR)
    us = collect("US", US_SECTORS, "sector", bench=BENCH_US)
    mc = collect("MACRO", MACRO, "macro")

    us_sum = summarize("US", us)
    kr_sum = summarize("KR", kr)
    grade = market_grade(stance)

    snapshots = stance + kr + us + mc
    briefings = [b for b in [us_sum, kr_sum] if b]
    save_supabase(snapshots, briefings, captured_at)

    text = build_message(header_date, us, kr, mc, us_sum, kr_sum, stance, grade)
    print("\n" + text)

    setup_font()
    images = []
    if stance:
        images.append((render_stance_table("KR MARKET STANCE", f"{header_date} 마감", stance, grade),
                       f"섹터 로테이션 · {header_date} 마감"))
    if kr:
        images.append((render_sector_table("KR SECTORS", f"{header_date} 마감", kr,
                                            rotation=(kr_sum or {}).get("rotation")), None))
    if us:
        images.append((render_sector_table("US SECTORS", f"{header_date} 마감", us,
                                            rotation=(us_sum or {}).get("rotation")), None))
    if stance or kr:
        images.append((render_bar_chart(f"한국 마켓+섹터 · 1-Day · {now.strftime('%m/%d')}",
                                        stance, kr), None))

    # 온디맨드("지금" 버튼) 요청이면 그 사람에게만, 아니면 구독자 전원에게 브로드캐스트
    ondemand_chat_id = os.environ.get("ON_DEMAND_CHAT_ID", "").strip()
    if ondemand_chat_id:
        print(f"[INFO] 온디맨드 요청 → {ondemand_chat_id} 에게만 발송")
        send_telegram_text(ondemand_chat_id, text)
        send_telegram_photos(ondemand_chat_id, images)
    else:
        subs = get_subscribers()
        if not subs:
            print("[WARN] 구독자가 없음 — 아무에게도 발송 안 됨. 텔레그램에서 /start 를 먼저 보내야 함.")
        else:
            broadcast(subs, text, images)


if __name__ == "__main__":
    main()
