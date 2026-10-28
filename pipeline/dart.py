"""OpenDART 수집: 회사 고유번호, 기업개황, 재무제표(전체), 배당, 공시 목록.

받은 응답은 data/raw/dart/종목코드/ 아래에 그대로 저장해 두고(캐시),
이미 받은 과거 보고서는 다시 요청하지 않는다.
"""
import io
import xml.etree.ElementTree as ET
import zipfile

from .common import RAW, env, http_get, now_kst, read_json, write_json

API = "https://opendart.fss.or.kr/api/"
REPORTS = [("11013", "Q1"), ("11012", "Q2"), ("11014", "Q3"), ("11011", "FY")]


def call(endpoint, **params):
    params["crtfc_key"] = env("DART_API_KEY")
    r = http_get(API + endpoint, params=params, timeout=60)
    data = r.json()
    status = data.get("status")
    if status == "000":
        return data
    if status == "013":  # 조회된 데이터 없음
        return None
    raise RuntimeError(f"DART {endpoint} 오류 {status}: {data.get('message')}")


def corp_code(stock_code):
    path = RAW / "dart" / "corp_codes.json"
    table = read_json(path, {})
    if stock_code not in table:
        r = http_get(API + "corpCode.xml", params={"crtfc_key": env("DART_API_KEY")}, timeout=120)
        if not r.content.startswith(b"PK"):
            raise RuntimeError("DART 회사 목록 오류: " + r.text[:200])
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            root = ET.fromstring(z.read(z.namelist()[0]))
        table = {}
        for item in root.iter("list"):
            sc = (item.findtext("stock_code") or "").strip()
            if sc:
                table[sc] = item.findtext("corp_code")
        write_json(path, table)
    if stock_code not in table:
        raise RuntimeError(f"종목코드 {stock_code}를 DART에서 못 찾았어요.")
    return table[stock_code]


def _dir(code):
    return RAW / "dart" / code


def update_company(code, corp):
    data = call("company.json", corp_code=corp)
    write_json(_dir(code) / "company.json", data or {})


def update_financials(code, corp, first_year):
    """연도 × (1분기, 반기, 3분기, 사업보고서). 연결(CFS)이 없으면 별도(OFS)."""
    this_year = now_kst().year
    fetched = 0
    for year in range(first_year, this_year + 1):
        for reprt, tag in REPORTS:
            path = _dir(code) / f"fin_{year}_{tag}.json"
            cached = read_json(path)
            # 내용이 있으면 끝. 비어 있어도 2년 이상 지난 건 다시 안 물어봄.
            if cached is not None and (cached.get("list") or year < this_year - 1):
                continue
            got = None
            for fs_div in ("CFS", "OFS"):
                d = call("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year),
                         reprt_code=reprt, fs_div=fs_div)
                if d:
                    got = {"fs_div": fs_div, "list": d.get("list", [])}
                    break
            write_json(path, got or {"fs_div": None, "list": []})
            fetched += 1
            if got:
                print(f"    {year} {tag}: {got['fs_div']} {len(got['list'])}개 항목")
    print(f"  재무제표 요청 {fetched}건")


def update_dividends(code, corp, first_year):
    this_year = now_kst().year
    for year in range(first_year, this_year):
        path = _dir(code) / f"div_{year}.json"
        cached = read_json(path)
        if cached is not None and (cached.get("list") or year < this_year - 1):
            continue
        d = call("alotMatter.json", corp_code=corp, bsns_year=str(year), reprt_code="11011")
        write_json(path, {"list": (d or {}).get("list", [])})


def update_disclosures(code, corp, first_year):
    items, page = [], 1
    end = now_kst().strftime("%Y%m%d")
    while True:
        d = call("list.json", corp_code=corp, bgn_de=f"{first_year}0101", end_de=end,
                 last_reprt_at="Y", page_no=page, page_count=100)
        if not d:
            break
        items += d.get("list", [])
        if page >= int(d.get("total_page", 1)):
            break
        page += 1
    write_json(_dir(code) / "disclosures.json", items)
    print(f"  공시 목록 {len(items)}건")
