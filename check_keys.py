"""
DART · KRX 인증키 연결 테스트
기가비스(420770)를 예시로 두 API가 정상적으로 응답하는지 확인합니다.
인증키 자체는 화면에 출력하지 않습니다.
"""
import io
import os
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

STOCK_CODE = "420770"  # 기가비스

load_dotenv()
DART_KEY = os.getenv("DART_API_KEY", "").strip()
KRX_KEY = os.getenv("KRX_API_KEY", "").strip()


def check_dart():
    print("\n[1] DART 연결 테스트")
    if not DART_KEY:
        print("  [실패] .env 파일에 DART_API_KEY가 없어요.")
        return False

    # 1) 전체 회사 목록을 받아 종목코드로 DART 고유번호 찾기
    r = requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": DART_KEY},
        timeout=60,
    )
    if not r.content.startswith(b"PK"):  # 정상이면 zip 파일, 키가 틀리면 오류 메시지
        print("  [실패] DART 응답 오류:", r.text[:300])
        return False

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))

    corp_code = None
    for item in root.iter("list"):
        if (item.findtext("stock_code") or "").strip() == STOCK_CODE:
            corp_code = item.findtext("corp_code")
            break
    if not corp_code:
        print(f"  [실패] 종목코드 {STOCK_CODE}를 DART 목록에서 못 찾았어요.")
        return False

    # 2) 기업 개황 조회
    info = requests.get(
        "https://opendart.fss.or.kr/api/company.json",
        params={"crtfc_key": DART_KEY, "corp_code": corp_code},
        timeout=30,
    ).json()
    if info.get("status") != "000":
        print("  [실패] DART 응답 오류:", info.get("status"), info.get("message"))
        return False

    print("  [성공]")
    print("    회사명   :", info.get("corp_name"))
    print("    고유번호 :", corp_code)
    print("    대표자   :", info.get("ceo_nm"))
    print("    설립일   :", info.get("est_dt"))
    print("    결산월   :", info.get("acc_mt"))
    return True


def krx_get(basdd):
    """코스닥 일별매매정보 요청. https가 안 되면 http로 한 번 더 시도."""
    path = "data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd"
    headers = {"AUTH_KEY": KRX_KEY}
    try:
        return requests.get(f"https://{path}", headers=headers,
                            params={"basDd": basdd}, timeout=30)
    except requests.exceptions.SSLError:
        return requests.get(f"http://{path}", headers=headers,
                            params={"basDd": basdd}, timeout=30)


def check_krx():
    print("\n[2] KRX 연결 테스트")
    if not KRX_KEY:
        print("  [실패] .env 파일에 KRX_API_KEY가 없어요.")
        return False

    day = date.today()
    for _ in range(10):  # 주말·휴일이면 하루씩 거슬러 올라가며 시도
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        basdd = day.strftime("%Y%m%d")
        resp = krx_get(basdd)
        if resp.status_code != 200:
            print(f"  [실패] KRX 응답 오류 (HTTP {resp.status_code}):", resp.text[:300])
            return False

        rows = resp.json().get("OutBlock_1", [])
        if not rows:
            continue

        for row in rows:
            if row.get("ISU_CD") == STOCK_CODE:
                mktcap = int(row.get("MKTCAP", "0").replace(",", "")) / 1e8
                print("  [성공]")
                print("    기준일     :", basdd)
                print("    종목명     :", row.get("ISU_NM"))
                print("    종가       :", row.get("TDD_CLSPRC"), "원")
                print("    시가총액   :", f"{mktcap:,.0f} 억원")
                print("    상장주식수 :", row.get("LIST_SHRS"))
                return True

        print("  [실패] 데이터는 받았지만 기가비스를 못 찾았어요.")
        print("    받은 항목 이름:", list(rows[0].keys()))
        return False

    print("  [실패] 최근 영업일 데이터가 비어 있어요.")
    return False


if __name__ == "__main__":
    results = []
    for check in (check_dart, check_krx):
        try:
            results.append(check())
        except Exception as e:
            print("  [실패] 예외 발생:", type(e).__name__, e)
            results.append(False)

    print("\n" + ("두 API 모두 정상입니다." if all(results)
                  else "위 [실패] 메시지를 Claude에게 그대로 보여주세요."))
