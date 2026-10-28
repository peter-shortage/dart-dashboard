"""KRX 일별매매정보 수집.

KRX API는 '날짜 하루치 시장 전체'를 돌려주는 방식이라, 하루에 한 번 요청해서
관심 종목 줄만 골라 종목별 CSV(data/prices/종목코드.csv)에 이어 붙인다.
같은 시장 종목이 여러 개여도 날짜당 요청은 한 번.
"""
import csv
from datetime import date, timedelta

import requests

from .common import DATA, env, http_get, now_kst, to_num

HOST = "data-dbg.krx.co.kr/svc/apis/sto/"
ENDPOINT = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd"}
COLUMNS = ["date", "open", "high", "low", "close", "volume", "value", "mktcap", "shares"]
_scheme = "https"


def fetch_day(market, basdd):
    global _scheme
    headers = {"AUTH_KEY": env("KRX_API_KEY")}
    try:
        r = http_get(f"{_scheme}://{HOST}{ENDPOINT[market]}",
                     params={"basDd": basdd}, headers=headers)
    except requests.exceptions.SSLError:
        _scheme = "http"
        r = http_get(f"http://{HOST}{ENDPOINT[market]}",
                     params={"basDd": basdd}, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"KRX 오류 HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("OutBlock_1", []) or []


def prices_path(code):
    return DATA / "prices" / f"{code}.csv"


def load_prices(code):
    path = prices_path(code)
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in COLUMNS[1:]:
            r[k] = to_num(r[k])
    return rows


def save_prices(code, rows):
    path = prices_path(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: (int(r[k]) if isinstance(r[k], float) else r[k]) for k in COLUMNS})


def _row_from_krx(basdd, x):
    return {
        "date": f"{basdd[:4]}-{basdd[4:6]}-{basdd[6:]}",
        "open": to_num(x.get("TDD_OPNPRC")),
        "high": to_num(x.get("TDD_HGPRC")),
        "low": to_num(x.get("TDD_LWPRC")),
        "close": to_num(x.get("TDD_CLSPRC")),
        "volume": to_num(x.get("ACC_TRDVOL")),
        "value": to_num(x.get("ACC_TRDVAL")),
        "mktcap": to_num(x.get("MKTCAP")),
        "shares": to_num(x.get("LIST_SHRS")),
    }


def update_prices(stocks):
    today = now_kst().date()
    end = today - timedelta(days=1)  # 당일 데이터는 다음 날 공개되므로 어제까지

    by_market = {}
    for s in stocks:
        rows = load_prices(s["code"])
        if rows:
            start = date.fromisoformat(rows[-1]["date"]) + timedelta(days=1)
        else:
            start = date.fromisoformat(s.get("price_start") or str(today - timedelta(days=3653)))
        by_market.setdefault(s["market"], {})[s["code"]] = {"rows": rows, "start": start, "name": s["name"]}

    for market, targets in by_market.items():
        first = min(t["start"] for t in targets.values())
        days = [first + timedelta(days=i) for i in range((end - first).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        if not days:
            print(f"  {market}: 이미 최신이에요.")
            continue
        print(f"  {market}: {days[0]} ~ {days[-1]} ({len(days)}일) 요청")

        added = 0
        for i, d in enumerate(days, 1):
            need = {c for c, t in targets.items() if t["start"] <= d}
            basdd = d.strftime("%Y%m%d")
            for x in fetch_day(market, basdd):
                code = x.get("ISU_CD")
                if code in need:
                    targets[code]["rows"].append(_row_from_krx(basdd, x))
                    added += 1
            if i % 50 == 0 or i == len(days):
                print(f"    {i}/{len(days)}일 처리")
                for c, t in targets.items():
                    save_prices(c, t["rows"])
        print(f"  {market}: {added}건 추가")
