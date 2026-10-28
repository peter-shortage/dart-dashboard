"""대시보드 데이터 수집 실행 파일.

사용법 (저장소 폴더에서):
    python collect.py              전체 수집 + 가공
    python collect.py --no-prices  주가는 건너뛰고 DART만
    python collect.py --build-only 수집 없이 가공만 다시
"""
import argparse
import sys

from pipeline import build, dart, krx
from pipeline.common import ROOT, now_kst, read_json

# 윈도우 콘솔에서 한글이 깨지지 않게
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-prices", action="store_true")
    p.add_argument("--build-only", action="store_true")
    args = p.parse_args()

    stocks = read_json(ROOT / "stocks.json")["stocks"]

    if not args.build_only:
        if not args.no_prices:
            print("== 주가 (KRX) ==")
            krx.update_prices(stocks)

        for s in stocks:
            print(f"\n== {s['name']} 공시 (DART) ==")
            corp = dart.corp_code(s["code"])
            first = int(s.get("fin_start") or now_kst().year - 10)
            dart.update_company(s["code"], corp)
            dart.update_financials(s["code"], corp, first)
            dart.update_dividends(s["code"], corp, first)
            dart.update_disclosures(s["code"], corp, first)

    print("\n== 대시보드 데이터 만들기 ==")
    build.build_all(stocks)
    print("\n완료. 위 요약을 Claude에게 보여주세요.")


if __name__ == "__main__":
    main()
