"""공통 도구: 경로, 인증키, 파일 읽기/쓰기, 재시도가 붙은 HTTP 요청."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"          # 수집한 원본 데이터
RAW = DATA / "raw"
SITE = ROOT / "docs" / "data"  # 대시보드가 읽는 가공 데이터
KST = timezone(timedelta(hours=9))

load_dotenv(ROOT / ".env")


def env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f".env 파일(또는 GitHub Secrets)에 {name}가 없어요.")
    return value


def now_kst():
    return datetime.now(KST)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)


_session = requests.Session()
_session.headers["User-Agent"] = "dart-dashboard/0.1"


def http_get(url, *, params=None, headers=None, timeout=30, tries=4, pause=0.15):
    """일시적인 오류는 몇 번 다시 시도. 오류 메시지에는 인증키가 들어가지 않게 주소만 남김."""
    last = None
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            time.sleep(pause)
            return r
        except requests.exceptions.SSLError:
            raise
        except requests.exceptions.RequestException as e:
            last = type(e).__name__
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"요청 실패({last}): {url}")


def to_num(value):
    """'1,234', '-1,234', '', '-' 같은 문자열을 숫자로. 비어 있으면 None."""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
