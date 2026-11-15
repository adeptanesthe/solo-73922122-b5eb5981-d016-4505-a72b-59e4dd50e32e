"""上传 CSV 的解析与校验。

CSV 每行对应一个季度，整份文件必须属于同一 公司 / 财年 / 披露时刻 / 报告号。
必需表头（支持中英文别名）：
    company, fiscal_year, disclosed_at, report_no, quarter, cumulative_revenue
金额必须为整数元（不允许小数）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass

from .timeutil import normalize_moment

HEADER_ALIASES = {
    "company": "company",
    "公司": "company",
    "fiscal_year": "fiscal_year",
    "财年": "fiscal_year",
    "会计年度": "fiscal_year",
    "disclosed_at": "disclosed_at",
    "披露时刻": "disclosed_at",
    "披露时间": "disclosed_at",
    "report_no": "report_no",
    "报告号": "report_no",
    "报告编号": "report_no",
    "quarter": "quarter",
    "季度": "quarter",
    "cumulative_revenue": "cumulative_revenue",
    "累计营收": "cumulative_revenue",
    "累计营收(元)": "cumulative_revenue",
    "累计营收（元）": "cumulative_revenue",
}

REQUIRED_FIELDS = {"company", "fiscal_year", "disclosed_at", "report_no", "quarter", "cumulative_revenue"}

INT_RE = re.compile(r"^[+-]?\d+$")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_LEN = 200


class CsvError(ValueError):
    """CSV 内容不合法（对应 HTTP 422）。"""


@dataclass(frozen=True)
class ReportRow:
    quarter: int
    cumulative_revenue: int


@dataclass(frozen=True)
class ParsedReport:
    company: str
    fiscal_year: int
    disclosed_at: str  # 规范化 UTC ISO
    report_no: str
    rows: tuple[ReportRow, ...]  # 按 quarter 升序
    content_hash: str


def _content_hash(company: str, fiscal_year: int, disclosed_at: str,
                  report_no: str, rows: tuple[ReportRow, ...]) -> str:
    h = hashlib.sha256()
    h.update(f"{company}\n{fiscal_year}\n{disclosed_at}\n{report_no}\n".encode("utf-8"))
    for r in rows:
        h.update(f"{r.quarter}={r.cumulative_revenue}\n".encode("utf-8"))
    return h.hexdigest()


def parse_report_csv(data: bytes) -> ParsedReport:
    if len(data) > MAX_FILE_BYTES:
        raise CsvError("文件过大（上限 2MB）")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("文件必须是 UTF-8 编码") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise CsvError("空文件：缺少表头") from exc

    header: list[str] = []
    for cell in raw_header:
        key = HEADER_ALIASES.get(cell.strip().lower())
        if key is None:
            raise CsvError(f"无法识别的表头列: {cell.strip()!r}，"
                           f"需要列: {sorted(REQUIRED_FIELDS)}")
        if key in header:
            raise CsvError(f"表头列重复: {cell.strip()!r}")
        header.append(key)
    missing = REQUIRED_FIELDS - set(header)
    if missing:
        raise CsvError(f"缺少必需列: {sorted(missing)}")

    idx = {name: header.index(name) for name in REQUIRED_FIELDS}
    companies: set[str] = set()
    years: set[int] = set()
    moments: set[str] = set()
    report_nos: set[str] = set()
    rows: list[ReportRow] = []
    seen_quarters: set[int] = set()

    for lineno, record in enumerate(reader, start=2):
        if not record or all(not c.strip() for c in record):
            continue  # 跳过空行
        if len(record) != len(header):
            raise CsvError(f"第 {lineno} 行列数({len(record)})与表头({len(header)})不一致")

        company = record[idx["company"]].strip()
        report_no = record[idx["report_no"]].strip()
        if not company or not report_no:
            raise CsvError(f"第 {lineno} 行：公司或报告号为空")
        if len(company) > MAX_TEXT_LEN or len(report_no) > MAX_TEXT_LEN:
            raise CsvError(f"第 {lineno} 行：公司或报告号超长（>{MAX_TEXT_LEN} 字符）")

        fy_raw = record[idx["fiscal_year"]].strip()
        if not INT_RE.match(fy_raw):
            raise CsvError(f"第 {lineno} 行：财年 {fy_raw!r} 不是整数")
        fiscal_year = int(fy_raw)
        if not (1900 <= fiscal_year <= 2200):
            raise CsvError(f"第 {lineno} 行：财年 {fiscal_year} 超出合理范围")

        try:
            moment = normalize_moment(record[idx["disclosed_at"]])
        except ValueError as exc:
            raise CsvError(f"第 {lineno} 行：披露时刻 "
                           f"{record[idx['disclosed_at']].strip()!r} 不是合法 ISO-8601 时间") from exc

        q_raw = record[idx["quarter"]].strip()
        if not INT_RE.match(q_raw):
            raise CsvError(f"第 {lineno} 行：季度 {q_raw!r} 不是整数")
        quarter = int(q_raw)
        if quarter not in (1, 2, 3, 4):
            raise CsvError(f"第 {lineno} 行：季度必须为 1-4，得到 {quarter}")
        if quarter in seen_quarters:
            raise CsvError(f"第 {lineno} 行：季度 {quarter} 在文件内重复")
        seen_quarters.add(quarter)

        amount_raw = record[idx["cumulative_revenue"]].strip()
        if not INT_RE.match(amount_raw):
            raise CsvError(f"第 {lineno} 行：累计营收 {amount_raw!r} 不是整数元")
        amount = int(amount_raw)

        companies.add(company)
        years.add(fiscal_year)
        moments.add(moment)
        report_nos.add(report_no)
        rows.append(ReportRow(quarter=quarter, cumulative_revenue=amount))

    if not rows:
        raise CsvError("文件没有任何数据行")
    if len(companies) != 1 or len(years) != 1 or len(moments) != 1 or len(report_nos) != 1:
        raise CsvError("一份 CSV 必须对应同一公司、同一财年、同一披露时刻、同一报告号；"
                       "检测到文件内元数据不一致")

    rows.sort(key=lambda r: r.quarter)
    parsed_rows = tuple(rows)
    company = companies.pop()
    year = years.pop()
    moment = moments.pop()
    report_no = report_nos.pop()
    return ParsedReport(
        company=company,
        fiscal_year=year,
        disclosed_at=moment,
        report_no=report_no,
        rows=parsed_rows,
        content_hash=_content_hash(company, year, moment, report_no, parsed_rows),
    )
