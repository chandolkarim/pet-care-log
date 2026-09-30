"""반려동물 돌봄 기록(구글 시트 일정 탭 + 폼 응답 탭)을 읽어 '지금 상태' 페이지를 만듭니다. 외부 패키지 불필요.

읽는 순서: --schedule/--log 인자 → 환경 변수 SCHEDULE_CSV_URL/LOG_CSV_URL → 같은 폴더의 sample_*.csv
잘못된 데이터(빠진 열, 잘못된 시각·날짜, 사용 중인 일정 없음 등)는 HTML을 만들기 전에 이유와 행 번호를 출력하고 멈춥니다.
페이지는 열 때마다 브라우저가 시트를 다시 읽고, 읽지 못하면 이 스크립트가 넣어 둔 생성 시점 데이터를 보여 줍니다.
"""

import argparse
import csv
from datetime import date, datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
SAMPLE_SCHEDULE = HERE / "sample_schedule.csv"
SAMPLE_LOG = HERE / "sample_log.csv"
SAMPLE_BASE_DATE = date(2026, 9, 30)  # 샘플 기록의 '오늘'. 로컬 실행 때 실제 오늘로 옮깁니다.
SITE_FILE = HERE / "site.json"
TEMPLATE_FILE = HERE / "page.html"

SCHEDULE_REQUIRED = ("반려동물", "항목", "종류", "시각", "사용")
LOG_REQUIRED = ("타임스탬프", "누가", "반려동물", "항목")
KINDS = {"밥", "약"}
USE_VALUES = {"Y": True, "N": False}
FREE_ITEMS = {"증상", "체중"}  # 일정 없이 기록만 하는 항목

KO_STAMP = re.compile(r"^(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s+(?:(오전|오후)\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?$")
ISO_STAMP = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
US_STAMP = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(AM|PM))?$", re.I)
TIME_ONLY = re.compile(r"^(?:(오전|오후)\s*)?(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*(AM|PM))?$", re.I)
DATE_TEXT = re.compile(r"(\d{4})\s*[-.]\s*(\d{1,2})\s*[-.]\s*(\d{1,2})\.?")


class DataError(ValueError):
    """시트 데이터가 약속한 형식과 다를 때 사용합니다."""


def to_24h(hour, marker):
    marker = (marker or "").upper()
    if marker in ("오후", "PM") and hour < 12:
        return hour + 12
    if marker in ("오전", "AM") and hour == 12:
        return 0
    return hour


def parse_stamp(value):
    """구글 폼 타임스탬프(한국어·영어·ISO 표시)를 한국시간 기준 날짜·시각으로 바꿉니다."""
    value = value.strip()
    if m := KO_STAMP.match(value):
        y, mo, d, marker, h, mi, _ = m.groups()
    elif m := ISO_STAMP.match(value):
        y, mo, d, h, mi, _ = m.groups()
        marker = None
    elif m := US_STAMP.match(value):
        mo, d, y, h, mi, _, marker = m.groups()
    else:
        return None
    try:
        return datetime(int(y), int(mo), int(d), to_24h(int(h), marker), int(mi))
    except ValueError:
        return None


def parse_time_only(value):
    m = TIME_ONLY.match(value.strip())
    if not m:
        return None
    marker, h, mi, marker_en = m.groups()
    hour, minute = to_24h(int(h), marker or marker_en), int(mi)
    return (hour, minute) if hour < 24 and minute < 60 else None


def parse_date(value, number, name):
    if not value:
        return ""
    m = DATE_TEXT.fullmatch(value)  # 시트가 날짜로 바꿔 '2026. 9. 1'처럼 내보내도 읽습니다
    if not m:
        raise DataError(f"일정 {number}행의 {name}은 YYYY-MM-DD 형식이어야 합니다: {value}")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError as error:
        raise DataError(f"일정 {number}행의 {name}이 없는 날짜입니다: {value}") from error


def read_source(source):
    """URL이면 내려받고, 파일 경로면 읽어서 CSV 문자열을 돌려줍니다."""
    if re.match(r"https?://", source):
        request = Request(source, headers={"User-Agent": "pet-care-log/1.0"})
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
        except HTTPError as error:
            raise DataError(f"시트 주소에서 {error.code} 응답을 받았습니다. 웹에 게시했는지, 주소가 맞는지 확인하세요.") from error
        except URLError as error:
            raise DataError(f"시트 주소에 연결하지 못했습니다: {error.reason}") from error
    else:
        raw = Path(source).read_bytes()
    text = raw.decode("utf-8-sig")
    head = text.lstrip()[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise DataError("CSV 대신 웹페이지가 왔습니다. 웹에 게시에서 CSV로 게시한 주소인지 확인하세요.")
    return text


def read_table(text, required, label):
    """열 이름을 확인하고 (시트 행 번호, 정리한 행) 목록을 돌려줍니다. 머리글이 1행입니다."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise DataError(f"{label} 탭이 비어 있습니다. 1행에 열 이름을 적으세요.")
    header = [name.strip() for name in reader.fieldnames]
    missing = [name for name in required if name not in header]
    if missing:
        raise DataError(f"{label} 탭에 필수 열이 없습니다: {', '.join(missing)} / 현재 열: {', '.join(header) or '(없음)'}")
    rows = []
    for number, row in enumerate(reader, start=2):
        row = {(key or "").strip(): (value or "").strip() for key, value in row.items() if key is not None}
        if any(row.values()):
            rows.append((number, row))
    return rows


def parse_schedule(text):
    schedule, paused = [], 0
    for number, row in read_table(text, SCHEDULE_REQUIRED, "일정"):
        use = row.get("사용", "").upper()
        if use not in USE_VALUES:
            raise DataError(f"일정 {number}행의 사용은 Y 또는 N이어야 합니다: {row.get('사용') or '(빈칸)'}")
        for name in ("반려동물", "항목"):
            if not row.get(name):
                raise DataError(f"일정 {number}행의 {name}이(가) 비어 있습니다.")
        kind = row.get("종류", "")
        if kind not in KINDS:
            raise DataError(f"일정 {number}행의 종류는 밥 또는 약이어야 합니다: {kind or '(빈칸)'}")
        times = []
        for part in [p.strip() for p in row.get("시각", "").split(",") if p.strip()]:
            parsed = parse_time_only(part)  # 시트가 시간으로 바꿔 '8:00:00'처럼 내보내도 읽습니다
            if parsed is None:
                raise DataError(f"일정 {number}행의 시각은 HH:MM이어야 합니다(여러 개면 쉼표): {part}")
            times.append(f"{parsed[0]:02d}:{parsed[1]:02d}")
        if not times:
            raise DataError(f"일정 {number}행의 시각이 비어 있습니다.")
        interval = None
        raw_interval = row.get("최소간격", "")
        if raw_interval:
            try:
                interval = float(raw_interval)
            except ValueError:
                interval = -1
            if interval <= 0:
                raise DataError(f"일정 {number}행의 최소간격은 0보다 큰 숫자(시간)여야 합니다: {raw_interval}")
        elif kind == "약":
            raise DataError(f"일정 {number}행은 약이므로 최소간격(시간)을 적어야 합니다. 중복 경고의 기준입니다.")
        start = parse_date(row.get("시작일", ""), number, "시작일")
        end = parse_date(row.get("종료일", ""), number, "종료일")
        if start and end and end < start:
            raise DataError(f"일정 {number}행의 종료일({end})이 시작일({start})보다 앞입니다.")
        if not USE_VALUES[use]:
            paused += 1
            continue
        schedule.append({"row": number, "pet": row["반려동물"], "item": row["항목"], "kind": kind,
                         "times": sorted(set(times)), "amount": row.get("양", ""), "interval": interval,
                         "start": start, "end": end, "memo": row.get("메모", "")})
    if not schedule:
        raise DataError("사용이 Y인 일정이 없습니다. 지금 챙기는 밥·약 행의 사용 열에 Y를 적으세요.")
    return schedule, paused


def parse_log(text):
    logs = []
    for number, row in read_table(text, LOG_REQUIRED, "기록"):
        stamp = parse_stamp(row.get("타임스탬프", ""))
        if stamp is None:
            raise DataError(f"기록 {number}행의 타임스탬프를 읽지 못했습니다: {row.get('타임스탬프') or '(빈칸)'}")
        for name in ("누가", "반려동물", "항목"):
            if not row.get(name):
                raise DataError(f"기록 {number}행의 {name}이(가) 비어 있습니다. 폼에서 필수 질문으로 두세요.")
        at = stamp
        if row.get("실제 시각"):
            actual = parse_time_only(row["실제 시각"])
            if actual is None:
                raise DataError(f"기록 {number}행의 실제 시각을 읽지 못했습니다: {row['실제 시각']}")
            at = stamp.replace(hour=actual[0], minute=actual[1])
            if at > stamp + timedelta(minutes=5):  # 자정 넘어 몰아 적은 경우: 전날 시각
                at -= timedelta(days=1)
        logs.append({"row": number, "at": at, "who": row["누가"], "pet": row["반려동물"], "item": row["항목"],
                     "amount": row.get("양", ""), "weight": row.get("체중", ""),
                     "symptoms": row.get("증상", ""), "memo": row.get("메모", "")})
    logs.sort(key=lambda log: log["at"])
    return logs


def unknown_items(schedule_text, logs):
    """일정 탭 어디에도 없는 항목(사용 N 포함)으로 적힌 기록. 멈추지 않고 알리기만 합니다."""
    known = {(row.get("반려동물", ""), row.get("항목", "")) for _, row in read_table(schedule_text, SCHEDULE_REQUIRED, "일정")}
    return [log for log in logs if log["item"] not in FREE_ITEMS and (log["pet"], log["item"]) not in known]


def shift_sample(schedule, logs, today):
    """샘플은 날짜가 고정돼 있으니, 샘플의 '오늘'을 실제 오늘로 옮겨 로컬에서도 상태가 보이게 합니다."""
    offset = today - SAMPLE_BASE_DATE
    for item in schedule:
        for key in ("start", "end"):
            if item[key]:
                item[key] = (date.fromisoformat(item[key]) + offset).isoformat()
    for log in logs:
        log["at"] += offset


def build_config(site, schedule, logs, urls, generated_at, source_label, environment=None):
    environment = os.environ if environment is None else environment
    run_number = environment.get("GITHUB_RUN_NUMBER", "")
    commit = environment.get("GITHUB_SHA", "")
    build = f"Actions 실행 #{run_number}" if run_number else "로컬 생성본"
    if commit:
        build += f" · 커밋 {commit[:7]}"
    return {
        "title": site["title"], "description": site["description"],
        "scheduleUrl": urls["schedule"], "logUrl": urls["log"], "formUrl": urls["form"],
        "generatedAt": generated_at.strftime("%Y-%m-%d %H:%M"), "sourceLabel": source_label, "build": build,
        "snapshot": {
            "schedule": [{k: v for k, v in item.items() if k != "row"} for item in schedule],
            "logs": [{**{k: v for k, v in log.items() if k not in ("row", "at")}, "at": log["at"].strftime("%Y-%m-%d %H:%M")}
                     for log in logs],
        },
    }


def generate_html(config, template_path=TEMPLATE_FILE):
    template = Path(template_path).read_text(encoding="utf-8")
    payload = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    title = config["title"].replace("&", "&amp;").replace("<", "&lt;")
    return template.replace("__TITLE__", title).replace("/*__CONFIG__*/null", payload)


def load_site(filepath=SITE_FILE):
    with Path(filepath).open(encoding="utf-8") as source:
        site = json.load(source)
    for key in ("title", "description"):
        if not isinstance(site.get(key), str) or not site[key].strip():
            raise DataError(f"site.json의 {key}는 비어 있지 않은 문자열이어야 합니다.")
    return site


def atomic_write(output, content):
    """같은 폴더에 임시 파일을 완성한 뒤 교체하여 기존 HTML의 손상을 막습니다."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=f".{output.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="반려동물 돌봄 기록으로 '지금 상태' 페이지를 만듭니다.")
    parser.add_argument("--schedule", help="일정 탭 CSV 파일 경로 또는 웹에 게시한 CSV 주소")
    parser.add_argument("--log", help="기록 탭(폼 응답) CSV 파일 경로 또는 웹에 게시한 CSV 주소")
    parser.add_argument("--output", default="index.html", type=Path, help="출력 HTML 경로")
    args = parser.parse_args(argv)

    schedule_source = args.schedule or os.environ.get("SCHEDULE_CSV_URL", "").strip() or str(SAMPLE_SCHEDULE)
    log_source = args.log or os.environ.get("LOG_CSV_URL", "").strip() or str(SAMPLE_LOG)
    form_url = os.environ.get("FORM_URL", "").strip()
    is_url = lambda s: bool(re.match(r"https?://", s))
    using_sample = Path(schedule_source) == SAMPLE_SCHEDULE and Path(log_source) == SAMPLE_LOG
    try:
        if form_url and not is_url(form_url):
            raise DataError(f"FORM_URL은 http:// 또는 https://로 시작해야 합니다: {form_url}")
        generated_at = datetime.now(KST)
        site = load_site()
        schedule_text = read_source(schedule_source)
        schedule, paused = parse_schedule(schedule_text)
        logs = parse_log(read_source(log_source))
        unknown = unknown_items(schedule_text, logs)
        if using_sample:
            shift_sample(schedule, logs, generated_at.date())
        if is_url(schedule_source) and is_url(log_source):
            source_label = "구글 시트(웹에 게시한 CSV)"
        elif using_sample:
            source_label = "샘플 CSV(날짜를 오늘로 옮김)"
        else:
            source_label = f"로컬 파일 {Path(schedule_source).name} · {Path(log_source).name}"
        urls = {"schedule": schedule_source if is_url(schedule_source) else "",
                "log": log_source if is_url(log_source) else "", "form": form_url}
        config = build_config(site, schedule, logs, urls, generated_at, source_label)
        atomic_write(args.output, generate_html(config))
    except (OSError, ValueError) as error:
        print(f"생성 실패: {error}", file=sys.stderr)
        return 1
    print(f"데이터: {source_label}")
    print(f"사용 중인 일정 {len(schedule)}개 · 멈춘 일정 {paused}개 · 기록 {len(logs)}개")
    for log in unknown:
        print(f"경고: 기록 {log['row']}행의 항목 '{log['pet']} / {log['item']}'이(가) 일정 탭에 없습니다. 이름이 같은지 확인하세요.")
    print(f"HTML 생성 완료: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
