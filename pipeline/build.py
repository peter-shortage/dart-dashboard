"""수집한 원본으로 대시보드용 숫자를 계산해 docs/data/종목코드.json 으로 저장.

- 분기 실적: 1~3분기는 보고서 값, 4분기 = 연간 - 3분기 누적
- 현금흐름: 분기 보고서 값이 누적이라 차이로 분기값 계산
- TTM(최근 4개 분기 합)으로 PER, EPS, ROE 계산
- 재무 건전성 체크 항목은 매번 자동 판정
"""
import re

from .common import RAW, SITE, now_kst, read_json, to_num, write_json
from .krx import load_prices

# ---------------------------------------------------------------- 계정 찾기
SINGLE = {
    # 키: (표 구분, 표준계정 ID, 계정명 후보)
    "revenue": ("IS", ["ifrs-full_Revenue"],
                ["매출액", "수익(매출액)", "매출액(수익)", "영업수익", "매출"]),
    "op_income": ("IS", ["dart_OperatingIncomeLoss"], ["영업이익"]),
    "net_income": ("IS", ["ifrs-full_ProfitLoss"],
                   ["당기순이익", "분기순이익", "반기순이익",
                    "연결당기순이익", "연결분기순이익", "연결반기순이익"]),
    "net_income_owner": ("IS", ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
                         ["지배기업의소유주에게귀속되는당기순이익", "지배기업의소유주",
                          "지배기업소유주지분", "지배기업소유주"]),
    "total_assets": ("BS", ["ifrs-full_Assets"], ["자산총계"]),
    "total_liab": ("BS", ["ifrs-full_Liabilities"], ["부채총계"]),
    "equity": ("BS", ["ifrs-full_Equity"], ["자본총계"]),
    "equity_owner": ("BS", ["ifrs-full_EquityAttributableToOwnersOfParent"],
                     ["지배기업소유주지분", "지배기업의소유주에게귀속되는자본", "지배기업의소유주지분"]),
    "current_assets": ("BS", ["ifrs-full_CurrentAssets"], ["유동자산"]),
    "current_liab": ("BS", ["ifrs-full_CurrentLiabilities"], ["유동부채"]),
    "cash": ("BS", ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "cfo": ("CF", ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
            ["영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동으로부터의현금흐름"]),
    "capex": ("CF", ["ifrs-full_PurchaseOfPropertyPlantAndEquipment"], ["유형자산의취득"]),
}
SUMS = {
    # 여러 줄을 더하는 항목 (계정명 기준)
    "short_fin": ("BS", ["단기금융상품", "단기금융자산", "단기투자자산", "단기예금", "단기예치금"]),
    "borrowings": ("BS", ["단기차입금", "장기차입금", "유동성장기차입금", "유동성장기부채",
                          "사채", "유동성사채", "전환사채", "유동성전환사채",
                          "신주인수권부사채", "유동성신주인수권부사채", "교환사채"]),
    "cb_bw": ("BS", ["전환사채", "유동성전환사채", "신주인수권부사채",
                     "유동성신주인수권부사채", "교환사채"]),
}
IS_KEYS = ["revenue", "op_income", "net_income", "net_income_owner"]
CF_KEYS = ["cfo", "capex"]
BS_KEYS = ["total_assets", "total_liab", "equity", "equity_owner", "current_assets",
           "current_liab", "cash", "short_fin", "borrowings", "cb_bw"]


def norm(name):
    s = re.sub(r"\s+", "", name or "")
    s = re.sub(r"\((손실|이익|순손실|순이익|유동|비유동)\)", "", s)
    return s


def _group(row):
    sj = row.get("sj_div")
    return "IS" if sj in ("IS", "CIS") else sj


def extract(rows, field):
    groups = {}
    for r in sorted(rows, key=lambda r: 1 if r.get("sj_div") == "CIS" else 0):  # 손익계산서 우선
        groups.setdefault(_group(r), []).append(r)

    out = {}
    for key, (g, ids, names) in SINGLE.items():
        cands = groups.get(g, [])
        val = None
        for match in (lambda r: r.get("account_id") in ids,
                      lambda r: norm(r.get("account_nm")) in names):
            for r in cands:
                if match(r):
                    val = to_num(r.get(field))
                    if val is not None:
                        break
            if val is not None:
                break
        out[key] = val

    for key, (g, names) in SUMS.items():
        cands = groups.get(g, [])
        has_data = any(to_num(r.get(field)) is not None for r in cands)
        total, seen = 0.0, set()
        for r in cands:
            n = norm(r.get("account_nm"))
            v = to_num(r.get(field))
            if n in names and n not in seen and v is not None:
                total += v
                seen.add(n)
        out[key] = total if has_data else None

    # 연결이 아니면 지배주주 항목이 없으므로 전체 값으로 대신
    if out["net_income_owner"] is None:
        out["net_income_owner"] = out["net_income"]
    if out["equity_owner"] is None:
        out["equity_owner"] = out["equity"]
    if out["capex"] is not None:
        out["capex"] = abs(out["capex"])
    return out


# ---------------------------------------------------------------- 기간별 정리
def _sub(a, b):
    return None if a is None or b is None else a - b


def build_financials(code):
    folder = RAW / "dart" / code
    annual, prior, quarters, fs_used = {}, {}, {}, set()

    for path in sorted(folder.glob("fin_*.json")):
        _, year, tag = path.stem.split("_")
        year = int(year)
        data = read_json(path) or {}
        rows = data.get("list") or []
        if not rows:
            continue
        fs_used.add(data.get("fs_div"))
        cur = extract(rows, "thstrm_amount")
        if tag == "FY":
            annual[year] = cur
            # 사업보고서에 실린 전기·전전기 값은 해당 연도 보고서가 없을 때만 사용
            for back, field in ((1, "frmtrm_amount"), (2, "bfefrmtrm_amount")):
                prev = extract(rows, field)
                if any(v is not None for v in prev.values()):
                    prior.setdefault(year - back, prev)
        else:
            cum = extract(rows, "thstrm_add_amount")
            quarters[(year, int(tag[1]))] = {"cur": cur, "cum": cum}

    for y, v in prior.items():
        annual.setdefault(y, v)

    def is_cum(y, q, k):
        """손익 누적값 (q분기까지)."""
        e = quarters.get((y, q))
        if not e:
            return None
        if e["cum"][k] is not None:
            return e["cum"][k]
        if q == 1:
            return e["cur"][k]
        parts = [quarters.get((y, i), {}).get("cur", {}).get(k) for i in range(1, q + 1)]
        return None if None in parts else sum(parts)

    qlist = []
    years = sorted({y for y, _ in quarters} | set(annual))
    for y in years:
        for q in (1, 2, 3, 4):
            rec = {}
            if q < 4:
                e = quarters.get((y, q))
                if not e:
                    continue
                for k in IS_KEYS:
                    rec[k] = e["cur"][k]
                for k in BS_KEYS:
                    rec[k] = e["cur"][k]
                for k in CF_KEYS:  # 현금흐름은 누적값 → 직전 분기 누적을 빼서 분기값
                    prev = None if q == 1 else quarters.get((y, q - 1), {}).get("cur", {}).get(k)
                    rec[k] = e["cur"][k] if q == 1 else _sub(e["cur"][k], prev)
            else:
                a = annual.get(y)
                if not a or (y, 3) not in quarters:
                    continue
                for k in IS_KEYS:
                    rec[k] = _sub(a[k], is_cum(y, 3, k))
                for k in BS_KEYS:
                    rec[k] = a[k]
                for k in CF_KEYS:
                    rec[k] = _sub(a[k], quarters[(y, 3)]["cur"][k])
            rec["period"] = f"{y}Q{q}"
            qlist.append(rec)

    alist = []
    for y in sorted(annual):
        rec = dict(annual[y])
        rec["period"] = str(y)
        alist.append(rec)

    _add_ratios(alist, lambda p: str(int(p) - 1))
    _add_ratios(qlist, lambda p: f"{int(p[:4]) - 1}{p[4:]}")  # 전년 동기
    return alist, qlist, sorted(x for x in fs_used if x)


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(a / b * 100, 1)


def _growth(a, b):
    if a is None or b in (None, 0) or b < 0:
        return None
    return round((a / b - 1) * 100, 1)


def _add_ratios(series, prev_period):
    by_period = {r["period"]: r for r in series}
    for r in series:
        r["opm"] = _pct(r["op_income"], r["revenue"])
        r["npm"] = _pct(r["net_income"], r["revenue"])
        prev = by_period.get(prev_period(r["period"]))
        r["rev_yoy"] = _growth(r["revenue"], prev["revenue"]) if prev else None
        r["op_yoy"] = _growth(r["op_income"], prev["op_income"]) if prev else None


def _ttm(qlist, key):
    """최근 4개 분기 합. 연속된 4개가 안 되면 None."""
    vals = [q.get(key) for q in qlist[-4:]]
    if len(vals) < 4 or None in vals:
        return None
    return sum(vals)


# ---------------------------------------------------------------- 배당
def build_dividends(code):
    out = []
    for path in sorted((RAW / "dart" / code).glob("div_*.json")):
        year = int(path.stem.split("_")[1])
        rec = {"year": year, "dps": None, "payout": None, "yield": None, "eps": None}
        for r in (read_json(path) or {}).get("list", []):
            se = norm(r.get("se"))
            kind = r.get("stock_knd") or ""
            v = to_num(r.get("thstrm"))
            if se.startswith("주당현금배당금") and (not kind or "보통" in kind):
                rec["dps"] = v
            elif "현금배당성향" in se:
                rec["payout"] = v
            elif "현금배당수익률" in se and (not kind or "보통" in kind):
                rec["yield"] = v
            elif "주당순이익" in se and rec["eps"] is None:
                rec["eps"] = v
        if any(rec[k] is not None for k in ("dps", "payout", "eps")):
            out.append(rec)
    return out


# ---------------------------------------------------------------- 주가·이동평균
def build_prices(code):
    rows = load_prices(code)
    closes = [r["close"] for r in rows]

    def ma(n):
        out, total = [], 0.0
        for i, c in enumerate(closes):
            total += c
            if i >= n:
                total -= closes[i - n]
            out.append(round(total / n, 1) if i >= n - 1 else None)
        return out

    return rows, {
        "date": [r["date"] for r in rows],
        "open": [r["open"] for r in rows],
        "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows],
        "close": closes,
        "volume": [r["volume"] for r in rows],
        "ma5": ma(5), "ma20": ma(20), "ma60": ma(60), "ma120": ma(120),
    }


# ---------------------------------------------------------------- 공시
def build_disclosures(code):
    items = read_json(RAW / "dart" / code / "disclosures.json", [])
    link = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
    simple = [{"date": f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}",
               "title": x.get("report_nm", "").strip(),
               "url": link + x.get("rcept_no", "")} for x in items]
    simple.sort(key=lambda x: x["date"], reverse=True)
    contracts = [x for x in simple if "단일판매" in x["title"] or "공급계약" in x["title"]]
    return contracts, simple[:30]


# ---------------------------------------------------------------- 재무 건전성 체크
def health_checks(h):
    checks = []

    def add(level, text):
        checks.append({"level": level, "text": text})

    if h["net_cash"] is not None and h["net_cash"] < 0:
        add("warn", "순차입 상태 (현금성 자산보다 차입금이 많음)")
    if h["debt_ratio"] is not None:
        if h["debt_ratio"] > 200:
            add("danger", f"부채비율 {h['debt_ratio']}% (200% 초과)")
        elif h["debt_ratio"] > 100:
            add("warn", f"부채비율 {h['debt_ratio']}% (100% 초과)")
    if h["current_ratio"] is not None:
        if h["current_ratio"] < 100:
            add("danger", f"유동비율 {h['current_ratio']}% (100% 미만)")
        elif h["current_ratio"] < 150:
            add("warn", f"유동비율 {h['current_ratio']}% (150% 미만)")
    if h["cb_bw"]:
        add("warn", "전환사채·신주인수권부사채 잔액 있음 (주식 희석 가능)")
    if h["op_income_ttm"] is not None and h["op_income_ttm"] < 0:
        add("warn", "최근 4개 분기 합산 영업적자")
    if h["cfo_ttm"] is not None and h["cfo_ttm"] < 0:
        add("warn", "최근 4개 분기 영업현금흐름 마이너스")
    if not checks:
        add("ok", "주요 재무 위험 신호 없음")
    return checks


# ---------------------------------------------------------------- 종목 하나 만들기
def build_stock(stock):
    code = stock["code"]
    company = read_json(RAW / "dart" / code / "company.json", {})
    annual, quarterly, fs_used = build_financials(code)
    dividends = build_dividends(code)
    price_rows, chart = build_prices(code)
    contracts, recent = build_disclosures(code)

    last = price_rows[-1] if price_rows else {}
    price, mktcap, shares = last.get("close"), last.get("mktcap"), last.get("shares")
    prev_close = price_rows[-2]["close"] if len(price_rows) > 1 else None
    year_rows = price_rows[-250:]

    latest_q = quarterly[-1] if quarterly else {}
    latest_bs = latest_q if latest_q.get("total_assets") is not None else (annual[-1] if annual else {})
    ni_ttm = _ttm(quarterly, "net_income_owner")
    equity_owner = latest_bs.get("equity_owner")

    def div(a, b):
        return None if a is None or b in (None, 0) else a / b

    per = div(mktcap, ni_ttm) if ni_ttm and ni_ttm > 0 else None
    eps_ttm = div(ni_ttm, shares)
    fwd_eps = stock.get("consensus_eps_12m")
    last_div = dividends[-1] if dividends else {}

    overview = {
        "price": price,
        "change_pct": round((price / prev_close - 1) * 100, 2) if price and prev_close else None,
        "price_date": last.get("date"),
        "mktcap": mktcap,
        "shares": shares,
        "high_52w": max((r["high"] for r in year_rows), default=None),
        "low_52w": min((r["low"] for r in year_rows), default=None),
        "per_ttm": round(per, 1) if per else None,
        "per_note": "적자" if ni_ttm is not None and ni_ttm <= 0 else None,
        "fper_12m": round(price / fwd_eps, 1) if price and fwd_eps else None,
        "pbr": round(div(mktcap, equity_owner), 2) if div(mktcap, equity_owner) else None,
        "eps_ttm": round(eps_ttm) if eps_ttm is not None else None,
        "bps": round(div(equity_owner, shares)) if div(equity_owner, shares) else None,
        "roe_ttm": _pct(ni_ttm, equity_owner),
        "dps": last_div.get("dps"),
        "payout": last_div.get("payout"),
        "div_yield": round(last_div["dps"] / price * 100, 2) if last_div.get("dps") and price else None,
        "ttm_basis": f"{quarterly[-4]['period']}~{quarterly[-1]['period']}" if len(quarterly) >= 4 else None,
    }

    cash, short_fin = latest_bs.get("cash"), latest_bs.get("short_fin")
    liquid = None if cash is None else cash + (short_fin or 0)
    borrowings = latest_bs.get("borrowings")
    cfo_ttm, capex_ttm = _ttm(quarterly, "cfo"), _ttm(quarterly, "capex")
    health = {
        "basis": latest_bs.get("period"),
        "cash": cash,
        "short_fin": short_fin,
        "liquid": liquid,
        "borrowings": borrowings,
        "net_cash": _sub(liquid, borrowings),
        "liquid_to_mktcap": _pct(liquid, mktcap),
        "cb_bw": latest_bs.get("cb_bw"),
        "debt_ratio": _pct(latest_bs.get("total_liab"), latest_bs.get("equity")),
        "current_ratio": _pct(latest_bs.get("current_assets"), latest_bs.get("current_liab")),
        "cfo_ttm": cfo_ttm,
        "capex_ttm": capex_ttm,
        "fcf_ttm": _sub(cfo_ttm, capex_ttm),
        "op_income_ttm": _ttm(quarterly, "op_income"),
    }
    health["checks"] = health_checks(health)

    out = {
        "meta": {
            "code": code,
            "name": stock["name"],
            "market": stock["market"],
            "sector": stock.get("sector"),
            "corp_code": company.get("corp_code"),
            "ceo": company.get("ceo_nm"),
            "est_dt": company.get("est_dt"),
            "homepage": company.get("hm_url"),
            "fs_div": fs_used,
            "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        },
        "overview": overview,
        "health": health,
        "annual": annual,
        "quarterly": quarterly,
        "dividends": dividends,
        "contracts": contracts,
        "recent_disclosures": recent,
        "chart": chart,
    }
    write_json(SITE / f"{code}.json", out, compact=True)
    return out


def build_all(stocks):
    index = []
    for s in stocks:
        out = build_stock(s)
        o = out["overview"]
        index.append({"code": s["code"], "name": s["name"], "sector": s.get("sector"),
                      "price": o["price"], "change_pct": o["change_pct"], "mktcap": o["mktcap"]})
        print_summary(out)
    write_json(SITE / "index.json", {"updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
                                     "stocks": index})


def _eok(v):
    return "-" if v is None else f"{v / 1e8:,.0f}억"


def print_summary(out):
    m, o, h = out["meta"], out["overview"], out["health"]
    print(f"\n  ■ {m['name']} ({m['code']})  재무제표 기준: {', '.join(m['fs_div']) or '-'}")
    print(f"    주가 {o['price']:,.0f}원 ({o['price_date']})  시총 {_eok(o['mktcap'])}" if o["price"] else "    주가 데이터 없음")
    print(f"    PER {o['per_ttm'] or o['per_note'] or '-'}  PBR {o['pbr'] or '-'}  "
          f"EPS {o['eps_ttm'] or '-'}  ROE {o['roe_ttm'] or '-'}%  (TTM {o['ttm_basis'] or '-'})")
    print("    연간 실적 (매출 / 영업이익 / 순이익)")
    for r in out["annual"][-5:]:
        print(f"      {r['period']}: {_eok(r['revenue'])} / {_eok(r['op_income'])} / {_eok(r['net_income'])}")
    print("    최근 분기 (매출 / 영업이익)")
    for r in out["quarterly"][-4:]:
        print(f"      {r['period']}: {_eok(r['revenue'])} / {_eok(r['op_income'])}")
    print(f"    재무({h['basis']}): 현금성 {_eok(h['liquid'])}  차입금 {_eok(h['borrowings'])}  "
          f"순현금 {_eok(h['net_cash'])}  부채비율 {h['debt_ratio']}%  유동비율 {h['current_ratio']}%")
    for c in h["checks"]:
        print(f"      [{c['level']}] {c['text']}")
    print(f"    공급계약 공시 {len(out['contracts'])}건, 주가 {len(out['chart']['date'])}일치")
