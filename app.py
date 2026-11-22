import os
# 행렬 계산이 CPU 코어를 전부 차지하지 않도록 계산 스레드를 1개로 제한 (numpy를 불러오기 전에 설정해야 함)
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import json
import time
import threading
import html
from concurrent.futures import ThreadPoolExecutor
import re
import streamlit.components.v1 as components
from pathlib import Path
from urllib.parse import urlencode
import hmac
import importlib
import trader                                   # 자동매매 엔진 (같은 폴더의 trader.py)

# GitHub에 trader.py를 새로 올려도 Streamlit이 예전 trader.py를 계속 쓰는 일이 있어서,
# 파일이 바뀌었으면 여기서 다시 불러옴
TRADER_VER = Path(trader.__file__).stat().st_mtime
if getattr(trader, "_loaded_ver", None) != TRADER_VER:
    trader = importlib.reload(trader)
    trader._loaded_ver = TRADER_VER

st.set_page_config(page_title="코인 거래대금 TOP10 · 자동매매", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{padding:0.8rem 0.4rem 110px 0.4rem;max-width:100%;}  /* 아래 110px 여백: GitHub 화면(index.html)이 아래 72px를 가려도 마지막 줄까지 보이게 */
header,[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],footer,
[data-testid="manage-app-button"],[class*="_profileContainer_"],[class*="viewerBadge"],[class*="_terminalButton_"],[class*="_gzau3_"]{display:none !important;}
</style>""", unsafe_allow_html=True)

# ───────────── 차트 계산 설정(고정값) ─────────────
disp, calc, lam, kmul, band_pct = 120, 720, 8e3, 1.6, 0.0


TOP_N = 10   # 거래대금 상위 몇 등까지 보여줄지
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 거래소: 업비트(기본) / 빗썸 — 둘 다 같은 모양의 공개 시세 주소(v1)를 씀
EXCH = {"upbit": ("업비트", "https://api.upbit.com"), "bithumb": ("빗썸", "https://api.bithumb.com")}

# 가격이 거의 안 움직이는 스테이블코인은 순위에서 뺌
STABLE = {"USDT", "USDC", "USDE", "USD1", "USDS", "DAI", "PYUSD", "FDUSD", "TUSD", "RLUSD", "USDP"}


def _num(v):
    """'1,234' / '-1.5' / NaN 등을 숫자(또는 None)로"""
    try:
        f = float(str(v).replace(",", "").replace("%", "").strip())
        return f if np.isfinite(f) else None
    except Exception:
        return None


def dec_of(p) -> int:
    """가격 크기에 맞는 소수 자릿수 (원화마켓 호가 단위 기준)"""
    p = abs(p or 0)
    for lim, d in ((1000, 0), (100, 1), (10, 2), (1, 3), (0.1, 4), (0.01, 5)):
        if p >= lim:
            return d
    return 6


def px(v, d=None) -> str:
    """가격 글자: 1,234 / 12.34 / 0.0123"""
    if v is None:
        return "-"
    d = dec_of(v) if d is None else d
    return f"{v:,.{d}f}"


def _get(xch: str, path: str, params=None, tries: int = 5):
    """거래소 공개 시세 주소 호출. 너무 자주 부르면(429) 잠깐 쉬었다가 다시"""
    base = EXCH[xch][1]
    for i in range(tries):
        r = requests.get(base + path, params=params, headers=UA, timeout=8)
        if r.status_code == 429:
            time.sleep(0.25 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("요청이 너무 많아 잠시 막혔습니다")


@st.cache_data(ttl=86400, show_spinner=False)
def markets(xch: str) -> dict:
    """원화(KRW) 마켓 전체 {KRW-BTC: (한글이름, 영문이름)} — 하루 동안 저장"""
    out = {}
    for it in _get(xch, "/v1/market/all", {"isDetails": "false"}):
        m = str(it.get("market", ""))
        if m.startswith("KRW-"):
            out[m] = (str(it.get("korean_name") or m[4:]), str(it.get("english_name") or m[4:]))
    return out


@st.cache_data(ttl=60, show_spinner=False)
def top_list(n: int = TOP_N, xch: str = "upbit"):
    """24시간 거래대금 1~n위 코인. 1분 동안은 저장해 둔 목록을 그대로 사용 (새로고침 버튼으로 바로 다시 받기)
    한 줄: [마켓, 한글이름, 심볼, 현재가, 대비(부호 포함), 등락률(%), 시가, 고가, 저가, 거래대금(원), 전일종가]"""
    asof = pd.Timestamp.now(tz="Asia/Seoul").strftime("%m-%d %H:%M")
    mk = markets(xch)
    codes = [m for m in mk if m[4:] not in STABLE]
    rows = []
    for i in range(0, len(codes), 100):            # 주소가 너무 길어지지 않게 100개씩 나눠서
        for it in _get(xch, "/v1/ticker", {"markets": ",".join(codes[i:i + 100])}):
            m = it.get("market")
            rate = _num(it.get("signed_change_rate"))
            rows.append([m, mk[m][0], m[4:], _num(it.get("trade_price")), _num(it.get("signed_change_price")),
                         None if rate is None else rate * 100, _num(it.get("opening_price")),
                         _num(it.get("high_price")), _num(it.get("low_price")),
                         _num(it.get("acc_trade_price_24h")), _num(it.get("prev_closing_price"))])
    rows = [r for r in rows if r[9]]
    rows.sort(key=lambda r: r[9], reverse=True)
    return rows[:n], asof


def _daily(key: str, days: int):
    """일봉 (한국시간 오전 9시 기준). key = '거래소:마켓' 예) upbit:KRW-BTC
    한 번에 200개까지만 주므로 나눠서 받아 이어 붙임"""
    xch, m = key.split(":", 1)
    got, to = [], None
    while len(got) < days:
        p = {"market": m, "count": min(200, days - len(got))}
        if to:
            p["to"] = to
        part = _get(xch, "/v1/candles/days", p)
        if not part:
            break
        got += part
        to = part[-1]["candle_date_time_utc"]       # 가장 오래된 캔들 시각 → 그 이전 것을 이어서
        if len(part) < p["count"]:
            break
    d = pd.DataFrame({
        "Open": [_num(c.get("opening_price")) for c in got],
        "High": [_num(c.get("high_price")) for c in got],
        "Low": [_num(c.get("low_price")) for c in got],
        "Close": [_num(c.get("trade_price")) for c in got],
        "Volume": [_num(c.get("candle_acc_trade_volume")) for c in got],
    }, index=pd.to_datetime([c["candle_date_time_kst"] for c in got]).normalize(), dtype=float)
    d = d[~d.index.duplicated(keep="first")].sort_index().dropna(subset=["Close"])
    return d


# ───────────── 공통: 시세 받기 + 매도선·매수선 계산 (리스트 신호와 차트가 똑같이 사용) ─────────────


STORE_TTL = 600     # 받은 시세를 10분 동안 재사용


@st.cache_resource(show_spinner=False)
def _store():
    """리스트에서 받은 종목 시세를 차트도 그대로 쓰도록 서버에 보관하는 저장소"""
    return {"lock": threading.Lock(), "data": {}}


@st.cache_resource(show_spinner=False)
def _sig_store():
    """종목별 신호 보관함 {(코드, 기간): (그 시세를 받은 시각, 신호)}"""
    return {}


def _hist_time(code: str, days: int):
    ent = _store()["data"].get((code, int(days)))
    return ent[0] if ent and time.time() - ent[0] < STORE_TTL else None


def known_signals(codes: tuple, days: int) -> dict:
    """시세도 신호도 이미 보관돼 있는 종목만 {코드: 신호} (받기·계산 없이 바로)"""
    out, ss_ = {}, _sig_store()
    for c in codes:
        t = _hist_time(c, days)
        ent = ss_.get((c, int(days)))
        if t is not None and ent and ent[0] == t:
            out[c] = ent[1]
    return out


def cached_hist(code: str, days: int):
    """저장소에 10분 이내에 받은 시세가 있으면 그것을, 없으면 None"""
    ent = _store()["data"].get((code, int(days)))
    if ent and time.time() - ent[0] < STORE_TTL:
        return ent[1]
    return None


def _fetch(code: str, days: int):
    d = _daily(code, int(days)).tail(int(days))
    st_ = _store()
    with st_["lock"]:
        st_["data"][(code, int(days))] = (time.time(), d)
    return d


def hist(code: str, days: int):
    """종목 일봉 (10분 동안 저장해 두고 리스트·차트가 같이 사용)"""
    d = cached_hist(code, days)
    return d if d is not None else _fetch(code, days)


@st.cache_resource(show_spinner=False, max_entries=32)
def _hp_inv(n: int, lam: float) -> np.ndarray:
    """HP필터 계산용 행렬의 역행렬. 기간(n)이 같으면 모든 종목이 같은 행렬이라 한 번만 만들어
    서버에 계속 보관 (화면을 새로 열어도 다시 만들지 않음)"""
    D = np.zeros((n - 2, n))
    for i in range(n - 2):
        D[i, i:i + 3] = [1, -2, 1]
    return np.linalg.inv(np.eye(n) + lam * D.T @ D)


def hp_trend(z: np.ndarray, lam: float) -> np.ndarray:
    """Hodrick-Prescott 필터의 추세 성분: (I + λ D'D) τ = z"""
    return _hp_inv(len(z), float(lam)) @ z


def calc_bands(y: np.ndarray):
    """곡선추세, 매도선(상한), 매수선(하한) — 자동매매 엔진(trader.py)과 똑같은 계산을 그대로 사용"""
    return trader.calc_bands(y, lam, kmul, band_pct)


TOUCH_TOL = 0          # 0 = 매도선·매수선에 정확히 닿거나 넘었을 때만 신호


def signal_of(y: np.ndarray):
    """'sell'(매도 가능) / 'buy'(매수 가능) / None"""
    _, up, lo = calc_bands(y)
    last = y[-1]
    if last >= up[-1] * (1 - TOUCH_TOL):
        return "sell"
    if last <= lo[-1] * (1 + TOUCH_TOL):
        return "buy"
    return None


def prefetch(codes: tuple, days: int, timeout: float = 40):
    """저장소에 없는(또는 10분 지난) 종목만 동시에 받아 저장. 전체 40초가 지나면 받은 것까지만 사용"""
    from concurrent.futures import wait
    missing = [c for c in codes if cached_hist(c, days) is None]
    if missing:
        ex = ThreadPoolExecutor(max_workers=4)     # 거래소 요청 제한(초당 10번)에 걸리지 않게 4개씩
        futs = [ex.submit(_fetch, c, days) for c in missing]
        wait(futs, timeout=timeout)
        ex.shutdown(wait=False, cancel_futures=True)
    return {c: cached_hist(c, days) for c in codes}


def fmt_sec(x: float) -> str:
    return f"{x:.1f}초" if x < 10 else f"{x:.0f}초"


def timing_html(parts) -> str:
    """[('리스트', 0.3), ('시세', 5.2), ...] → 작은 회색 글씨 한 줄. 느린 칸(3초 이상)은 주황색"""
    out = []
    for k, v in parts:
        col = "#d9480f" if v >= 3 else "#888"
        out.append(f'{k} <b style="color:{col}">{fmt_sec(v)}</b>')
    return '<span class="tm">⏱ ' + " · ".join(out) + "</span>"


# ───────────── 오늘 시가·고가·저가가 나온 시각 ─────────────
# 거래소마다 '하루' 시작이 다름 (업비트 오전 9시, 빗썸은 다를 수 있음) → 최신 일봉의 시작 시각을 시가 시각으로 사용
# 고가·저가: 60분봉에서 리스트의 고가·저가와 똑같은 값이 나온 시간을 찾고, 그 1시간의 1분봉에서 몇 분인지 찾음
def _kst(c) -> pd.Timestamp:
    return pd.Timestamp(c["candle_date_time_kst"])


def _same(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(abs(b), 1e-12) * 1e-9


def _minutes_of_hour(xch: str, m: str, t: pd.Timestamp) -> list:
    """t(한국시간)부터 1시간 동안의 1분봉. 거래소마다 'to' 표기가 달라 몇 가지 방식으로 시도하고,
    실제로 그 1시간 안의 캔들만 골라 씀"""
    def inside(lst):
        return [c for c in lst if t <= _kst(c) < t + pd.Timedelta(hours=1)]
    now = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    if now - t < pd.Timedelta(minutes=190):                 # 최근 3시간 안이면 'to' 없이 최근 200개로 충분
        got = inside(_get(xch, "/v1/candles/minutes/1", {"market": m, "count": 200}))
        if got:
            return got
    end_kst = t + pd.Timedelta(hours=1)
    end_utc = end_kst - pd.Timedelta(hours=9)
    for to in (end_utc.strftime("%Y-%m-%dT%H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S"),
               end_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00"), end_kst.strftime("%Y-%m-%d %H:%M:%S")):
        try:
            got = inside(_get(xch, "/v1/candles/minutes/1", {"market": m, "count": 60, "to": to}))
            if got:
                return got
        except Exception:
            continue
    return []


@st.cache_data(ttl=300, show_spinner=False)     # 5분 동안 저장 (고가·저가 값이 바뀌면 새로 구함)
def hl_times(key: str, hi=None, lo=None):
    """{'o': '09:00', 'h': '03:15', 'l': '10:42'} — 못 구한 칸은 빈 칸"""
    xch, m = key.split(":", 1)
    out = {}
    s0 = None
    try:
        day = _get(xch, "/v1/candles/days", {"market": m, "count": 1})
        s0 = _kst(day[0])
        out["o"] = s0.strftime("%H:%M")
    except Exception:
        pass
    hours = _get(xch, "/v1/candles/minutes/60", {"market": m, "count": 25})
    if not hours:
        return out
    win = [c for c in hours if s0 is None or _kst(c) >= s0] or hours
    for k, f, pick, target in (("h", "high_price", max, hi), ("l", "low_price", min, lo)):
        hit = [c for c in hours if _same(_num(c.get(f)), target)]      # 리스트 값과 같은 시간(가장 최근)
        hc = hit[0] if hit else pick(win, key=lambda c: _num(c.get(f)) or (0 if pick is max else 1e30))
        t = _kst(hc)
        try:
            mins = _minutes_of_hour(xch, m, t)
            if mins:
                mhit = [c for c in mins if _same(_num(c.get(f)), target or _num(hc.get(f)))]
                mc = mhit[-1] if mhit else pick(mins, key=lambda c: _num(c.get(f)) or (0 if pick is max else 1e30))
                t = _kst(mc)
        except Exception:
            pass
        out[k] = t.strftime("%H:%M")
    return out


def all_hl_times(items: tuple, timeout: float = 20) -> dict:
    """리스트 코인들의 시가·고가·저가 시각을 동시에 구함. items = ((키, 고가, 저가), ...)
    20초 안에 못 구한 코인은 빈 칸"""
    from concurrent.futures import wait
    ex = ThreadPoolExecutor(max_workers=3)
    futs = {ex.submit(hl_times, k, hi, lo): k for k, hi, lo in items}
    wait(futs, timeout=timeout)
    ex.shutdown(wait=False, cancel_futures=True)
    return {k: f.result() for f, k in futs.items() if f.done() and not f.exception()}


def list_signals(codes: tuple):
    """리스트 종목들의 매수·매도 신호 {코드: 'buy'/'sell'/None}, 시세를 받은 종목 수"""
    days = max(int(calc), 180)
    _t = time.time()
    data = prefetch(codes, days)
    t_fetch = time.time() - _t
    _t = time.time()
    sig, got = {}, 0
    known = known_signals(codes, days)
    ss_ = _sig_store()
    for c in codes:
        d = data.get(c)
        sig[c] = None
        if d is None:
            continue
        got += 1
        if c in known:                    # 같은 시세로 이미 계산해 둔 신호
            sig[c] = known[c]
            continue
        try:
            y = d["Close"].to_numpy(dtype=float)
            if len(y) >= 60 and np.isfinite(y).all():
                sig[c] = signal_of(y)
        except Exception:
            pass
        t = _hist_time(c, days)
        if t is not None:
            ss_[(c, int(days))] = (t, sig[c])
    return sig, got, t_fetch, time.time() - _t


# ───────────── 자동매매 (코인002) ─────────────
# API 키는 코드에 쓰지 않고 .streamlit/secrets.toml 에만 넣음 (GitHub에 올리면 절대 안 됨)
def _secret(k: str) -> str:
    try:
        return str(st.secrets.get(k, "") or "").strip()
    except Exception:
        return ""


@st.cache_resource(show_spinner=False)
def _engines() -> list:
    """지금까지 만든 엔진 목록 (trader.py가 바뀌면 새 엔진을 만들고 예전 것은 멈추기 위해)"""
    return []


@st.cache_resource(show_spinner=False)
def _get_engine(ver: float):
    import gc                                    # 예전 app.py가 만든 엔진까지 찾아서
    olds = list(_engines()) + [o for o in gc.get_objects() if type(o).__name__ == "Engine" and hasattr(o, "data")]
    for old in olds:                             # 예전 trader.py로 만든 엔진은 멈춤 (두 개가 같이 주문하지 않게)
        try:
            with old.lock:
                old.data["enabled"] = False
        except Exception:
            pass
    ak, sk = _secret("UPBIT_ACCESS_KEY"), _secret("UPBIT_SECRET_KEY")
    client = trader.Upbit(ak, sk) if ak and sk else None
    eng = trader.Engine(Path(__file__).parent, client)
    eng.all_off()                                # 자동매매가 꺼진 채로 시작하므로 코인 버튼도 모두 꺼진 상태로
    eng.start()
    _engines().append(eng)
    return eng


def get_engine():
    """자동매매 엔진은 프로그램(서버)마다 하나만 만들어 계속 돌림 → 브라우저를 닫아도 계속 감시"""
    return _get_engine(TRADER_VER)


@st.cache_data(ttl=10, show_spinner=False)
def account_info():
    """업비트 잔고 (10초 동안 저장)"""
    return get_engine().client.accounts()


def _ip_hint(msg: str) -> str:
    if "ip" in msg.lower():
        return (" → 업비트 Open API 관리 화면에서 이 컴퓨터의 공인 IP를 '허용 IP'로 등록해야 합니다. "
                "(주문 권한은 IP 등록이 필수예요)")
    if "jwt" in msg.lower() or "invalid_access_key" in msg.lower():
        return " → Access/Secret 키가 맞는지 secrets.toml을 확인해 주세요."
    return ""


LIST2_CSS = """<style>
/* 맨 위 줄: [모의][실전] + [자동매매 켜짐/꺼짐] */
.st-key-topbar [data-testid="stHorizontalBlock"]{gap:10px !important}
.sl-top a.tab{white-space:nowrap}
.st-key-topctl{border:1px solid #c9ced6;border-radius:10px;padding:6px 10px;background:#fff}
.st-key-topctl [data-testid="stHorizontalBlock"]{flex-wrap:nowrap !important;gap:6px !important}
.st-key-topctl [data-testid="stColumn"]{min-width:0 !important;width:auto !important}
.st-key-topctl button{min-height:38px;padding:4px 8px !important}
.st-key-topctl button p{font-size:15px !important;font-weight:700;white-space:nowrap}
.st-key-mode_mock button,.st-key-mode_real button{background:#fff !important;border-color:#c9ced6 !important}
.st-key-mode_mock button p,.st-key-mode_real button p{color:#888 !important}
.st-key-mode_mock button[kind="primary"],.st-key-mode_mock [data-testid="stBaseButton-primary"]{background:#eef1f5 !important;border:2px solid #555 !important}
.st-key-mode_mock button[kind="primary"] p,.st-key-mode_mock [data-testid="stBaseButton-primary"] p{color:#333 !important}
.st-key-mode_real button[kind="primary"],.st-key-mode_real [data-testid="stBaseButton-primary"]{background:#fdecec !important;border:2px solid #e5484d !important}
.st-key-mode_real button[kind="primary"] p,.st-key-mode_real [data-testid="stBaseButton-primary"] p{color:#e5484d !important}
.st-key-trade_onoff button{background:#f4f5f7 !important;border-color:#c9ced6 !important}
.st-key-trade_onoff button p{color:#666 !important}
.st-key-trade_onoff button[kind="primary"],.st-key-trade_onoff [data-testid="stBaseButton-primary"]{background:#e6f6ec !important;border:2px solid #0aa05a !important}
.st-key-trade_onoff button[kind="primary"] p,.st-key-trade_onoff [data-testid="stBaseButton-primary"] p{color:#0a8a4c !important}
@media (max-width:600px){.st-key-topbar [data-testid="stColumn"]{min-width:100% !important}}
/* 리스트 테두리. 세로 스크롤바가 생기지 않게 세로는 숨기고, 창이 아주 좁을 때만 가로로 밀어서 보기 */
.st-key-coinlist{gap:0 !important;border:1px solid #c9ced6;overflow-x:auto;overflow-y:hidden}
.st-key-coinlist [data-testid="stElementContainer"]{margin:0 !important}
/* Streamlit 글 칸은 아래 여백이 -1rem 이라 다음 줄이 위로 겹쳐 글씨를 가림 → 0으로 */
.st-key-coinlist [data-testid="stMarkdownContainer"]{margin-bottom:0 !important}
.st-key-coinlist [data-testid="stMarkdownContainer"]>div{margin:0}
/* 가격 칸은 가장 긴 숫자도 다 들어가는 폭보다 좁아지지 않음 → 잘리지 않음 (모든 줄이 같은 폭이라 세로줄도 맞음) */
.cl-row{display:grid;grid-template-columns:42px minmax(110px,1.1fr) minmax(80px,.8fr) minmax(112px,1fr) minmax(100px,.9fr)
 minmax(80px,.8fr) minmax(80px,.8fr) minmax(152px,1.3fr) minmax(152px,1.3fr) minmax(152px,1.3fr);
 font-size:13px;line-height:1.35;background:#fff;font-variant-numeric:tabular-nums;min-width:1060px}
.cl-row>span{padding:7px 8px;border-right:1px solid #e3e6ea;border-top:1px solid #e3e6ea;text-align:right;white-space:nowrap;
 display:flex;align-items:center;justify-content:flex-end;min-height:44px;box-sizing:border-box}
.cl-row>span:last-child{border-right:0}
.cl-row.hd>span{background:#e9edf3;color:#333;font-weight:600;justify-content:center;border-top:0;border-right-color:#d3d8df;min-height:36px}
.cl-row.ev>span{background:#fafbfc}
.cl-row.open>span{background:#eef4ff}
.cl-row .rk{justify-content:center;color:#888}
.cl-row .nm{flex-direction:column;align-items:flex-start;justify-content:center;font-weight:700;color:#111;text-align:left;overflow:hidden}
.cl-row .nm>span:first-child{max-width:100%;overflow:hidden;text-overflow:ellipsis}
.cl-row .nm .sub{display:flex;flex-wrap:wrap;align-items:center;gap:2px 3px;font-size:11px;font-weight:400;color:#999;white-space:normal}
/* 켜진 버튼 표시: 코인 영문 옆(둘째 줄)에 붙이고, 칸이 좁으면 다음 줄로 넘어감 → 잘리지 않음 */
.cl-row .nm .on{display:inline-block;padding:0 4px;border-radius:3px;color:#fff;font-size:10px;font-weight:600;line-height:15px}
.cl-row .nm .on.up{background:#1a56db}.cl-row .nm .on.mid{background:#444}.cl-row .nm .on.low{background:#e5484d}
.cl-row .sg{justify-content:center;font-weight:800}.cl-row .sg.buy{color:#e5484d}.cl-row .sg.sell{color:#1a56db}
.cl-row .tv{color:#333}
.cl-row .hm{font-size:11px;color:#999;font-weight:400;margin-left:4px}
.cl-row .up{color:#e5484d}.cl-row .dn{color:#1a56db}.cl-row .eq{color:#333}
@media (max-width:600px){
 .cl-row{grid-template-columns:32px minmax(0,1fr) 64px 100px 68px;min-width:0;font-size:12.5px}
 .cl-row>span{padding:7px 5px}
 .cl-row .pc{display:none !important}
}
[class*="st-key-lrow_"]{position:relative;gap:0 !important}
[class*="st-key-lbtn_"]{position:absolute !important;inset:0;z-index:3}
[class*="st-key-lbtn_"] div,[class*="st-key-lbtn_"] button{width:100% !important;height:100% !important}
[class*="st-key-lbtn_"] button{opacity:0;cursor:pointer;min-height:0 !important}
.cbtn{margin-left:auto;display:inline-block;padding:6px 14px;font-size:14px;font-weight:500;white-space:nowrap;
 border:1px solid #c9ced6;border-radius:8px;background:#fff;color:#31333f !important;text-decoration:none !important}
.cbtn:hover{border-color:#1f4fa8;color:#1f4fa8 !important}
.pn-top{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
.pn-left{flex:0 0 auto;min-width:230px}
.hbox{flex:1 1 420px;min-width:0;background:#f7f8fa;border:1px solid #e3e6ea;border-radius:8px;padding:8px 12px;font-size:13px;color:#333}
.hbox .plan{margin-bottom:6px;line-height:1.5}
.hbox .mk{display:inline-block;margin-right:6px;padding:0 5px;border-radius:3px;background:#888;color:#fff;font-size:11px}
.hbox .hgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px 14px}
.hbox .hgrid>div{display:flex;justify-content:space-between;gap:6px;white-space:nowrap;border-bottom:1px dashed #e3e6ea;padding:2px 0}
.hbox .lb{color:#888}.hbox .vl{font-weight:600;font-variant-numeric:tabular-nums}
.hbox .r{color:#e5484d}.hbox .b{color:#1a56db}.hbox .mt{color:#999;font-size:12px}
.pn-top .cbtn{margin-left:0}
@media (max-width:900px){.hbox .hgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:600px){.pn-left{min-width:0;flex:1 1 100%}.hbox{flex:1 1 100%;order:3}.pn-top{gap:8px}.pn-top .cbtn{position:absolute;right:12px;top:10px}.pn-left{padding-right:96px}
 [class*="st-key-lpanel_"]{position:relative}}
[class*="st-key-lpanel_"]{background:#fff;border-top:1px solid #e3e6ea;padding:10px 12px 12px 12px;gap:8px !important}
[class*="st-key-lamt_"] [data-testid="stHorizontalBlock"],[class*="st-key-tbtn_"] [data-testid="stHorizontalBlock"]{flex-wrap:nowrap !important;gap:6px !important;align-items:center}
[class*="st-key-lamt_"] [data-testid="stColumn"],[class*="st-key-tbtn_"] [data-testid="stColumn"]{min-width:0 !important;width:auto !important}
[class*="st-key-tbtn_"] [data-testid="stColumn"]{flex:1 1 0 !important}
[class*="st-key-tbtn_"] [data-testid="stColumn"]:nth-child(-n+2){flex:0 0 100px !important}
[class*="st-key-tbtn_"] [class*="st-key-btn_all_"] button{background:#f4f5f7 !important;border-color:#c9ced6 !important}
[class*="st-key-tbtn_"] [class*="st-key-btn_all_"] button p{color:#555 !important}
@media (max-width:600px){[class*="st-key-tbtn_"] [data-testid="stColumn"]:nth-child(-n+2){flex:0 0 52px !important}}
[class*="st-key-lamt_"]{background:#fff5f5;border:1px solid #f3c9cb;border-radius:8px;padding:6px 10px}
[class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(1){flex:0 0 auto !important}
[class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(2),[class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(4){flex:0 0 40px !important}
[class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(3){flex:0 0 110px !important}
[class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(5){flex:1 1 auto !important}
@media (max-width:600px){[class*="st-key-lamt_"] [data-testid="stHorizontalBlock"]{flex-wrap:wrap !important}
 [class*="st-key-lamt_"] [data-testid="stColumn"]:nth-child(5){flex:1 1 100% !important}}
.bkinfo{font-size:13px;color:#666}.bkinfo b{color:#e5484d}
.bkval{font-size:17px;font-weight:700;color:#e5484d;text-align:center;white-space:nowrap}.bkval.mt{color:#999;font-size:15px}
[class*="st-key-bkm_"] button,[class*="st-key-bkp_"] button{min-height:34px;padding:0 !important;font-weight:700}
[class*="st-key-bkm_"] button p,[class*="st-key-bkp_"] button p{font-size:18px !important}
.offnote{font-size:13px;color:#8a5a00;background:#fff4dc;border:1px solid #f1d9a6;border-radius:8px;padding:6px 10px}
[class*="st-key-tbtn_"] button:disabled{opacity:.45}
[class*="st-key-btn_allon_"] button{background:#f4f5f7 !important;border-color:#c9ced6 !important}
[class*="st-key-tbtn_"] [class*="st-key-btn_allon_"] button p{color:#333 !important}

[class*="st-key-tbtn_"] button{padding:6px 4px !important;min-height:42px;background:#fff !important}
[class*="st-key-tbtn_"] button p{font-size:14px !important;font-weight:600;line-height:1.3;white-space:normal !important;word-break:keep-all}
/* 버튼 글씨 색: 자동매도 파랑 · 중간자동매도 검정 · 자동매수 빨강 */
[class*="st-key-btn_up_"] button p{color:#1a56db !important}
[class*="st-key-btn_mid_"] button p{color:#111 !important}
[class*="st-key-btn_low_"] button p{color:#e5484d !important}
[class*="st-key-btn_up_"] button{border-color:#9db4e0 !important}
[class*="st-key-btn_low_"] button{border-color:#f0b4b6 !important}
/* 켜진 버튼(취소로 바뀐 것): 글씨 색은 그대로 두고 바탕을 옅게 칠하고 테두리를 굵게 */
[class*="st-key-btn_up_"] button[kind="primary"],[class*="st-key-btn_up_"] [data-testid="stBaseButton-primary"]{background:#e8f0fe !important;border:2px solid #1a56db !important}
[class*="st-key-btn_mid_"] button[kind="primary"],[class*="st-key-btn_mid_"] [data-testid="stBaseButton-primary"]{background:#eceef1 !important;border:2px solid #111 !important}
[class*="st-key-btn_low_"] button[kind="primary"],[class*="st-key-btn_low_"] [data-testid="stBaseButton-primary"]{background:#fdecec !important;border:2px solid #e5484d !important}
@media (max-width:600px){[class*="st-key-tbtn_"] button p{font-size:12.5px !important}[class*="st-key-tbtn_"] button{min-height:52px}}
</style>"""

BTN_HELP = {"up": "현재가가 매도가능(곡선상한)에 닿으면 남은 것 전부 매도",
            "mid": "추세가 하락 중일 때 현재가가 중간값(곡선)에 닿으면 절반 매도",
            "low": "현재가가 매수가능(곡선하한)에 닿으면 '자동매수 1회 금액'만큼 매수"}
BTN_TAG = {"up": "매도", "mid": "중간", "low": "매수"}       # 리스트 줄에 붙는 작은 표시
BTN_LABEL = {"up": "자동매도", "mid": "중간자동매도", "low": "자동매수"}   # 펼친 칸의 버튼 이름


def _trade_ctx():
    """엔진 + 계좌(잔고) — 한 번 실행에 한 번만 조회"""
    eng = get_engine()
    acc, err = None, ""
    if eng.client is not None:
        try:
            acc = account_info()
        except Exception as e:
            err = str(e)
    return eng, acc, err


def _locked() -> bool:
    """비밀번호를 정해 두었는데 아직 입력하지 않았으면 True"""
    return bool(_secret("TRADE_PASSWORD")) and not st.session_state.get("trade_ok")


def _password_box():
    v = st.text_input("자동매매 비밀번호", type="password", key="trade_pw")
    if v and hmac.compare_digest(v, _secret("TRADE_PASSWORD")):
        st.session_state.trade_ok = True
        st.rerun()
    elif v:
        st.error("비밀번호가 틀렸습니다.")


def _on_row(m):
    """리스트 줄을 누름: 그 코인 칸을 펼치거나(이미 펼쳐져 있으면) 접음"""
    st.session_state.open_coin = None if st.session_state.get("open_coin") == m else m


DEFAULT_BUY_KRW = 10_000      # 자동매수 1회 금액을 아직 안 정했으면 이 금액으로 시작 (아래 [자동매매] 칸에서 바꿈)


BUY_STEP = 5_000              # [−] [+] 한 번에 바뀌는 금액


def _on_amount_step(step):
    """자동매수 1회 금액 [−] / [+] (모든 코인 공통)"""
    eng = get_engine()
    cur = int(eng.snapshot().get("buy_krw") or 0)
    if not cur:
        new = DEFAULT_BUY_KRW if step > 0 else trader.MIN_ORDER_KRW
    else:
        new = max(trader.MIN_ORDER_KRW, cur + step)
    eng.set_buy_krw(new)


def _on_click_all(m, kr):
    """이 코인의 자동매도·중간자동매도·자동매수를 모두 켬"""
    eng = get_engine()
    if not (eng.snapshot().get("buy_krw") or 0) >= trader.MIN_ORDER_KRW:
        eng.set_buy_krw(DEFAULT_BUY_KRW)
    rule = eng.snapshot()["rules"].get(m, {})
    for k in ("up", "mid", "low"):
        if not rule.get(k):
            note = eng.toggle(m, k, kr)
            if note:
                st.session_state.setdefault("trade_msgs", []).append(note)


def _on_cancel_all(m, kr):
    """이 코인의 켜진 버튼(자동매도·중간자동매도·자동매수)을 모두 끔. 걸려 있던 주문도 같이 취소됨"""
    eng = get_engine()
    rule = eng.snapshot()["rules"].get(m, {})
    for k in ("up", "mid", "low"):
        if rule.get(k):
            note = eng.toggle(m, k, kr)
            if note:
                st.session_state.setdefault("trade_msgs", []).append(note)


def _on_toggle(m, k, kr):
    eng = get_engine()
    if k == "low" and not (eng.snapshot().get("buy_krw") or 0) >= trader.MIN_ORDER_KRW:
        eng.set_buy_krw(DEFAULT_BUY_KRW)     # 금액이 없어서 자동매수가 안 눌리는 일이 없게 기본 금액을 넣어 둠
    note = eng.toggle(m, k, kr)
    if note:
        st.session_state.setdefault("trade_msgs", []).append(note)


def _hold_box(eng, acc, m, sym, close) -> str:
    """펼친 칸 가운데 상자: 자동매수 계획(예수금·1회 금액·횟수) + 이 코인 보유수량·주문가능·평균단가·매입/평가금액·손익·수익률"""
    snap = eng.snapshot()
    bk = snap.get("buy_krw") or 0
    mock = snap.get("mock", True)
    # ── 자동매수 계획 ──
    krw = (acc or {}).get("KRW", {}).get("balance") if acc is not None else None
    if krw is None:
        plan = (f'자동매수 1회 <b class="r">{bk:,.0f}원</b>' if bk else '자동매수 1회 금액 <b>미정</b>') + \
               ' · <span class="mt">예수금은 업비트 API 키를 넣으면 보여요</span>'
    elif bk:
        n = int(krw // bk)
        plan = (f'예수금 <b>{krw:,.0f}원</b> 중 1회 <b class="r">{bk:,.0f}원</b>씩 '
                f'<b class="r">{n}번</b> 매수 가능 <span class="mt">(1회 = 예수금의 {bk / krw * 100 if krw else 0:.1f}%)</span>')
    else:
        plan = f'예수금 <b>{krw:,.0f}원</b> · 자동매수 1회 금액 <b>미정</b>'
    # ── 보유 ──
    qty = avail = avg = None
    if acc is not None:
        a = acc.get(sym, {})
        avail = a.get("balance", 0.0)
        qty = avail + a.get("locked", 0.0)
        avg = a.get("avg") or None
    else:                                                   # 키 없음(모의): 프로그램이 '산 셈 친' 수량과 그 가격
        r = snap["rules"].get(m, {})
        qty = avail = r.get("held") or 0.0
        if qty:
            for lg in snap.get("log", []):
                if lg.get("마켓") == m and "매수 체결" in str(lg.get("내용")) and lg.get("가격"):
                    avg = float(lg["가격"])
                    break
    cost = qty * avg if (qty and avg) else 0.0
    value = qty * close if (qty and close) else 0.0
    pnl = value - cost if cost else 0.0
    rate = pnl / cost * 100 if cost else 0.0
    pc = "r" if pnl > 0 else ("b" if pnl < 0 else "")
    d = dec_of(close)
    def q(v):                                              # 수량: 소수 8자리까지, 끝의 0은 지움
        return "0" if not v else f"{v:,.8f}".rstrip("0").rstrip(".")
    cells = [("보유수량", q(qty)), ("주문가능", q(avail)), ("평균단가", f"{px(avg, d)}원" if avg else "-"),
             ("매입금액", f"{cost:,.0f}원" if cost else "-"), ("평가금액", f"{value:,.0f}원" if value else "-"),
             ("평가손익", f'<span class="{pc}">{pnl:+,.0f}원</span>' if cost else "-"),
             ("수익률", f'<span class="{pc}">{rate:+.2f}%</span>' if cost else "-")]
    grid = "".join(f'<div><span class="lb">{k}</span><span class="vl">{v}</span></div>' for k, v in cells)
    tag = '<span class="mk">모의</span>' if mock else ""
    return f'<div class="hbox"><div class="plan">{tag}{plan}</div><div class="hgrid">{grid}</div></div>'


def _coin_panel(eng, acc, m, kr, sym, close, rule):
    """펼쳐진 칸: 코인·현재가 / 곡선 방향 / 상한·곡선·하한 / 보유·진행 / 매수 금액 / 버튼 3개"""
    b = rule.get("bands")                                   # 엔진이 마지막으로 계산한 값 (켜져 있을 때)
    if not b:
        d = cached_hist(f"upbit:{m}", max(int(calc), 180))
        if d is not None and len(d) >= 60 and close:
            y = d["Close"].to_numpy(dtype=float).copy()
            y[-1] = close
            b = trader.bands_now(y)
    if b:
        arrow = ('<span style="color:#1a56db">▼ 하락</span>' if b["falling"]
                 else '<span style="color:#e5484d">▲ 상승</span>')
        lines = (f'<div style="font-size:14px;margin-top:4px">추세 {arrow}</div>'
                 f'<div style="font-size:14px"><span style="color:#1a56db">매도가능 {px(b["up"])}</span> · '
                 f'중간값 {px(b["mid"])} · <span style="color:#e5484d">매수가능 {px(b["low"])}</span></div>')
    else:
        lines = '<div style="font-size:13px;color:#999;margin-top:4px">추세 계산 전입니다 (잠시 후 새로고침)</div>'
    stt = rule.get("state") or "대기"
    stt += f' · {rule["msg"]}' if rule.get("msg") else ""
    chart = html.escape(link(code=m, name=kr, x="upbit"))
    box = _hold_box(eng, acc, m, sym, close)
    with st.container(key=f"lpanel_{m}"):
        st.markdown(
            f'<div class="pn-top"><div class="pn-left">'
            f'<div style="font-size:17px;font-weight:700">{html.escape(kr)} '
            f'<span style="font-size:13px;color:#999;font-weight:400">{html.escape(sym)}</span> · {px(close)}원</div>'
            f'{lines}<div style="font-size:13px;color:#666;margin-top:2px">진행: {html.escape(stt)}</div></div>'
            f'{box}<a class="cbtn" href="{chart}" target="_self">차트 보기</a></div>', unsafe_allow_html=True)
        if _locked():
            _password_box()
            return
        on_all = bool(eng.snapshot()["enabled"])            # 맨 위 [자동매매]가 켜져 있어야 버튼을 쓸 수 있음
        off_tip = "먼저 맨 위 [○ 자동매매 꺼짐]을 눌러 자동매매를 켜 주세요"
        if not on_all:
            st.markdown('<div class="offnote">⏸ 자동매매가 꺼져 있어서 버튼을 누를 수 없어요. '
                        '맨 위 <b>[○ 자동매매 꺼짐]</b>을 눌러 켜 주세요.</div>', unsafe_allow_html=True)
        with st.container(key=f"tbtn_{m}"):
            cols = st.columns(5)
            any_on = any(rule.get(k) for k in ("up", "mid", "low"))
            cols[0].button("모두 취소", key=f"btn_all_{m}", width="stretch", on_click=_on_cancel_all, args=(m, kr),
                           disabled=not on_all,
                           help=(off_tip if not on_all else "이 코인의 자동매도 · 중간자동매도 · 자동매수를 한 번에 모두 끕니다"
                                 if any_on else "켜져 있는 버튼이 없어요"))
            cols[1].button("모두 클릭", key=f"btn_allon_{m}", width="stretch", on_click=_on_click_all, args=(m, kr),
                           disabled=not on_all,
                           help=off_tip if not on_all else "이 코인의 자동매도 · 중간자동매도 · 자동매수를 한 번에 모두 켭니다")
            for col, k in zip(cols[2:], ("up", "mid", "low")):
                is_on = bool(rule.get(k))
                val = f"({px(b[k])})" if b else ""                 # 자동매도(매도가능 값) · 중간자동매도(중간값) · 자동매수(매수가능 값)
                label = f"{BTN_LABEL[k]}{val}" + (" 취소" if is_on else "")
                col.button(label, key=f"btn_{k}_{m}", disabled=not on_all,
                           type="primary" if is_on else "secondary", width="stretch",
                           on_click=_on_toggle, args=(m, k, kr), help=off_tip if not on_all else BTN_HELP[k])
        # ── 자동매수 1회 금액 (모든 코인 공통): [−] 금액 [+] 버튼 — 입력칸이 아니라 버튼이라 값이 저절로 바뀌지 않음 ──
        bk = int(eng.snapshot().get("buy_krw") or 0)
        krw = (acc or {}).get("KRW", {}).get("balance") if acc is not None else None
        with st.container(key=f"lamt_{m}"):
            c0, c1, c2, c3, c4 = st.columns([1.25, 0.45, 1.1, 0.45, 3], vertical_alignment="center")
            c0.markdown('<div style="font-size:14px;font-weight:600;color:#e5484d;white-space:nowrap">자동매수 1회 금액</div>',
                        unsafe_allow_html=True)
            c1.button("−", key=f"bkm_{m}", width="stretch", on_click=_on_amount_step, args=(-BUY_STEP,),
                      disabled=not on_all or bk <= trader.MIN_ORDER_KRW, help="5,000원 내리기 (최소 5,000원)")
            c2.markdown(f'<div class="bkval">{bk:,}원</div>' if bk else '<div class="bkval mt">미정</div>',
                        unsafe_allow_html=True)
            c3.button("+", key=f"bkp_{m}", width="stretch", on_click=_on_amount_step, args=(BUY_STEP,),
                      disabled=not on_all, help="5,000원 올리기")
            if not bk:
                info = f"자동매수를 누르면 {DEFAULT_BUY_KRW:,}원으로 시작해요 · 모든 코인 공통"
            elif krw is not None:
                info = f"예수금 {krw:,.0f}원으로 <b>{int(krw // bk)}번</b> 살 수 있어요 · 모든 코인 공통"
            else:
                info = "모든 코인에 같은 금액이 적용돼요"
            c4.markdown(f'<div class="bkinfo">{info}</div>', unsafe_allow_html=True)
        for msg in st.session_state.pop("_panel_msgs", []):
            st.warning(msg)


def trade_list(rows, sig, hm=None):
    """업비트 리스트 (코인001 표와 같은 모양): 줄을 누르면 바로 아래에 자동매매 칸이 펼쳐짐 (한 번에 하나)"""
    hm = hm or {}
    eng, acc, _ = _trade_ctx()
    rules = eng.snapshot()["rules"]
    items = list(rows)                       # [마켓, 이름, 심볼, 현재가, 대비, 등락률, 시가, 고가, 저가, 거래대금, 전일종가]
    extra = [m for m, r in rules.items() if m not in {it[0] for it in items}
             and (any(r.get(k) for k in trader.BTN) or r.get("state") not in (trader.WAIT, trader.DONE))]
    if extra:                                                # 순위에서 빠졌어도 실행 중인 코인은 맨 아래에 표시
        try:
            pe = trader.prices(extra)
        except Exception:
            pe = {}
        items += [[m, rules[m].get("name") or m[4:], m[4:], pe.get(m)] + [None] * 7 for m in extra]
    msgs = st.session_state.pop("trade_msgs", [])
    if msgs and not st.session_state.get("open_coin"):
        for msg in msgs:
            st.warning(msg)
        msgs = []
    st.session_state["_panel_msgs"] = msgs                  # 펼친 칸 안에서 보여 줌
    st.markdown(LIST2_CSS, unsafe_allow_html=True)
    opened = st.session_state.get("open_coin")

    def when(t):                                             # 가격 옆 작은 회색 (시:분)
        return f'<span class="hm">({t})</span>' if t else ""

    with st.container(key="coinlist"):
        st.markdown('<div class="cl-row hd"><span class="rk">순위</span><span>코인</span><span>신호</span>'
                    '<span>현재가(원)</span><span class="pc">대비</span><span>등락률</span><span class="pc">거래대금</span>'
                    '<span class="pc">시가(시간)</span><span class="pc">고가(시간)</span><span class="pc">저가(시간)</span></div>',
                    unsafe_allow_html=True)
        for i, (m, kr, sym, close, chg, pct, op, hi, lo, tv, prev) in enumerate(items, 1):
            rule = rules.get(m, {})
            d = dec_of(close)
            tt = hm.get(f"upbit:{m}", {})
            v = (sig or {}).get(f"upbit:{m}")
            sg = ('<span class="sg buy">매수 가능</span>' if v == "buy" else
                  '<span class="sg sell">매도 가능</span>' if v == "sell" else '<span class="sg"></span>')
            k = "eq" if pct is None else ("up" if pct > 0 else ("dn" if pct < 0 else "eq"))
            arrow = "" if not pct else ("▲ " if pct > 0 else "▼ ")
            pct_t = "-" if pct is None else f"{pct:+.2f}%" if pct else "0.00%"
            ons = "".join(f'<span class="on {b_}">{BTN_TAG[b_]}</span>'
                          for b_ in ("up", "mid", "low") if rule.get(b_))          # 켜져 있는 버튼 표시
            cls = "cl-row" + (" open" if opened == m else (" ev" if i % 2 == 0 else ""))
            with st.container(key=f"lrow_{m}"):
                st.markdown(
                    f'<div class="{cls}"><span class="rk">{i if i <= len(rows) else "-"}</span>'
                    f'<span class="nm"><span>{html.escape(kr)}</span><span class="sub"><span>{html.escape(sym)}</span>{ons}</span></span>{sg}'
                    f'<span class="{k}">{px(close, d)}</span>'
                    f'<span class="pc {k}">{arrow}{px(None if chg is None else abs(chg), d)}</span>'
                    f'<span class="{k}">{pct_t}</span>'
                    f'<span class="pc tv">{money(tv)}</span>'
                    f'<span class="pc {ud(op, prev)}">{px(op, d)}{when(tt.get("o"))}</span>'
                    f'<span class="pc {ud(hi, prev)}">{px(hi, d)}{when(tt.get("h"))}</span>'
                    f'<span class="pc {ud(lo, prev)}">{px(lo, d)}{when(tt.get("l"))}</span></div>',
                    unsafe_allow_html=True)
                st.button(f"{kr} 펼치기", key=f"lbtn_{m}", on_click=_on_row, args=(m,))
            if opened == m:
                _coin_panel(eng, acc, m, kr, sym, close, rule)


@st.dialog("실전 모드로 바꿀까요?")
def _real_dialog():
    st.markdown("실전 모드에서는 **업비트에 실제 돈으로 주문**이 나갑니다.  \n"
                "모의 모드에서 원하는 대로 사고파는지 충분히 확인하셨나요?")
    c1, c2 = st.columns(2)
    if c1.button("실전으로 바꾸기", type="primary", width="stretch"):
        get_engine().all_off()
        err = get_engine().set_mock(False)
        if err:
            st.error(err)
        else:
            st.rerun()
    if c2.button("취소", width="stretch"):
        st.rerun()


def _on_mode(want_mock: bool):
    """[모의] / [실전] 버튼"""
    eng = get_engine()
    if want_mock == eng.snapshot()["mock"]:
        return
    if want_mock:
        eng.all_off()                                        # 모드를 바꾸면 자동매매가 꺼지므로 버튼도 모두 끔
        err = eng.set_mock(True)
        if err:
            st.session_state.setdefault("trade_msgs", []).append(err)
    else:
        st.session_state["_ask_real"] = True                # 다음 화면에서 확인 창을 띄움


def _on_onoff():
    """[자동매매 켜기/끄기] 버튼: 누를 때마다 켜짐 ↔ 꺼짐. 끄면 모든 코인의 버튼도 모두 꺼짐"""
    eng = get_engine()
    if eng.snapshot()["enabled"]:
        eng.all_off()                                        # 자동매도·중간자동매도·자동매수 모두 끔 (걸린 주문도 취소)
        eng.set_enabled(False)
    else:
        eng.set_enabled(True)


def top_controls():
    """화면 맨 위 줄: [모의][실전] + [자동매매 켜짐/꺼짐] — 모두 버튼이라 화면에 겹쳐 남지 않음"""
    eng = get_engine()
    if _locked():
        st.markdown('<div style="font-size:13px;color:#888;text-align:center">🔒 자동매매는 아래에서 비밀번호를 넣어 주세요</div>',
                    unsafe_allow_html=True)
        return
    snap = eng.snapshot()
    mock, on = snap["mock"], snap["enabled"]
    with st.container(key="topctl"):
        c1, c2, c3 = st.columns([1, 1, 2.3], vertical_alignment="center")
        c1.button("모의", key="mode_mock", type="primary" if mock else "secondary", width="stretch",
                  on_click=_on_mode, args=(True,), help="모의 = 실제 주문 없이 기록만 남김")
        c2.button("실전", key="mode_real", type="secondary" if mock else "primary", width="stretch",
                  on_click=_on_mode, args=(False,), disabled=eng.client is None,
                  help="실전 = 업비트에 실제 주문" + ("" if eng.client else " (업비트 API 키가 있어야 해요)"))
        c3.button("● 자동매매 켜짐" if on else "○ 자동매매 꺼짐", key="trade_onoff",
                  type="primary" if on else "secondary", width="stretch", on_click=_on_onoff,
                  help=("지금 켜져 있어요. 누르면 멈춥니다." if on else "지금 꺼져 있어요. 누르면 시작합니다.") +
                       " 켜져 있으면 3초마다 현재가를 확인해서, 버튼이 켜진 코인만 조건이 맞을 때 주문합니다.")
    if st.session_state.pop("_ask_real", False) and snap["mock"]:
        _real_dialog()


def list_area(rows, sig, hm, left_html, asof_html):
    """업비트 화면 위쪽 전체: [탭·새로고침·시간 | 모의·실전·자동매매 켜기 | 기준 시각] + 누르면 펼쳐지는 리스트"""
    with st.container(key="topbar"):
        t1, t2, t3 = st.columns([5.2, 3.6, 2.5], vertical_alignment="center")
        t1.markdown(left_html, unsafe_allow_html=True)
        with t2:
            top_controls()                                  # 모드(모의/실전) · 자동매매 켜기
        t3.markdown(asof_html, unsafe_allow_html=True)
    trade_list(rows, sig, hm)


# 위쪽 버튼(모의/실전, 자동매매 켜기)과 리스트 안의 버튼(줄 펼치기, 자동매도/중간자동매도/자동매수, 모두 취소)을
# 누르면 이 부분만 다시 그림 → 페이지 전체를 다시 계산하지 않으므로 리스트가 접혔다 펴지지 않음
list_area_part = st.fragment(list_area) if hasattr(st, "fragment") else list_area


def trade_panel():
    """리스트 아래: 매매 기록만 (예수금·금액·모드·켜기는 위로 옮김). 오류가 있을 때만 빨간 안내"""
    eng, acc, acc_err = _trade_ctx()
    if _locked():
        return
    if acc_err:
        st.error(f"계좌 조회 실패: {acc_err}{_ip_hint(acc_err)}")

    def live_log():                                       # 5초마다 이 부분만 새로 고침
        s = eng.snapshot()
        if eng.last_error:
            st.caption(f"⚠️ 최근 오류: {eng.last_error}{_ip_hint(eng.last_error)}")
        n = min(len(s["log"]), 30)
        with st.expander(f"매매 기록 (최근 {n}개)" if n else "매매 기록 (아직 없음)", expanded=False):
            if s["log"]:
                st.dataframe(pd.DataFrame(s["log"][:30]), hide_index=True, width="stretch")
                st.caption("전체 기록은 코인002 폴더의 trade_log.csv 파일에 있어요 (엑셀로 열 수 있어요).")
            else:
                st.caption("자동매수·자동매도가 체결되면 여기에 기록돼요.")

    if hasattr(st, "fragment"):
        st.fragment(run_every=5)(live_log)()
    else:
        live_log()


# ───────────── ① 리스트 화면 ─────────────
# 주소에 종목이 있으면(?code=...) 차트 화면, 없으면 리스트 화면
qp = st.query_params
KEEP = {k: qp.get(k) for k in ("embed", "embed_options") if qp.get(k)}   # 감싼 화면(index.html)용 주소 옵션 유지


def link(**kw) -> str:
    return "?" + urlencode({**KEEP, **kw})


def money(v):
    """거래대금(원) → '1.2조' / '3,456억' / '0.5억'"""
    if v is None:
        return "-"
    e = v / 1e8
    if e >= 10000:
        return f"{e / 10000:,.2f}조"
    return f"{e:,.0f}억" if e >= 10 else f"{e:,.1f}억"


def ud(v, base):     # 전일 종가보다 높으면 빨강(up), 낮으면 파랑(dn)
    if v is None or base is None:
        return "eq"
    return "up" if v > base else ("dn" if v < base else "eq")


LIST_CSS = """<style>
.sl-top{display:flex;align-items:center;gap:8px;margin:4px 0 8px 0}
.sl-top a.tab{display:inline-block;min-width:92px;text-align:center;padding:8px 14px;font-size:16px;font-weight:700;
 border:1px solid #c9ced6;background:#f4f5f7;color:#555 !important;border-radius:8px;text-decoration:none !important}
.sl-top a.tab.on{background:#1f4fa8;border-color:#1f4fa8;color:#fff !important}
.sl-top a.tab.rf{min-width:0;font-size:14px;font-weight:600;color:#1f4fa8 !important;background:#fff;border-color:#9db4e0}
.sl-top .asof{margin-left:auto;font-size:12px;color:#888;white-space:nowrap}
.tm{font-size:12px;color:#888;white-space:nowrap}.tm b{font-weight:600}
.sl-top .brk{display:none}
@media (max-width:600px){.sl-top .pc{display:none}.sl-top{flex-wrap:wrap;row-gap:6px}.sl-top .brk{display:block;flex-basis:100%;height:0}.sl-top .tm{flex:1 1 auto;font-size:12px}.sl-top .asof{font-size:12px}}
.sl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid #c9ced6}
.sl-wrap table{border-collapse:collapse;width:100%;min-width:820px;font-size:13px;margin:0}
.sl-wrap th{background:#e9edf3;color:#333;font-weight:600;padding:6px;border:1px solid #d3d8df;white-space:nowrap;text-align:center}
.sl-wrap td{padding:0;border:1px solid #e3e6ea;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
.sl-wrap td a{display:block;padding:6px;color:inherit !important;text-decoration:none !important}
.sl-wrap td.rk{text-align:center;color:#888;width:34px}
.sl-wrap td.nm{text-align:left;font-weight:700;color:#111;max-width:260px;overflow:hidden;text-overflow:ellipsis}
.sl-wrap td.nm .kr{font-weight:600;color:#1f4fa8;margin-left:4px}
.sl-wrap td.nm .sub{display:block;font-size:11px;font-weight:400;color:#999;overflow:hidden;text-overflow:ellipsis}
.sl-wrap td.tv{color:#333}
.sl-wrap tr:nth-child(even) td{background:#fafbfc}
.sl-wrap tbody tr:hover td{background:#eef4ff}
.sl-wrap td.sg{text-align:center;font-weight:800;min-width:76px}.sl-wrap td.sg.buy{color:#e5484d}.sl-wrap td.sg.sell{color:#1a56db}.sl-wrap td.sg.wait{color:#bbb;font-weight:400}
.sl-wrap .hm{font-size:11px;color:#999;font-weight:400}
.sl-wrap .up{color:#e5484d}.sl-wrap .dn{color:#1a56db}.sl-wrap .eq{color:#333}
@media (max-width:600px){
 .sl-top a.tab{min-width:80px;padding:7px 10px;font-size:15px}
 .sl-top a.tab.rf{min-width:0;padding:7px 8px;font-size:13px}
 .sl-wrap table{min-width:680px}
 .sl-wrap td.nm{max-width:150px}
 .sl-wrap td.nm .kr{display:block;margin-left:0;font-size:12px}
 .sl-wrap th.rk,.sl-wrap td.rk{position:sticky;left:0;z-index:2;background:#fff}
 .sl-wrap th.nm,.sl-wrap td.nm{position:sticky;left:34px;z-index:2;background:#fff;box-shadow:1px 0 0 #d3d8df}
 .sl-wrap th.rk,.sl-wrap th.nm{background:#e9edf3}
 .sl-wrap tr:nth-child(even) td.rk,.sl-wrap tr:nth-child(even) td.nm{background:#fafbfc}
}
</style>"""


XCH = qp.get("x") if qp.get("x") in EXCH else "upbit"          # 기본값은 업비트
XNAME = EXCH[XCH][0]

if not qp.get("code"):
    if qp.get("refresh"):                                         # ↻ 새로고침을 눌렀음 → 시세 다시 받기
        top_list.clear()
        hl_times.clear()
        with _store()["lock"]:
            _store()["data"].clear()
        del st.query_params["refresh"]
    with st.spinner(f"{XNAME} 코인 거래대금 순위를 불러오는 중입니다…"):
        _tl = time.time()
        try:
            rows, asof = top_list(TOP_N, XCH)
        except Exception as e:
            rows, asof = [], ""
            st.caption(f"오류: {e}")
        t_list = time.time() - _tl
    if not rows:
        other = "bithumb" if XCH == "upbit" else "upbit"
        st.error(f"{XNAME} 코인 리스트를 불러오지 못했습니다. 잠시 후 새로고침해 주세요.")
        st.markdown(f'<a href="{link(x=other)}" target="_self">→ {EXCH[other][0]}로 보기</a>', unsafe_allow_html=True)
        st.stop()

    def build(sig, tm="", hm=None, table=True, links=True, part=None):
        hm = hm or {}
        if part == "asof":                              # 업비트 윗줄 오른쪽: 기준 시각만
            return (LIST_CSS + '<div class="sl-top" style="justify-content:flex-end;margin:0"><span class="asof">'
                    f'<span class="pc">24시간 거래대금 TOP{TOP_N} · </span>기준 {asof}<span class="pc"> (한국시간)</span></span></div>')
        h = [LIST_CSS, '<div class="sl-top"' + (' style="margin:0"' if part == "left" else "") + '>']
        for k_, (label, _) in EXCH.items():
            h.append(f'<a class="tab{" on" if k_ == XCH else ""}" href="{link(x=k_)}" target="_self">{label}</a>')
        h.append(f'<a class="tab rf" href="{link(x=XCH, refresh=1)}" target="_self" title="시세 다시 받기">↻ 새로고침</a><span class="brk"></span>')
        h.append(tm or '<span class="tm">⏱ 측정 중…</span>')
        if part != "left":
            h.append(f'<span class="asof"><span class="pc">24시간 거래대금 TOP{TOP_N} · </span>기준 {asof}<span class="pc"> (한국시간)</span></span>')
        h.append('</div>')
        if not table:                                   # 업비트: 위 버튼 줄만 (리스트는 누르면 펼쳐지는 칸으로 따로 그림)
            return "".join(h)
        h.append('<div class="sl-wrap"><table><thead><tr><th class="rk">순위</th>'
                 '<th class="nm">코인</th><th>신호</th><th>현재가(원)</th><th>대비</th><th>등락률</th>'
                 '<th>거래대금</th><th>시가(시간)</th><th>고가(시간)</th><th>저가(시간)</th></tr></thead><tbody>')
        for i, (m, kr, sym, close, chg, pct, op, hi, lo, tv, prev) in enumerate(rows, 1):
            key = f"{XCH}:{m}"
            href = html.escape(link(code=m, name=kr, x=XCH))
            a = f'<a href="{href}" target="_self">' if links else '<a>'     # links=False: 눌러도 차트로 안 넘어감
            d = dec_of(close)
            tt = hm.get(key, {})
            def when(t):                                       # 가격 옆 작은 회색 (시:분)
                return f' <span class="hm">({t})</span>' if t else ""
            k = "eq" if pct is None else ("up" if pct > 0 else ("dn" if pct < 0 else "eq"))
            arrow = "" if not pct else ("▲ " if pct > 0 else "▼ ")
            pct_t = "-" if pct is None else f"{pct:+.2f}%" if pct else "0.00%"
            if sig is None or key not in sig:                 # 아직 계산 중
                sg = '<td class="sg wait">' + a + '…</a></td>'
            else:
                v = sig.get(key)
                sg = ('<td class="sg buy">' + a + '매수 가능</a></td>' if v == "buy" else
                      '<td class="sg sell">' + a + '매도 가능</a></td>' if v == "sell" else
                      '<td class="sg">' + a + '&nbsp;</a></td>')
            h.append(
                f'<tr><td class="rk">{a}{i}</a></td>'
                f'<td class="nm" title="{html.escape(kr)}">{a}{html.escape(kr)}'
                f'<span class="sub">{html.escape(sym)}</span></a></td>'
                f'{sg}'
                f'<td class="{k}">{a}{px(close, d)}</a></td>'
                f'<td class="{k}">{a}{arrow}{px(None if chg is None else abs(chg), d)}</a></td>'
                f'<td class="{k}">{a}{pct_t}</a></td>'
                f'<td class="tv">{a}{money(tv)}</a></td>'
                f'<td class="{ud(op, prev)}">{a}{px(op, d)}{when(tt.get("o"))}</a></td>'
                f'<td class="{ud(hi, prev)}">{a}{px(hi, d)}{when(tt.get("h"))}</a></td>'
                f'<td class="{ud(lo, prev)}">{a}{px(lo, d)}{when(tt.get("l"))}</a></td></tr>')
        h.append('</tbody></table></div>')
        return "".join(h)                             # 줄바꿈 없이 한 덩어리로 넣어야 표가 깨지지 않음

    codes = tuple(f"{XCH}:{r[0]}" for r in rows)
    days = max(int(calc), 180)
    UP = XCH == "upbit"                                       # 업비트 = 누르면 아래로 펼쳐지는 리스트 + 자동매매
    known = known_signals(codes, days)                        # 보관된 신호(받기·계산 없이 바로)
    # 업비트는 화면 칸의 순서를 매번 똑같이 유지: [① 임시 안내 칸] [② 버튼 줄+리스트] [③ 아래 안내·기록]
    # 다시 계산하는 동안에는 ①만 쓰고 ②는 건드리지 않음 → 이전 리스트(펼친 칸 포함)가 그대로 보여서 접혔다 펴지지 않음
    note = st.empty()
    ph = None if UP else st.empty()
    if not UP:
        ph.markdown(build(known), unsafe_allow_html=True)     # ① 계산하는 동안 리스트를 먼저 표시
    elif len(known) < len(codes) and not st.session_state.get("_list_drawn"):
        with note.container():                                # 처음 열 때만 임시 표 (링크 없음 → 차트로 안 넘어감)
            st.caption("⏳ 매수·매도 신호를 계산하는 중입니다… (처음 한 번은 조금 걸려요)")
            st.markdown(build(known, links=False), unsafe_allow_html=True)
    if len(known) < len(codes) and not UP:
        note.caption("⏳ 매수·매도 신호를 계산하는 중입니다… (처음 한 번은 조금 걸려요)")
    _t0 = time.time()
    _hx = ThreadPoolExecutor(max_workers=1)                  # 고가·저가 시각은 신호 계산과 동시에 구함
    _hf = _hx.submit(all_hl_times, tuple((f"{XCH}:{r[0]}", r[7], r[8]) for r in rows))
    warn = []
    try:
        sig, _got, t_fetch, t_calc = list_signals(codes)
    except Exception as e:
        sig, _got, t_fetch, t_calc = {}, len(codes), time.time() - _t0, 0.0
        warn.append(f"신호 계산 오류: {e}")
    if _got < len(rows):          # 시세를 다 못 받았으면 알려줌 (빈칸 = 계산 못 한 코인 포함)
        warn.append(f"※ {len(rows)}개 중 {_got}개만 시세를 받아 신호를 계산했습니다 ({time.time() - _t0:.0f}초).")
    hm = {}
    try:
        hm = _hf.result(timeout=25)
    except Exception:
        hm = {}
    _hx.shutdown(wait=False)
    _tm = timing_html([("리스트", t_list), ("시세", t_fetch), ("계산", t_calc), ("전체", time.time() - _t0)])
    if UP:
        note.empty()
        list_area_part(rows, sig, hm, build(sig, _tm, table=False, part="left"), build(sig, part="asof"))   # ②
        st.session_state["_list_drawn"] = True
        for w in warn:
            st.caption(w)
        st.caption("※ 코인을 누르면 그 아래에 자동매매 버튼이 펼쳐집니다. 스테이블코인(USDT·USDC 등)은 가격이 거의 고정이라 순위에서 뺐습니다. "
                   "(시간)은 그 값이 나온 한국시간이며, 시가 시간은 거래소의 하루 시작 시각입니다.")
        trade_panel()
    else:
        note.empty()
        for w in warn:
            st.caption(w)
        ph.markdown(build(sig, _tm, hm), unsafe_allow_html=True)      # ② 신호 칸 + 걸린 시간 채워서 표시
        st.caption("※ 스테이블코인(USDT·USDC 등)은 가격이 거의 고정이라 순위에서 뺐습니다. (시간)은 그 값이 나온 한국시간이며, 시가 시간은 거래소의 하루 시작 시각입니다.")
        st.caption("※ 자동매매 칸은 [업비트] 화면에서 보입니다.")
    st.stop()


# ───────────── ② 차트 화면 (주식001 차트) ─────────────
_t_chart0 = time.time()
BACK_CSS = ('<style>.cv-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:2px 0 4px 0}'
            '.cv-top a.back{display:inline-block;padding:4px 14px;font-size:15px;font-weight:600;border:1px solid #c9ced6;'
            'border-radius:6px;background:#f4f5f7;color:#333 !important;text-decoration:none !important}'
            '.tm{font-size:12px;color:#888;white-space:nowrap}.tm b{font-weight:600}</style>')


def top_bar(tm: str) -> str:
    """← 목록으로 버튼 + 걸린 시간"""
    return (BACK_CSS + '<div class="cv-top">'
            f'<a class="back" href="{html.escape(link(x=XCH))}" target="_self">← 목록으로</a>{tm}</div>')


top_ph = st.empty()
top_ph.markdown(top_bar('<span class="tm">⏱ 측정 중…</span>'), unsafe_allow_html=True)

market = str(qp.get("code") or "").strip().upper()
if not market.startswith("KRW-"):
    market = "KRW-" + market
code = f"{XCH}:{market}"                      # 저장소·계산에 쓰는 이름 (거래소:마켓)
sym = market[4:]
name = str(qp.get("name") or sym)
name_ph = st.empty()
name_ph.markdown(
    f'<div style="font-size:20px;font-weight:700;line-height:1.3">{html.escape(str(name))} '
    f'<span style="font-size:13px;color:#888;font-weight:400">{html.escape(sym)} · {XNAME}</span></div>',
    unsafe_allow_html=True)

# 시세를 받는 동안 "고장난 것처럼" 보이지 않도록 안내 표시 (차트가 나오면 사라짐)
status = st.empty()
if cached_hist(code, max(int(calc), 180)) is None:
    status.markdown(
        '<div style="padding:10px 12px;border-radius:8px;background:#fff4dc;color:#8a5a00;font-size:14px;line-height:1.5">'
        '⏳ 시세를 불러오는 중입니다. 처음 한 번은 20~30초 걸릴 수 있어요. 잠시만 기다려 주세요.</div>',
        unsafe_allow_html=True)

def load_both(code: str, idx_code: str, days: int):
    """코인과 비교 코인(비트코인) 시세를 동시에 받아 온다. 비교 코인은 8초 안에 못 받으면 None(비교선 없이 표시)"""
    ex = ThreadPoolExecutor(max_workers=2)
    f_stock = ex.submit(hist, code, int(days))
    f_idx = ex.submit(hist, idx_code, int(days))
    try:
        stock = f_stock.result(timeout=40)
        try:
            idx = f_idx.result(timeout=8)
        except Exception:
            idx = None
        return stock, idx
    finally:
        ex.shutdown(wait=False)


calc = max(int(calc), 180)
# 비교선: 비트코인 (비트코인 차트에서는 이더리움)
idx_name, idx_mk = ("이더리움", "KRW-ETH") if sym == "BTC" else ("비트코인", "KRW-BTC")
try:
    _tf = time.time()
    df, _idf = load_both(code, f"{XCH}:{idx_mk}", calc)
    t_fetch = time.time() - _tf
except Exception as e:
    status.empty()
    st.error(f"데이터를 불러오지 못했습니다: {e}")
    st.stop()
if df.empty or len(df) < 60:
    status.empty()
    st.warning("데이터가 부족합니다(60일 이상 필요). 최근에 상장된 코인일 수 있어요.")
    st.stop()

_tc = time.time()
y = df["Close"].to_numpy(dtype=float)
n = len(y)
DEC = dec_of(y[-1])                         # 이 코인 가격에 맞는 소수 자릿수
t = np.arange(n)

# 곡선추세·매도선·매수선 (리스트 신호와 같은 계산)
curve, upper, lower = calc_bands(y)

# 화면에는 최근 disp일만 표시
D = n
init_span = int(min(disp, D))
sl = slice(n - D, n)
dates = df.index.strftime("%Y-%m-%d")[sl]
yv, curvev, upv, lov = y[sl], curve[sl], upper[sl], lower[sl]
def _ohl(c):   # 시가·고가·저가가 비어 있으면 종가로 대신 채움
    a = df[c].to_numpy(dtype=float)[sl] if c in df.columns else yv.copy()
    return np.where(np.isfinite(a) & (a > 0), a, yv)
ov, hv, lv = _ohl("Open"), _ohl("High"), _ohl("Low")
hv, lv = np.maximum.reduce([hv, ov, yv]), np.minimum.reduce([lv, ov, yv])

# ───────────── 지수(나스닥/S&P500) 비교선 ─────────────
idx_arr = None
_live_ok = False
try:
    _s = _idf["Close"].reindex(df.index).ffill().bfill()
    if len(_s) == n and np.isfinite(_s.to_numpy(dtype=float)).all():
        idx_arr = _s.to_numpy(dtype=float)
except Exception:
    idx_arr = None
idx_note = ""
if idx_arr is not None:     # 지수 데이터가 종목보다 며칠 일찍 끝나면 알려줌 (마지막 값으로 채워졌다는 뜻)
    _gap = (df.index.max() - _idf.index.max()).days
    if _gap >= 1 and not _live_ok:
        idx_note = f"※ {idx_name} 시세가 {_idf.index.max():%Y-%m-%d}까지만 있어 최근 {_gap}일은 마지막 값으로 채웠습니다."
idx_scaled = None
if idx_arr is not None:     # 처음 열었을 때 맨 왼쪽 종가에 한 번만 맞춘 뒤 고정 (밀거나 확대해도 변하지 않음)
    idx_scaled = idx_arr * (yv[n - init_span] / idx_arr[n - init_span])
PHONE_SPAN_PY = min(42, init_span)
idx_scaled_p = None
if idx_scaled is not None:  # 폰은 처음 2달치만 보이므로 그 맨 왼쪽 종가에 맞춘 지수선도 따로 준비
    idx_scaled_p = idx_arr * (yv[n - PHONE_SPAN_PY] / idx_arr[n - PHONE_SPAN_PY])

# ───────────── 상단 시세 헤더 ─────────────
last, prev = y[-1], y[-2]
chg = last - prev
pct = chg / prev * 100
col = "#e5484d" if chg > 0 else ("#3b6fd4" if chg < 0 else "#333333")
arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "-")
vol = df["Volume"].iloc[-1]

# ───────────── 종목명 옆: 기준 날짜 + 지수 ─────────────
if idx_arr is not None:
    name_ph.markdown(
        f'<div style="display:flex;align-items:baseline;gap:18px;flex-wrap:wrap">'
        f'<div style="font-size:20px;font-weight:700;line-height:1.3">{html.escape(str(name))} '
        f'<span style="font-size:13px;color:#888;font-weight:400">{html.escape(sym)} · {XNAME}</span></div>'
        f'<div style="font-size:17px;font-weight:700;color:#0aa05a">'
        f'● {df.index[-1]:%Y-%m-%d} {idx_name} {px(idx_arr[-1])}</div>'
        '</div>',
        unsafe_allow_html=True)

# ───────────── 매수·매도 표시 (현재가가 곡선추세 상·하한에 닿거나 넘었을 때) ─────────────
_up, _lo = float(upper[-1]), float(lower[-1])
sig_html = ""
if last >= _up * (1 - TOUCH_TOL):          # 상한에 닿거나 위로 넘음 → 파란색 매도
    sig_html = '<div style="font-size:40px;font-weight:800;color:#1a56db;line-height:1">매도</div>'
elif last <= _lo * (1 + TOUCH_TOL):        # 하한에 닿거나 아래로 내려감 → 빨간색 매수
    sig_html = '<div style="font-size:40px;font-weight:800;color:#e5484d;line-height:1">매수</div>'

_sig = ""
if sig_html:
    _sig_txt, _sig_col = ("매도 가능합니다", "#1a56db") if "매도" in sig_html else ("매수 가능합니다", "#e5484d")
    _sig = f'<div class="hd-sig" style="color:{_sig_col}">{_sig_txt}</div>'
st.markdown(
    '<style>'
    '.hd-row{display:flex;align-items:center;gap:16px;flex-wrap:nowrap}'
    '.hd-px{display:flex;flex-direction:column;align-items:flex-start;gap:4px}'
    '.hd-big{font-size:42px;font-weight:700;line-height:1}'
    '.hd-chg{font-size:16px}'
    '.hd-lines{display:flex;flex-direction:column;gap:10px}'
    '.hd-lbl{font-size:15px;color:#111;line-height:1.4;text-align:center;white-space:nowrap}'
    '.hd-sig{font-size:24px;font-weight:800;line-height:1.2;white-space:nowrap}'
    '.hd-wrap{display:flex;align-items:flex-end;justify-content:space-between;gap:8px 16px;flex-wrap:wrap}'
    '.cv-box{margin-left:auto;padding-right:4px;padding-bottom:2px}'
    '@media (max-width:600px){'
    '.hd-row{gap:10px}.hd-big{font-size:30px}.hd-chg{font-size:14px}'
    '.hd-lbl{font-size:12px}.hd-sig{font-size:17px}.cv-box{margin-left:0;width:100%}}'
    '</style>'
    '<div class="hd-wrap"><div class="hd-row">'
    '<div>'
    f'<div class="hd-px"><span class="hd-big" style="color:{col}">{px(last, DEC)}원</span>'
    f'<span class="hd-chg" style="color:{col}">{arrow} {px(abs(chg), DEC)} &nbsp; {pct:+.2f}%</span></div>'
    f'<div style="font-size:13px;color:#888;margin-top:6px">거래량 {vol:,.{0 if vol >= 100 else 2}f} {html.escape(sym)}</div>'
    '</div>'
    '<div class="hd-lines">'
    f'<div class="hd-lbl" data-ul="1" style="color:#1a56db;font-weight:600">매도선<br>(<span data-k="u">{px(_up, DEC)}</span>)</div>'
    f'<div class="hd-lbl" data-ul="1" style="color:#e5484d;font-weight:600">매수선<br>(<span data-k="l">{px(_lo, DEC)}</span>)</div>'
    '</div>'
    f'{_sig}'
    '</div><div class="cv-box"></div></div>',      # cv-box: 차트가 비교 정보·범례·버튼을 여기에 채움
    unsafe_allow_html=True)

# ───────────── 차트 ─────────────
def direction_colors(v):
    d = np.diff(v, prepend=v[1])
    d[0] = d[1]
    return np.where(d >= 0, "#e5484d", "#3b6fd4")  # 상승=빨강, 하락=파랑


def base_traces(fig):
    fig.add_trace(go.Candlestick(x=dates, open=ov, high=hv, low=lv, close=yv, name="종가", showlegend=False,
                                 increasing=dict(line=dict(color="#e5484d", width=1), fillcolor="#e5484d"),
                                 decreasing=dict(line=dict(color="#3b6fd4", width=1), fillcolor="#3b6fd4")), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=curvev, name="곡선추세", line=dict(color="#111111", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=upv, name="매도선", line=dict(color="#1a56db", width=2.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=lov, name="매수선", line=dict(color="#e5484d", width=2.2)), row=1, col=1)
    if idx_scaled is not None:   # 4번 트레이스: 지수(녹색 점선)
        fig.add_trace(go.Scatter(x=dates, y=idx_scaled, name=idx_name,
                                 line=dict(color="#00a651", width=3, dash="dot")), row=1, col=1)
        if idx_scaled_p is not None:   # 5번 트레이스: 폰용 지수선(처음엔 숨김, 폰이면 JS가 이것으로 교체)
            fig.add_trace(go.Scatter(x=dates, y=idx_scaled_p, name=idx_name, visible=False, showlegend=False,
                                     line=dict(color="#00a651", width=3, dash="dot")), row=1, col=1)


CROSSHAIR_JS = """
function installCrosshair(gd, initSpan, S, B, IDX, IT, IDXNAME, IDXRAW, OPEN, LOW, ANCH, IDX2, IT2, HIGH){
  var LP_MS=500;           // 길게 눌러 진동이 오기까지의 시간(ms)
  var CROSS_DELAY_MS=150;  // 진동 후 십자선이 나타나기까지의 시간(ms) (진동 → 십자선 순서 맞춤)
  var PAN_GAIN=1.5;        // 손가락 이동 속도 배율
  var ZOOM_GAIN=1.5;       // 확대/축소 속도 배율
  var COMPARE_MODE='level'; // 'level'=그날 종목 가격과 그날 지수선(녹색 점선) 비교, 'daily'=그날 등락률끼리 비교
  var SAME_BAND_LEVEL=2;    // level: 가격 차이가 ±2% 이내면 '보합'
  var SAME_BAND_DAILY=0.3;  // daily: 등락률 차이가 ±0.3%p 이내면 '보합'
  var idxOn=true;          // 지수 비교선 표시 여부
  var RPAD_RATIO=0.08;     // 오른쪽 여백: 보이는 기간의 8%만큼 빈 칸 (120일 → 약 10칸, 폰 42일 → 약 3칸)
  var RPAD_MIN=3;          // 오른쪽 여백 최소 칸 수
  // 브라우저(앱 브라우저)가 두 손가락으로 화면 전체를 확대하지 못하게 막기
  try{
    var pd=window.parent.document, mv=pd.querySelector('meta[name=viewport]');
    if(mv)mv.setAttribute('content','width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no');
    if(!pd.getElementById('nozoom_css')){
      var st=pd.createElement('style'); st.id='nozoom_css';
      st.textContent='iframe{touch-action:pan-y !important;}'; pd.head.appendChild(st);
    }
  }catch(err){}
  gd.style.position='relative'; gd.style.touchAction='pan-y';
  gd.style.userSelect='none'; gd.style.webkitUserSelect='none';
  gd.style.webkitTouchCallout='none'; gd.style.webkitTapHighlightColor='transparent';
  var cats=gd.data[0].x, N=cats.length, MINS=Math.min(20,N), MAXS=Math.min(Math.max(120,initSpan),N);
  function clampSpan(v){return Math.round(Math.max(MINS,Math.min(MAXS,v)));}
  function clampI0(v,sp){return Math.round(Math.max(0,Math.min(N-sp,v)));}
  // 핸드폰(화면 폭 600px 이하)은 처음에 약 2달(42거래일)만 확대해서 보여줌. PC는 그대로.
  var PHONE_SPAN=42, ITa=IT;
  var isPhone=false;
  try{ isPhone=Math.min(window.screen&&window.screen.width||9999, window.innerWidth||9999)<=600; }catch(e0){}
  if(isPhone && initSpan>PHONE_SPAN && IDX && IDX2 && IDX2.length===N){
    try{   // 폰: 2달치 기준으로 미리 환산해 둔 지수선으로 교체(보이기/숨기기만 사용)
      IDX=IDX2; ITa=IT2; initSpan=PHONE_SPAN;
      var pr0=Plotly.restyle(gd,{visible:[false]},[IT]); if(pr0&&pr0.then)pr0.then(null,function(){});
      var pr1=Plotly.restyle(gd,{visible:[true]},[IT2]); if(pr1&&pr1.then)pr1.then(null,function(){});
    }catch(e1){}
  } else if(isPhone && initSpan>PHONE_SPAN){ initSpan=PHONE_SPAN; }
  var span=clampSpan(initSpan), i0=N-span;
  var pending=false, mode=null, timer=null, sx=0, sy=0, startI0=0, lastT=null, mdown=false, pinch=null, markersOn=null;
  var tStart=0, lastTap=0, lastTouch=0;

  function mk(css){var d=document.createElement('div');
    d.style.cssText='position:absolute;pointer-events:none;display:none;z-index:50;'+css;
    gd.appendChild(d);return d;}
  var lab='background:#222;color:#fff;font:11px sans-serif;padding:2px 4px;white-space:nowrap;';
  var vl=mk('width:0;border-left:1px solid #333;');
  var hl=mk('height:0;border-top:1px solid #333;');
  var dl=mk(lab+'transform:translateX(-50%);white-space:pre;text-align:center;line-height:1.3;');   // 날짜 + (가격 , 지수) 두 줄
  var pr=mk(lab+'transform:translateY(-50%);');
  function f2(v){return Number(v).toLocaleString('en-US',{minimumFractionDigits:DECI,maximumFractionDigits:DECI});}
  // 오른쪽 위 상자: 비교 요약 3줄 → 범례 + [비교 코인][＋][－] 버튼
  // 위쪽 시세 줄 오른쪽 빈자리(cv-box)에 넣음. 못 찾으면 차트 오른쪽 위에 표시
  var HD=document, host=null;
  try{ host=window.parent.document.querySelector('.cv-box'); if(host){HD=window.parent.document; host.innerHTML='';} }catch(eh){host=null; HD=document;}
  // 상자 = [글씨 칸(비교 요약·범례)] + [버튼 칸]. 버튼 칸은 오른쪽 끝에 따로 고정 → 글씨 길이가 바뀌어도 버튼은 안 움직임
  var outer=HD.createElement('div');
  outer.style.cssText=(host?'':'position:absolute;top:2px;right:4px;z-index:55;')+'max-width:100%;'
    +'display:flex;align-items:flex-end;justify-content:flex-end;gap:14px;';
  (host||gd).appendChild(outer);
  var box=HD.createElement('div');
  box.style.cssText='display:flex;flex-direction:column;align-items:flex-start;gap:2px;min-width:0;';
  outer.appendChild(box);
  var bcol=HD.createElement('div');
  bcol.style.cssText='display:flex;align-items:center;gap:6px;flex:none;';
  outer.appendChild(bcol);
  function bk(css){var d=HD.createElement('div'); d.style.cssText='display:none;'+css; box.appendChild(d); return d;}
  var sm=bk('font:600 12px sans-serif;white-space:nowrap;');                 // 비교 코인 대비 요약
  var dt=bk('font:11px sans-serif;color:#666;white-space:normal;word-break:keep-all;line-height:1.35;');   // 줄바꿈은 띄어쓰기에서만   // 시가·저가·종가·비교 코인
  var ig=bk('font:600 11px sans-serif;color:#00a651;white-space:nowrap;');   // 비교 코인 값(녹색)
  if(IDX){sm.style.display='block'; dt.style.display='block'; ig.style.display='block';}
  var brow=HD.createElement('div');
  brow.style.cssText='margin-top:2px;line-height:1.35;';
  brow.innerHTML='<span style="font:11px sans-serif;color:#111;white-space:nowrap;margin-right:4px">'
    +'<span style="color:#e5484d">▮</span><span style="color:#3b6fd4">▮</span> 종가 캔들&nbsp; ━ 곡선&nbsp; '
    +'<span style="color:#1a56db">━ 상한</span>&nbsp; <span style="color:#e5484d">━ 하한</span></span>';
  box.appendChild(brow);
  // 폰(폭 600px 이하): 글씨는 오른쪽 끝까지 넓게 쓰고, 맨 아래 줄은 [범례 ……… 버튼]으로 배치
  var narrow=false;
  try{ narrow=Math.min(window.screen&&window.screen.width||9999, (HD.defaultView||window).innerWidth||9999)<=600; }catch(en){}
  if(narrow){
    outer.style.flexDirection='column'; outer.style.alignItems='stretch'; outer.style.gap='4px';
    box.style.width='100%';
    var lastRow=HD.createElement('div');
    lastRow.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;';
    outer.appendChild(lastRow);
    lastRow.appendChild(brow); lastRow.appendChild(bcol);
    brow.style.marginTop='0';
  }
  var focus=N-1;           // 십자선이 가리키는 날짜(없으면 맨 오른쪽=최근)
  function sg(v){return (v>=0?'+':'')+v.toFixed(1)+'%';}
  function md(i){return cats[i].slice(5);}
  function pxs(v){return Number(v).toLocaleString('en-US',{minimumFractionDigits:DEC,maximumFractionDigits:DEC});}
  function updateSummary(){   // 선택한 날짜(없으면 최근 날짜)의 종목 가격과 지수로 강함/약함/보합 판단
    var D0=focus;
    try{
      var su=window.parent.document.querySelectorAll('[data-k=u]'), sl2=window.parent.document.querySelectorAll('[data-k=l]');
      for(var q=0;q<su.length;q++)su[q].textContent=pxs(S[2][D0]);
      for(var q2=0;q2<sl2.length;q2++)sl2[q2].textContent=pxs(S[3][D0]);}catch(e2){}
    if(!IDX)return;
    var R=IDXRAW||IDX, D=focus, d, band, row2, unit;
    if(COMPARE_MODE==='daily' && D>=1){
      var sr=(S[0][D]/S[0][D-1]-1)*100, ir=(R[D]/R[D-1]-1)*100;
      d=sr-ir; band=SAME_BAND_DAILY; unit='%p ';
      row2='그날 등락 종목 '+sg(sr)+' · '+IDXNAME+' '+sg(ir);
    } else {
      d=(S[0][D]/IDX[D]-1)*100; band=SAME_BAND_LEVEL; unit='% ';     // 종목 가격이 지수선보다 몇 % 위/아래인지
      // 시가·저가·종가는 전일 종가 대비, 지수선은 전일 지수선 대비 등락률
      var prevClose = D > 0 ? S[0][D-1] : NaN;
      var prevIndex = D > 0 ? IDX[D-1] : NaN;
      function coloredValue(label, value, base, pctSourceValue){
        // pctSourceValue가 있으면 실제 원본 지수값으로 등락률을 계산
        var compareValue = (pctSourceValue !== undefined) ? pctSourceValue : value;
        var pct = (isFinite(base) && base !== 0 && isFinite(compareValue))
                    ? (compareValue / base - 1) * 100 : NaN;
        var color = !isFinite(pct) || Math.abs(pct) < 0.0000001 ? '#000000' : (pct > 0 ? '#e5484d' : '#3b6fd4');
        var pctText = isFinite(pct) ? (pct > 0 ? '+' : '') + pct.toFixed(2) + '%' : '—';
        return '<span style="color:'+color+'">'+label+' '+f2(value)+' ('+pctText+')</span>';
      }
      var prevRawIndex = D > 0 ? R[D-1] : NaN;
      row2 = '<span style="color:#000000">시가 '+pxs(OPEN[D])+'</span> · ' +
       '<span style="color:#000000">저가 '+pxs(LOW[D])+'</span> · ' +
       '<span style="color:#000000">종가 '+pxs(S[0][D])+'</span> · ' +
       coloredValue(IDXNAME, R[D], prevRawIndex);
    }
    var up=d>band, dn=d<-band;
    sm.textContent=IDXNAME+' 대비 '+(d>=0?'+':'')+d.toFixed(1)+unit+(up?'강함':(dn?'약함':'보합'));
    sm.style.color=up?'#e5484d':(dn?'#3b6fd4':'#666');
    if(COMPARE_MODE==='daily'){
      dt.textContent=row2;
    } else {
      dt.innerHTML=row2;
    }
    ig.textContent='● '+cats[D]+'  '+IDXNAME+' '+f2(R[D]);
  }
  var tg=null;
  var dbg=mk('left:4px;top:66px;font:11px sans-serif;color:#c00;background:rgba(255,255,255,.92);padding:2px 4px;max-width:92%;z-index:70;');
  function showErr(e){dbg.style.display='block'; dbg.textContent='차트 오류: '+(e&&e.message?e.message:e);}
  function hide(){[vl,hl,dl,pr].forEach(function(e){e.style.display='none';}); if(focus!==N-1){focus=N-1; updateSummary();}}

  function fmt(c){return c.slice(5).replace('-','.');}

  // 보이는 구간의 최고/최저에 맞춰 가격 범위를 즉시 변경 + 지수선을 맨 왼쪽 종가에 맞춰 환산
  function draw(){
    var a=Math.max(0,Math.floor(i0)), b=Math.min(N-1,Math.ceil(i0+span)-1), i, k, v;
    var lo=Infinity, hi=-Infinity, bl=0, bh=0;
    for(k=0;k<S.length;k++){for(i=a;i<=b;i++){v=S[k][i]; if(v<lo)lo=v; if(v>hi)hi=v;}}
    if(IDX&&idxOn){for(i=a;i<=b;i++){v=IDX[i]; if(v<lo)lo=v; if(v>hi)hi=v;}}
    if(HIGH&&LOW){for(i=a;i<=b;i++){if(HIGH[i]>hi)hi=HIGH[i]; if(LOW[i]<lo)lo=LOW[i];}}   // 캔들 고가·저가
    for(i=a;i<=b;i++){v=B[i]; if(v<bl)bl=v; if(v>bh)bh=v;}
    if(!isFinite(lo)||!isFinite(hi)||!isFinite(bl)||!isFinite(bh))throw new Error('가격 범위 계산 오류');
    var pad=(hi-lo)*0.06||1;
    var step=Math.max(3,Math.round(span/4)), tv=[], tt=[], idx;
    for(idx=N-1;idx>=a;idx-=step){tv.push(cats[idx]); tt.push(fmt(cats[idx]));}
    var rp=Math.max(RPAD_MIN,Math.round(span*RPAD_RATIO));   // 오늘 캔들이 오른쪽 끝에 붙지 않게 빈 칸 추가
    var xr=[i0-0.5,i0+span-0.5+rp];
    var lay={'xaxis.range':xr,'xaxis2.range':xr,
      'yaxis.range':[lo-pad,hi+pad],'yaxis2.range':[bl*1.1,(bh*1.1)||1],
      'xaxis.tickvals':tv,'xaxis.ticktext':tt,'xaxis2.tickvals':tv,'xaxis2.ticktext':tt};
    var p=Plotly.relayout(gd,lay);
    if(p&&p.then)p.then(null,showErr);
  }
  function render(){
    pending=false;
    try{ draw(); }
    catch(e){                                // 지수 부분에서 문제가 생겨도 차트는 계속 그리도록 지수 기능만 끔
      showErr(e);
      if(IDX){
        IDX=null; sm.style.display='none'; dt.style.display='none'; ig.style.display='none'; if(tg)tg.style.display='none';
        try{ Plotly.restyle(gd,{visible:[false]},[ITa]); }catch(e1){}
        try{ draw(); }catch(e2){ showErr(e2); }
      }
    }
  }
  function schedule(){if(!pending){pending=true;requestAnimationFrame(render);}}

  function move(cx,cy){
    var r=gd.getBoundingClientRect(), x=cx-r.left, y=cy-r.top;
    var fl=gd._fullLayout, s=fl._size, xa=fl.xaxis, ya=fl.yaxis, yb=fl.yaxis2;
    if(x<s.l||x>s.l+s.w||y<s.t||y>s.t+s.h){hide();return;}
    var lo=xa.range[0], hi=xa.range[1];
    var idx=Math.round((x-xa._offset)/xa._length*(hi-lo)+lo);
    idx=Math.max(0,Math.min(N-1,idx));
    focus=idx; updateSummary();
    var xs=xa._offset+(idx-lo)/(hi-lo)*xa._length;
    if(!isFinite(xs))return;
    var ax=(y>=yb._offset)?yb:ya;
    var v=ax.range[1]-(y-ax._offset)/ax._length*(ax.range[1]-ax.range[0]);
    var txt=Number(v).toLocaleString('en-US',{minimumFractionDigits:DEC,maximumFractionDigits:DEC});
    vl.style.cssText+=';display:block;left:'+xs+'px;top:'+s.t+'px;height:'+s.h+'px;';
    hl.style.cssText+=';display:block;left:'+s.l+'px;top:'+y+'px;width:'+s.w+'px;';
    var showI=!!(IDX&&IDXRAW&&idxOn);
    // 예) 2026-10-20 (줄바꿈) (11,700 , 5,600.00)  → 날짜 / (그날 종목 가격 , 그날 지수)
    dl.textContent=cats[idx]+'\\n('+pxs(S[0][idx])+(showI?' , '+f2(IDXRAW[idx]):'')+')';
    dl.style.display='block'; dl.style.top=(ya._offset+ya._length+2)+'px';
    var half=(dl.offsetWidth||0)/2, tot=fl.width||gd.clientWidth||0;
    dl.style.left=(tot?Math.max(half,Math.min(tot-half,xs)):xs)+'px';        // 화면 밖으로 나가지 않게
    pr.style.display='block'; pr.style.left=(s.l+s.w+2)+'px'; pr.style.top=y+'px'; pr.textContent=txt;
  }
  function pxToBars(dx){return dx/gd._fullLayout._size.w*span;}
  function fracOf(clientX){
    var r=gd.getBoundingClientRect(), s=gd._fullLayout._size;
    return Math.max(0,Math.min(1,(clientX-r.left-s.l)/s.w));
  }
  function dist(t){var dx=t[0].clientX-t[1].clientX, dy=t[0].clientY-t[1].clientY; return Math.sqrt(dx*dx+dy*dy);}
  function resetView(){                       // 더블클릭/더블탭: 처음 상태로
    span=clampSpan(initSpan); i0=N-span; pinch=null; if(mode==='cross')mode=null; hide(); schedule();
  }
  function zoomBy(factor){                    // 화면 가운데 기준 확대/축소
    var c=i0+span/2, ns=clampSpan(span*factor);
    if(ns===span)ns=clampSpan(span+(factor<1?-1:1));
    span=ns; i0=clampI0(c-ns/2,ns); hide(); schedule();
  }

  // ── 확대/축소 버튼 (두 손가락 확대가 막히는 폰용) ──
  function btn(txt,fn,w,fs){
    var b=HD.createElement('div'); b.textContent=txt;
    b.style.cssText=(w?'padding:0 10px;':'width:32px;')+'height:28px;line-height:28px;text-align:center;white-space:nowrap;'
      +'font:600 '+(fs||16)+'px sans-serif;background:#f3f3f3;border:1px solid #ccc;border-radius:5px;cursor:pointer;'
      +'touch-action:manipulation;user-select:none;-webkit-user-select:none;';
    ['touchstart','touchmove','touchend','mousedown','mousemove','dblclick'].forEach(function(n){
      b.addEventListener(n,function(e){e.stopPropagation();},{passive:true});});
    b.addEventListener('click',function(e){e.stopPropagation();fn();});
    bcol.appendChild(b);
    return b;
  }
  if(IDX){                                  // 비교선 켜기/끄기 버튼 (버튼 글자 = 비교 코인 이름)
    tg=btn(IDXNAME,function(){idxOn=!idxOn; paintTg(); try{Plotly.restyle(gd,{visible:[idxOn]},[ITa]);}catch(e){showErr(e);} schedule();},1,14);
    var paintTg=function(){tg.style.color=idxOn?'#007a3d':'#999'; tg.style.background=idxOn?'#e8f8ef':'#f3f3f3'; tg.style.borderColor=idxOn?'#00a651':'#ccc';};
    paintTg();
  }
  btn('＋',function(){zoomBy(0.6);});
  btn('－',function(){zoomBy(1/0.6);});

  // ── 손가락 ──
  // 바로 좌우로 밀기=기간 이동(한 칸씩 딱딱하게) / 길게 눌러 진동=십자선 / 두 손가락=확대·축소
  gd.addEventListener('touchstart',function(e){
    clearTimeout(timer); lastTouch=Date.now();
    if(e.touches.length>=2){
      hide(); mode='pinch';
      pinch={d:dist(e.touches),span:span,anchor:i0+fracOf((e.touches[0].clientX+e.touches[1].clientX)/2)*span};
      return;
    }
    hide();
    var t=e.touches[0]; lastT=t; sx=t.clientX; sy=t.clientY; startI0=i0; mode=null; tStart=Date.now();
    timer=setTimeout(function(){
      if(mode!==null)return;
      mode='arming'; if(navigator.vibrate)navigator.vibrate(40);       // ① 진동
      timer=setTimeout(function(){                                      // ② 잠깐 뒤 십자선 표시 후 따라다님
        if(mode==='arming'){mode='cross'; move(lastT.clientX,lastT.clientY);}
      },CROSS_DELAY_MS);
    },LP_MS);
  },{passive:true});
  gd.addEventListener('touchmove',function(e){
    if(mode==='pinch'){
      if(e.touches.length===2&&pinch){
        e.preventDefault();
        var f=fracOf((e.touches[0].clientX+e.touches[1].clientX)/2);
        var ns=clampSpan(pinch.span*Math.pow(pinch.d/dist(e.touches),ZOOM_GAIN));
        span=ns; i0=clampI0(pinch.anchor-f*ns,ns); schedule();
      }
      return;
    }
    if(e.touches.length!==1)return;
    var t=e.touches[0]; lastT=t;
    if(mode==='arming'){e.preventDefault(); return;}
    if(mode==='cross'){e.preventDefault(); move(t.clientX,t.clientY); return;}
    if(mode==='pan'){e.preventDefault(); i0=clampI0(startI0-pxToBars(t.clientX-sx)*PAN_GAIN,span); schedule(); return;}
    if(mode==='scroll')return;
    var dx=t.clientX-sx, dy=t.clientY-sy;
    if(Math.abs(dx)>6||Math.abs(dy)>6){
      clearTimeout(timer);
      if(Math.abs(dx)>Math.abs(dy)){mode='pan'; e.preventDefault(); i0=clampI0(startI0-pxToBars(dx)*PAN_GAIN,span); schedule();}
      else mode='scroll';
    }
  },{passive:false});
  function touchEnd(e){
    clearTimeout(timer); lastTouch=Date.now();
    if(e.touches&&e.touches.length>0)return;      // 아직 손가락이 남아 있음
    pinch=null;
    if(mode===null&&e.type==='touchend'&&Date.now()-tStart<300){   // 짧게 톡 → 두 번이면 기본값
      var now=Date.now();
      if(now-lastTap<350){resetView(); lastTap=0;} else lastTap=now;
    }
    if(mode!=='cross')mode=null;                   // 십자선은 손을 떼도 남고, 다음 터치 때 사라짐
  }
  gd.addEventListener('touchend',touchEnd);
  gd.addEventListener('touchcancel',touchEnd);
  gd.addEventListener('contextmenu',function(e){e.preventDefault();});

  // ── PC: 마우스를 누른 채 끌면 이동, 그냥 움직이면 십자선, Ctrl+휠=확대/축소 ──
  function emu(e){return (Date.now()-lastTouch<1000)||(e.sourceCapabilities&&e.sourceCapabilities.firesTouchEvents);}
  gd.addEventListener('mousedown',function(e){if(emu(e))return; mdown=true; sx=e.clientX; startI0=i0; e.preventDefault();});
  window.addEventListener('mousemove',function(e){
    if(!mdown)return;
    i0=clampI0(startI0-pxToBars(e.clientX-sx),span); schedule();
  },true);
  window.addEventListener('mouseup',function(){mdown=false;},true);
  gd.addEventListener('mousemove',function(e){if(emu(e))return; move(e.clientX,e.clientY);});
  gd.addEventListener('mouseleave',function(e){if(emu(e))return; hide();});
  gd.addEventListener('dblclick',function(e){resetView();});
  gd.addEventListener('wheel',function(e){
    if(!e.ctrlKey)return; e.preventDefault();
    var f=fracOf(e.clientX), anchor=i0+f*span;
    var ns=clampSpan(span*(e.deltaY>0?1.23:0.81));
    if(ns===span)ns=clampSpan(span+(e.deltaY>0?1:-1));
    span=ns; i0=clampI0(anchor-f*ns,ns); schedule();
  },{passive:false});
  updateSummary();
  render();
}
"""


def finish(fig, low_label):
    m = len(dates)
    pos = sorted({m - 1 - int(round(i * (m - 1) / 3.6)) for i in range(4)})
    fig.update_xaxes(type="category", tickmode="array",
                     tickvals=[dates[i] for i in pos],
                     ticktext=[pd.to_datetime(dates[i]).strftime("%m.%d") for i in pos],
                     tickangle=0, tickfont=dict(size=11, color="#555"),
                     showgrid=True, gridcolor="#f0b8b8", gridwidth=1,
                     showline=True, mirror=True, linecolor="#9aa3ae", linewidth=1, zeroline=False)   # 차트 영역 테두리
    fig.update_xaxes(showticklabels=True, row=1, col=1)    # 날짜는 위 차트 아래(두 차트 사이)에 표시
    fig.update_xaxes(showticklabels=False, row=2, col=1)
    fig.update_yaxes(side="right", tickformat=f",.{DEC}f", tickfont=dict(size=11, color="#555"),
                     showgrid=True, gridcolor="#eeeeee", zeroline=False, ticks="",
                     showline=True, mirror=True, linecolor="#9aa3ae", linewidth=1)
    fig.update_yaxes(nticks=4, tickformat=f",.{min(DEC + 1, 6)}f", row=2, col=1)
    tagc = "#e5484d" if chg > 0 else "#3b6fd4"
    # 현재가 태그(오른쪽 축)
    fig.add_annotation(xref="paper", x=1.0, xanchor="left", yref="y", y=yv[-1], showarrow=False,
                       text=f"{px(last, DEC)}<br>{pct:+.2f}%", align="left", borderpad=2,
                       font=dict(color="white", size=11), bgcolor=tagc)
    # 아래 차트 이름
    fig.add_annotation(xref="paper", yref="paper", x=0.01, y=fig.layout.yaxis2.domain[1],
                       xanchor="left", yanchor="top", showarrow=False,
                       text=low_label, font=dict(size=11, color="#555"))
    fig.update_layout(height=620, margin=dict(l=4, r=max(66, 12 + 7 * len(px(last, DEC))), t=26, b=22),
                      showlegend=False, bargap=0.05, hovermode=False, dragmode=False,
                      xaxis_rangeslider_visible=False, xaxis2_rangeslider_visible=False,
                      plot_bgcolor="white", paper_bgcolor="white")
    return fig


def show(fig, div_id, bars):
    """좌우 이동 + 길게 눌러 십자선 기능을 얹어 차트를 표시"""
    def pack(v):
        return np.round(np.asarray(v, dtype=float), DEC + 3).tolist()
    series = [pack(v) for v in (yv, curvev, upv, lov)]
    dump = lambda o: json.dumps(o, separators=(",", ":"))
    idx_json = dump(pack(idx_scaled)) if idx_scaled is not None else "null"
    raw_json = dump(pack(idx_arr)) if idx_arr is not None else "null"
    open_json = dump(pack(ov))
    low_json = dump(pack(lv))
    idx2_json = dump(pack(idx_scaled_p)) if idx_scaled_p is not None else "null"
    post = "installCrosshair(document.getElementById('{plot_id}'), %d, %s, %s, %s, %d, %s, %s, %s, %s, %d, %s, %d, %s);" % (
        init_span, dump(series), dump(pack(bars)),
        idx_json, (4 if idx_scaled is not None else -1), json.dumps(idx_name),
        raw_json, open_json, low_json, n - init_span, idx2_json, 5, dump(pack(hv)))
    dec_i = dec_of(idx_arr[-1]) if idx_arr is not None else 0
    html = f"<script>var DEC={DEC}, DECI={dec_i};" + CROSSHAIR_JS + "</script>" + fig.to_html(
        full_html=False, include_plotlyjs="cdn", div_id=div_id,
        config={"staticPlot": True, "responsive": True},
        post_script=post)
    components.html(html, height=640)


t_calc = time.time() - _tc
_td = time.time()
fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.06)
base_traces(fig)
cv = curvev - curvev.min()
fig.add_trace(go.Bar(x=dates, y=cv, marker_color=direction_colors(curvev),
                     name="곡선추세 방향", showlegend=False), row=2, col=1)
show(finish(fig, "곡선추세 방향"), "chart_trend", cv)
status.empty()
top_ph.markdown(top_bar(timing_html([("시세", t_fetch), ("계산", t_calc), ("차트", time.time() - _td)])),
                unsafe_allow_html=True)
if idx_arr is None:
    st.caption(f"※ {idx_name} 시세를 불러오지 못해 비교선 없이 표시 중입니다.")
elif idx_note:
    st.caption(idx_note)
