"""업비트 자동매매 엔진 (코인002)

화면(app.py)과 따로 떨어진 '두뇌' 부분입니다. streamlit 없이도 동작합니다.
매매 기준은 차트의 세 선입니다 (app.py 차트와 똑같은 계산):
  · 자동매수     : 매수가능선(곡선하한)에서 매수
  · 중간자동매도 : 추세가 하락 중일 때 중간값(곡선)에서 절반 매도 (상승 중이면 건너뜀)
  · 자동매도     : 매도가능선(곡선상한)에서 남은 것 전부 매도
코인마다 [자동매도] [중간자동매도] [자동매수] 버튼을 켜 둔 것만 동작합니다.
- 모의 모드: 실제 주문 없이 '체결됐다고 치고' 기록만 남김 (처음엔 꼭 모의로 확인!)
- 설정과 진행 상태는 trade_state.json 에 저장 (다시 켜면 안전을 위해 '자동매매 꺼짐'으로 시작)
"""
import base64
import csv
import hashlib
import hmac
import json
import threading
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from urllib.parse import urlencode, unquote

import requests

API = "https://api.upbit.com"
MIN_ORDER_KRW = 5000          # 업비트 원화마켓 최소 주문 금액
SLIP_TICKS = 2                # 주문 가격: 매수는 매도 1호가보다 2호가 높게, 매도는 매수 1호가보다 2호가 낮게 (+FOK)
FEE = 0.0005                  # 업비트 기본 수수료 0.05% (예수금 확인할 때 여유분으로 사용)
INTERVAL = 3                  # 몇 초마다 현재가를 확인할지
LOG_KEEP = 300                # 화면용 기록은 최근 300개만 보관 (전체 기록은 trade_log.csv)

# 상태 이름
WAIT, BUYING, HOLD, SELLING, DONE, ERROR = "대기", "매수주문", "보유", "매도주문", "완료", "오류"
PENDING = (BUYING, SELLING)


class UpbitError(Exception):
    pass


# ───────────── 인증 (JWT, HS256) ─────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(access: str, secret: str, params: dict | None = None) -> str:
    """업비트 인증 토큰. 주문처럼 값(params)이 있으면 그 값의 SHA512 해시를 함께 넣어야 함"""
    payload = {"access_key": access, "nonce": str(uuid.uuid4())}
    if params:
        q = unquote(urlencode(params, doseq=True))
        payload["query_hash"] = hashlib.sha512(q.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


class Upbit:
    """업비트 개인 API (잔고·주문). 키는 화면에 절대 보이지 않게 secrets에서만 받음"""

    def __init__(self, access: str, secret: str):
        self.access, self.secret = access.strip(), secret.strip()

    def _req(self, method: str, path: str, params: dict | None = None, tries: int = 4):
        for i in range(tries):
            headers = {"Authorization": "Bearer " + make_jwt(self.access, self.secret, params),
                       "Accept": "application/json"}
            if method == "POST":
                r = requests.post(API + path, json=params, headers=headers, timeout=8)
            else:
                r = requests.request(method, API + path, params=params, headers=headers, timeout=8)
            if r.status_code == 429:                       # 너무 자주 부름 → 잠깐 쉬고 다시
                time.sleep(0.3 * (i + 1))
                continue
            if r.status_code >= 400:
                try:
                    e = r.json().get("error", {})
                    msg = f"{e.get('name', '')}: {e.get('message', '')}".strip(": ")
                except Exception:
                    msg = r.text[:200]
                raise UpbitError(f"[{r.status_code}] {msg}")
            return r.json()
        raise UpbitError("요청이 너무 많아 잠시 막혔습니다")

    def accounts(self) -> dict:
        """{'KRW': {'balance': 사용가능, 'locked': 주문중, 'avg': 평균단가}, 'BTC': {...}}"""
        out = {}
        for a in self._req("GET", "/v1/accounts"):
            out[a["currency"]] = {"balance": float(a.get("balance") or 0),
                                  "locked": float(a.get("locked") or 0),
                                  "avg": float(a.get("avg_buy_price") or 0)}
        return out

    def limit_order(self, market: str, side: str, volume: Decimal, price: Decimal, tif: str | None = "fok") -> dict:
        """지정가 주문. side = 'bid'(매수) / 'ask'(매도)
        tif='fok' (Fill or Kill): 그 가격 안에서 '전부' 체결될 수 있을 때만 체결, 아니면 하나도 체결하지 않고 바로 취소
        → 일부만 체결돼 5,000원 미만 자투리가 남거나, 주문이 업비트에 걸린 채 남는 일이 없음"""
        body = {"market": market, "side": side, "ord_type": "limit",
                "volume": fmt_dec(volume), "price": fmt_dec(price)}
        if tif:
            body["time_in_force"] = tif
        return self._req("POST", "/v1/orders", body)

    def get_order(self, uid: str) -> dict:
        return self._req("GET", "/v1/order", {"uuid": uid})

    def cancel(self, uid: str) -> dict:
        return self._req("DELETE", "/v1/order", {"uuid": uid})


# ───────────── 시세 (공개) ─────────────
def quotes(market: str) -> tuple:
    """지금 호가: (매도 1호가, 매수 1호가) — 매수는 매도 1호가에, 매도는 매수 1호가에 사고팔 수 있음"""
    r = requests.get(API + "/v1/orderbook", params={"markets": market},
                     headers={"Accept": "application/json"}, timeout=8)
    r.raise_for_status()
    u = r.json()[0]["orderbook_units"][0]
    return float(u["ask_price"]), float(u["bid_price"])


def prices(markets: list) -> dict:
    """{KRW-BTC: 현재가}"""
    if not markets:
        return {}
    r = requests.get(API + "/v1/ticker", params={"markets": ",".join(markets)},
                     headers={"Accept": "application/json"}, timeout=8)
    r.raise_for_status()
    return {it["market"]: float(it["trade_price"]) for it in r.json()}


# ───────────── 호가 단위 (주문 가격은 이 단위에 맞아야 함) ─────────────
TICKS = [(2_000_000, "1000"), (1_000_000, "500"), (500_000, "100"), (100_000, "50"), (10_000, "10"),
         (1_000, "1"), (100, "0.1"), (10, "0.01"), (1, "0.001"), (0.1, "0.0001"), (0.01, "0.00001"),
         (0.001, "0.000001"), (0.0001, "0.0000001"), (0, "0.00000001")]
_inst_cache: dict = {}        # {마켓: (받은 시각, 현재가 구간의 호가 단위)}


def _table_tick(p: float) -> Decimal:
    for lim, t in TICKS:
        if p >= lim:
            return Decimal(t)
    return Decimal(TICKS[-1][1])


def _band(p: float) -> int:
    for i, (lim, _) in enumerate(TICKS):
        if p >= lim:
            return i
    return len(TICKS) - 1


def tick_size(market: str, price: float, now_price: float | None = None) -> Decimal:
    """표로 구한 호가 단위. 목표가가 현재가와 같은 구간이면 업비트 '호가 정책 조회'의 값을 우선 사용
    (100~1,000원 구간처럼 코인마다 단위가 다른 경우가 있어서)"""
    t = _table_tick(price)
    if now_price is None or _band(now_price) != _band(price):
        return t
    ent = _inst_cache.get(market)
    if not ent or time.time() - ent[0] > 3600:
        try:
            r = requests.get(API + "/v1/orderbook/instruments", params={"markets": market}, timeout=5)
            r.raise_for_status()
            ent = (time.time(), Decimal(str(r.json()[0]["tick_size"])))
            _inst_cache[market] = ent
        except Exception:
            return t
    return ent[1]


def fit_price(market: str, price: float, side: str, now_price: float | None = None) -> Decimal:
    """매수가는 내림, 매도가는 올림해서 호가 단위에 맞춤 (원래 생각한 값보다 불리해지지 않게)"""
    t = tick_size(market, price, now_price)
    q = (Decimal(str(price)) / t).to_integral_value(ROUND_DOWN if side == "bid" else ROUND_UP)
    return (q * t).normalize()


def fit_volume(v: float) -> Decimal:
    """수량은 소수 8자리까지 (내림)"""
    return Decimal(str(v)).quantize(Decimal("0.00000001"), ROUND_DOWN)


def buy_volume(krw: float, price: Decimal) -> Decimal:
    """'krw원어치' 살 수량. 내림하면 5,000원이 4,999.99원이 되어 최소 주문 금액에 걸리므로 올림 (차이는 1원 미만)"""
    return (Decimal(str(krw)) / price).quantize(Decimal("0.00000001"), ROUND_UP)


def order_price(market: str, side: str, ask: float, bid: float, now_price: float | None = None) -> Decimal:
    """바로 체결되도록 부르는 가격. 매수 = 매도 1호가 + 2호가, 매도 = 매수 1호가 - 2호가 (호가 단위에 맞춤)"""
    base = ask if side == "bid" else bid
    t = tick_size(market, base, now_price)
    d = Decimal(str(base)) + (t * SLIP_TICKS if side == "bid" else -t * SLIP_TICKS)
    if d <= 0:
        d = t
    q = (d / t).to_integral_value(ROUND_DOWN if side == "bid" else ROUND_UP)   # 호가 단위에 맞춤
    return (q * t).normalize()


def _avg_price(o: dict):
    """주문의 실제 평균 체결 가격 (체결 금액 ÷ 체결 수량). 모르면 주문 가격"""
    ex, funds = _f(o.get("executed_volume")), _f(o.get("executed_funds"))
    if ex and funds:
        return funds / ex
    return _f(o.get("price"))


def fmt_dec(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _f(v):
    """빈칸/0/글자 → None, 숫자 → float"""
    try:
        f = float(str(v).replace(",", ""))
        return f if f > 0 else None
    except Exception:
        return None


# ───────────── 차트 곡선 계산 (app.py 차트와 똑같은 계산) ─────────────
import numpy as np

LAM, KMUL, BAND_PCT = 8e3, 1.6, 0.0     # app.py 의 lam, kmul, band_pct 와 같은 값
CALC_DAYS = 720                        # 곡선 계산에 쓰는 일봉 개수 (app.py 의 calc)
HIST_TTL = 600                         # 일봉은 10분마다 새로 받음 (그 사이에는 마지막 값만 현재가로 바꿈)
_hp_cache: dict = {}
_hist_cache: dict = {}


def _hp_inv(n: int, lam: float) -> np.ndarray:
    key = (n, float(lam))
    if key not in _hp_cache:
        D = np.zeros((n - 2, n))
        for i in range(n - 2):
            D[i, i:i + 3] = [1, -2, 1]
        if len(_hp_cache) > 8:
            _hp_cache.clear()
        _hp_cache[key] = np.linalg.inv(np.eye(n) + lam * D.T @ D)
    return _hp_cache[key]


def calc_bands(y, lam=LAM, kmul=KMUL, band_pct=BAND_PCT):
    """곡선(중간선), 곡선상한(매도선), 곡선하한(매수선)"""
    y = np.asarray(y, dtype=float)
    t = np.arange(len(y))
    b1, b0 = np.polyfit(t, y, 1)                       # 직선추세
    line = b0 + b1 * t
    curve = line + _hp_inv(len(y), lam) @ (y - line)   # 곡선추세 (HP 필터)
    width = band_pct / 100 if band_pct > 0 else kmul * (y / curve - 1).std()
    return curve, curve * (1 + width), curve * (1 - width)


def bands_now(y) -> dict:
    """오늘 기준 {'low': 하한, 'mid': 곡선, 'up': 상한, 'falling': 곡선이 내려가는 중인지}"""
    curve, up, lo = calc_bands(y)
    return {"low": float(lo[-1]), "mid": float(curve[-1]), "up": float(up[-1]),
            "falling": bool(curve[-1] < curve[-2])}


def closes(market: str, days: int = CALC_DAYS) -> np.ndarray:
    """업비트 일봉 종가 (오래된 것 → 최근). 10분 동안 저장"""
    ent = _hist_cache.get(market)
    if ent and time.time() - ent[0] < HIST_TTL:
        return ent[1].copy()
    got, to = [], None
    while len(got) < days:
        p = {"market": market, "count": min(200, days - len(got))}
        if to:
            p["to"] = to
        r = requests.get(API + "/v1/candles/days", params=p, headers={"Accept": "application/json"}, timeout=8)
        if r.status_code == 429:
            time.sleep(0.4)
            continue
        r.raise_for_status()
        part = r.json()
        if not part:
            break
        got += part
        to = part[-1]["candle_date_time_utc"]
        if len(part) < p["count"]:
            break
    seen, rows = set(), []
    for c in got:
        d = c["candle_date_time_kst"][:10]
        if d not in seen and c.get("trade_price"):
            seen.add(d)
            rows.append((d, float(c["trade_price"])))
    rows.sort()
    y = np.array([v for _, v in rows][-days:], dtype=float)
    _hist_cache[market] = (time.time(), y)
    return y.copy()


# ───────────── 자동매매 엔진 ─────────────
# 버튼 3개 (코인마다)
#   곡선하한(low) 실행 → 현재가가 곡선하한 이하로 내려오면 '매수 금액'만큼 매수
#   곡선(mid)    실행 → 곡선이 '하락' 중일 때, 현재가가 곡선에 닿으면 가진 것의 절반 매도
#   곡선상한(up)  실행 → 현재가가 곡선상한에 닿으면 남은 것 전부 매도
# 곡선이 '상승' 중일 때는 곡선(mid)에서 팔지 않고 상한까지 들고 감
BTN = ("low", "mid", "up")
BTN_NAME = {"low": "자동매수", "mid": "중간자동매도", "up": "자동매도"}
WAIT, BUYING, HOLD, SELLING, DONE, ERROR = "대기", "매수주문", "보유", "매도주문", "완료", "오류"
PENDING = (BUYING, SELLING)


class Engine:
    def __init__(self, folder: Path, client: Upbit | None = None, price_fn=prices, closes_fn=closes, quote_fn=quotes):
        self.quote_fn = quote_fn
        self.folder = Path(folder)
        self.path = self.folder / "trade_state.json"
        self.csv = self.folder / "trade_log.csv"
        self.client = client
        self.price_fn = price_fn
        self.closes_fn = closes_fn
        self.lock = threading.RLock()
        self.last_prices: dict = {}
        self.last_tick = 0.0
        self.last_error = ""
        self.data = {"enabled": False, "mock": True, "buy_krw": None, "rules": {}, "log": []}
        if self.path.exists():
            try:
                old = json.loads(self.path.read_text(encoding="utf-8"))
                if all("low" in r for r in old.get("rules", {}).values()):   # 예전(가격 입력) 방식 파일은 버림
                    self.data.update(old)
            except Exception:
                pass
        if self.client is None:
            self.data["mock"] = True
        self.data["enabled"] = False          # 안전: 프로그램을 다시 켜면 항상 '꺼짐'으로 시작
        self._save()
        self._thread = None

    # ── 저장·기록 ──
    def _save(self):
        """설정·상태를 파일에 저장. Windows에서는 백신·검색 프로그램이 파일을 잠깐 잡고 있으면
        '액세스가 거부되었습니다'가 날 수 있어서, 몇 번 다시 시도하고 그래도 안 되면 바로 덮어씀.
        저장이 안 돼도 프로그램은 멈추지 않음 (다음 저장 때 다시 시도)"""
        text = json.dumps(self.data, ensure_ascii=False, indent=1)
        tmp = self.path.with_suffix(".tmp")
        for i in range(6):
            try:
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self.path)
                return
            except OSError:
                time.sleep(0.05 * (i + 1))
        try:
            self.path.write_text(text, encoding="utf-8")
        except OSError as e:
            self.last_error = f"{time.strftime('%H:%M:%S')} 설정 파일 저장 실패: {e}"

    def _log(self, r: dict, action: str, price=None, volume=None, msg: str = ""):
        row = {"시각": time.strftime("%m-%d %H:%M:%S"), "코인": r.get("name") or r["market"],
               "마켓": r["market"], "내용": action, "가격": price, "수량": volume,
               "모드": "모의" if self.data["mock"] else "실전", "메모": msg}
        self.data["log"] = ([row] + self.data["log"])[:LOG_KEEP]
        new = not self.csv.exists()
        try:
            with self.csv.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(row))
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            pass

    def _rule(self, market: str, name: str = "") -> dict:
        r = self.data["rules"].get(market)
        if r is None:
            r = {"market": market, "name": name or market[4:], "krw": None, "low": False, "mid": False, "up": False,
                 "state": WAIT, "held": 0.0, "half_done": False, "order": None, "kind": None, "msg": "", "bands": None}
            self.data["rules"][market] = r
        if name:
            r["name"] = name
        return r

    # ── 화면에서 쓰는 함수 ──
    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def has_pending(self) -> bool:
        with self.lock:
            return any(r.get("state") in PENDING for r in self.data["rules"].values())

    def set_enabled(self, on: bool):
        with self.lock:
            self.data["enabled"] = bool(on)
            self._save()
        if on:
            self.start()

    def set_mock(self, mock: bool) -> str:
        with self.lock:
            if not mock and self.client is None:
                return "API 키가 없어 실전 모드로 바꿀 수 없습니다."
            if bool(mock) != bool(self.data["mock"]):
                if self.has_pending():
                    return "주문이 걸려 있는 코인이 있어 모드를 바꿀 수 없습니다. 버튼을 취소하거나 체결을 기다려 주세요."
                for r in self.data["rules"].values():          # 모의로 '산 셈 친' 것을 실전으로 넘기지 않음
                    r.update(state=WAIT, held=0.0, half_done=False, order=None, kind=None, msg="모드 변경으로 초기화")
                self.data["mock"] = bool(mock)
                self.data["enabled"] = False
                self._save()
            return ""

    def buy_krw(self, r: dict | None = None) -> float:
        """자동매수 1회 금액: 코인에 따로 정한 금액이 있으면 그것, 없으면 공통 금액"""
        return (r or {}).get("krw") or self.data.get("buy_krw") or 0.0

    def set_buy_krw(self, krw) -> str:
        """모든 코인 공통 '자동매수 1회 금액(원)'"""
        with self.lock:
            self.data["buy_krw"] = _f(krw)
            note = ""
            if not self.data["buy_krw"] or self.data["buy_krw"] < MIN_ORDER_KRW:
                off = [r for r in self.data["rules"].values() if r.get("low") and not r.get("krw")
                       and r.get("state") not in PENDING]
                for r in off:
                    r["low"] = False
                if off:
                    note = f"자동매수 금액이 {MIN_ORDER_KRW:,}원보다 작아 자동매수를 취소했습니다: " + ", ".join(r["name"] for r in off)
            self._save()
            return note

    def toggle(self, market: str, key: str, name: str = "") -> str:
        """버튼 한 번 누름: 실행 ↔ 취소. 돌려주는 값 = 알림 문장(없으면 빈칸)"""
        with self.lock:
            r = self._rule(market, name)
            on = not r.get(key)
            if on and key == "low" and self.buy_krw(r) < MIN_ORDER_KRW:
                return f"먼저 아래 [자동매매] 칸에서 '자동매수 1회 금액'을 {MIN_ORDER_KRW:,}원 이상 넣어 주세요."
            r[key] = on
            if key == "low" and not on:
                r["short"] = False
            note = ""
            if not on and r.get("state") in PENDING:          # 그 버튼으로 걸린 주문이면 취소
                mine = (key == "low" and r["state"] == BUYING) or (r["state"] == SELLING and r.get("kind") == key)
                if mine:
                    self._cancel(r, f"{BTN_NAME[key]} 취소 버튼으로 주문 취소")
                    note = f"{r['name']}: 걸려 있던 주문을 취소했습니다."
            if on and r.get("state") in (DONE, ERROR):
                r.update(state=WAIT if not r.get("held") else HOLD, msg="")
            self._log(r, f"{BTN_NAME[key]} {'실행' if on else '취소'}")
            self._save()
            return note

    def all_off(self):
        """모든 코인의 자동매도·중간자동매도·자동매수를 끔 (걸려 있던 주문도 취소)"""
        with self.lock:
            for m, r in list(self.data["rules"].items()):
                for k in BTN:
                    if r.get(k):
                        self.toggle(m, k)

    def reset(self, market: str | None = None):
        with self.lock:
            for m, r in self.data["rules"].items():
                if (market is None or m == market) and r.get("state") not in PENDING:
                    r.update(state=WAIT, held=0.0, half_done=False, order=None, kind=None, msg="")
            self._save()

    def planned_krw(self) -> float:
        with self.lock:
            return sum(self.buy_krw(r) for r in self.data["rules"].values()
                       if r.get("low") and r.get("state") == WAIT)

    # ── 백그라운드 실행 ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="auto-trade", daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.step()
                self.last_error = ""
            except Exception as e:
                self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
            time.sleep(INTERVAL)

    def step(self):
        with self.lock:
            if not self.data["enabled"]:
                return
            active = [r for r in self.data["rules"].values()
                      if any(r.get(k) for k in BTN) or r.get("state") in PENDING]
            if not active:
                return
            px = self.price_fn([r["market"] for r in active])
            self.last_prices.update(px)
            self.last_tick = time.time()
            acc = self.client.accounts() if (not self.data["mock"] and self.client is not None) else None
            for r in active:
                p = px.get(r["market"])
                if p is None:
                    continue
                try:
                    y = self.closes_fn(r["market"])
                    if len(y) < 60:
                        r["msg"] = "일봉이 60개보다 적어 추세를 계산할 수 없습니다"
                        continue
                    y[-1] = p                                   # 오늘 캔들 = 지금 가격 (차트와 같음)
                    b = bands_now(y)
                    r["bands"] = b
                    self._advance(r, p, b, acc)
                except UpbitError as e:
                    r.update(state=ERROR, msg=str(e))
                    self._log(r, "오류", p, None, str(e))
                except Exception as e:
                    r["msg"] = f"일시 오류: {e}"
            self._save()

    # ── 한 코인 한 칸 진행 ──
    def _advance(self, r: dict, p: float, b: dict, acc: dict | None):
        mock, m, s = self.data["mock"], r["market"], r.get("state", WAIT)

        if s == WAIT:
            if r.get("low"):
                if p > b["low"]:
                    r["msg"] = ""
                    r["short"] = r["miss"] = False
                    return
                if mock:                                    # 모의: 호가를 못 받으면 매수가능 값으로 계산
                    try:
                        ask, bid = self.quote_fn(m)
                        price = order_price(m, "bid", ask, bid, p)
                    except Exception:
                        price = fit_price(m, b["low"], "bid", p)
                else:
                    ask, bid = self.quote_fn(m)
                    price = order_price(m, "bid", ask, bid, p)     # 매도 1호가 + 2호가
                vol = buy_volume(self.buy_krw(r), price)
                cost = float(price * vol)
                if cost < MIN_ORDER_KRW:
                    raise UpbitError(f"주문 금액 {cost:,.0f}원 < 최소 {MIN_ORDER_KRW:,}원")
                if mock:
                    r.update(state=HOLD, held=float(vol), half_done=False, dust=False, msg=f"자동매수 {fmt_dec(price)}원(모의)")
                    self._log(r, "자동매수 체결(모의)", float(price), float(vol))
                    return
                krw = (acc or {}).get("KRW", {}).get("balance", 0.0)
                need = cost * (1 + FEE)
                if krw < need:
                    # 앞서 주문한 코인들이 예수금을 묶어서 모자람 → 주문을 보내지 않고 알림만 남김 (돈이 생기면 그때 매수)
                    r["msg"] = f"예수금 부족 (필요 {need:,.0f}원 / 있음 {krw:,.0f}원) — 주문 안 함, 기다리는 중"
                    if not r.get("short"):                     # 기록은 처음 한 번만 (3초마다 쌓이지 않게)
                        r["short"] = True
                        self._log(r, "예수금 부족 — 주문 안 함", float(price), float(vol),
                                  f"필요 {need:,.0f}원 / 있음 {krw:,.0f}원")
                    return
                o = self.client.limit_order(m, "bid", vol, price)
                if acc is not None and "KRW" in acc:           # 같은 3초 안의 다음 코인이 이 돈을 또 쓰지 않게 바로 뺌
                    acc["KRW"]["balance"] = krw - need
                r["short"] = False
                r.update(state=BUYING, order=o["uuid"], kind="low", msg=f"자동매수 주문 {fmt_dec(price)}원 (FOK)")
                if not r.get("miss"):                          # 체결 못 하고 다시 시도하는 중이면 기록을 쌓지 않음
                    self._log(r, "자동매수 주문", float(price), float(vol), "매도1호가+2호가 · 전량 체결 아니면 취소(FOK)")
            elif r.get("mid") or r.get("up"):               # 하한 매수 없이 → 이미 가진 코인을 팔기
                if mock:
                    r["msg"] = "모의 모드에서는 자동매수로 산 코인만 팔 수 있어요"
                    return
                bal = (acc or {}).get(m[4:], {}).get("balance", 0.0)
                if bal * p >= MIN_ORDER_KRW:
                    r.update(state=HOLD, held=bal, half_done=False, msg="가진 코인 매도 대기")
                else:
                    r["msg"] = "가진 코인이 없어 팔 것이 없습니다"
            return

        if s == BUYING:
            if mock:
                return
            o = self.client.get_order(r["order"])
            ex = float(o.get("executed_volume") or 0)
            if o["state"] in ("done", "cancel") and ex > 0:
                r.update(state=HOLD, held=ex, half_done=False, order=None, kind=None, miss=False, dust=False,
                         msg=f"매수 체결 {ex:g}개")
                self._log(r, "자동매수 체결", _avg_price(o), ex)
            elif o["state"] in ("done", "cancel"):           # FOK: 그 가격 안에 물량이 모자라 하나도 안 사고 취소됨
                r.update(state=WAIT, order=None, kind=None,
                         msg="호가 물량이 모자라 체결 안 됨(FOK 취소) — 3초 뒤 다시 시도")
                if not r.get("miss"):
                    r["miss"] = True
                    self._log(r, "자동매수 체결 안 됨(FOK 취소)", _f(o.get("price")), 0, "다시 시도합니다")
            else:
                r["msg"] = f"매수 주문 체결 기다리는 중 ({ex:g}개 체결)"
            return

        if s == HOLD:
            held = r.get("held") or 0.0
            if not mock:
                bal = (acc or {}).get(m[4:], {}).get("balance", 0.0)
                held = min(held, bal)
            kind = None
            if r.get("up") and p >= b["up"]:
                kind, target, vol = "up", b["up"], held                       # 전체 매도
            elif r.get("mid") and b["falling"] and not r.get("half_done") and p >= b["mid"]:
                kind, target, vol = "mid", b["mid"], held / 2                 # 절반 매도
            if kind is None:
                r["msg"] = "보유 중 · 추세 " + ("하락" if b["falling"] else "상승")
                r["miss"] = False
                return
            if mock:
                try:
                    ask, bid = self.quote_fn(m)
                    price = order_price(m, "ask", ask, bid, p)
                except Exception:
                    price = fit_price(m, target, "ask", p)
            else:
                ask, bid = self.quote_fn(m)
                price = order_price(m, "ask", ask, bid, p)         # 매수 1호가 - 2호가
            vol = fit_volume(vol)
            if float(price * vol) < MIN_ORDER_KRW:
                if kind == "mid":
                    r.update(half_done=True, msg="절반이 최소 주문 금액보다 작아 중간자동매도는 건너뜀")
                    return
                # 5,000원 미만 자투리: 업비트가 매도 주문을 받지 않음 → 오류로 멈추지 않고 알리기만 함
                r["msg"] = (f"가진 양이 {float(price * vol):,.0f}원어치라 업비트 최소 주문 금액 "
                            f"{MIN_ORDER_KRW:,}원보다 작아서 팔 수 없어요")
                if not r.get("dust"):
                    r["dust"] = True
                    self._log(r, "5,000원 미만이라 매도 불가", float(price), float(vol))
                return
            what = "중간자동매도(절반)" if kind == "mid" else "자동매도(전체)"
            if mock:
                self._log(r, what + " 체결(모의)", float(price), float(vol))
                self._sold(r, kind, float(vol), f"{what} {fmt_dec(price)}원(모의)")
            else:
                o = self.client.limit_order(m, "ask", vol, price)
                r.update(state=SELLING, order=o["uuid"], kind=kind, held=float(vol) if kind == "up" else r.get("held"),
                         msg=f"{what} 주문 {fmt_dec(price)}원 (FOK)")
                if not r.get("miss"):
                    self._log(r, what + " 주문", float(price), float(vol), "매수1호가-2호가 · 전량 체결 아니면 취소(FOK)")
            return

        if s == SELLING:
            if mock:
                return
            o = self.client.get_order(r["order"])
            ex = float(o.get("executed_volume") or 0)
            what = "중간자동매도(절반)" if r.get("kind") == "mid" else "자동매도(전체)"
            if o["state"] in ("done", "cancel") and ex > 0:
                r["miss"] = False
                self._log(r, what + " 체결", _avg_price(o), ex)
                self._sold(r, r.get("kind"), ex, f"{what} 체결 {ex:g}개")
            elif o["state"] in ("done", "cancel"):           # FOK: 물량이 모자라 하나도 안 팔고 취소됨 → 다음 차례에 다시
                r.update(state=HOLD, order=None, kind=None,
                         msg="호가 물량이 모자라 체결 안 됨(FOK 취소) — 3초 뒤 다시 시도")
                if not r.get("miss"):
                    r["miss"] = True
                    self._log(r, what + " 체결 안 됨(FOK 취소)", _f(o.get("price")), 0, "다시 시도합니다")
            else:
                r["msg"] = f"{what} 체결 기다리는 중 ({ex:g}개 체결)"
            return

    def _after_all(self, r: dict) -> str:
        """다 판 뒤: 곡선하한 버튼이 '실행'이면 다시 매수 대기, 아니면 완료"""
        return WAIT if r.get("low") else DONE

    def _sold(self, r: dict, kind: str, vol: float, msg: str):
        left = max((r.get("held") or 0) - vol, 0.0)
        if kind == "mid" and left > 1e-12:
            r.update(state=HOLD, held=left, half_done=True, order=None, kind=None, msg=msg)
        else:
            r.update(state=self._after_all(r), held=0.0, half_done=False, order=None, kind=None,
                     msg=msg + (" → 다시 자동매수 대기" if r.get("low") else ""))

    def _cancel(self, r: dict, why: str):
        if self.data["mock"] or not r.get("order"):
            r.update(state=WAIT if r.get("state") == BUYING else HOLD, order=None, kind=None, msg=why)
            return
        try:
            self.client.cancel(r["order"])
        except UpbitError as e:
            r["msg"] = f"취소 실패: {e}"
        try:
            ex = float(self.client.get_order(r["order"]).get("executed_volume") or 0)
        except Exception:
            ex = 0.0
        if r["state"] == BUYING:
            r.update(state=HOLD if ex > 0 else WAIT, held=ex, half_done=False, order=None, kind=None, msg=why)
        else:
            left = max((r.get("held") or 0) - ex, 0.0)
            r.update(state=HOLD if left > 0 else self._after_all(r), held=left, order=None, kind=None, msg=why)
        self._log(r, "주문 취소", None, ex, why)
