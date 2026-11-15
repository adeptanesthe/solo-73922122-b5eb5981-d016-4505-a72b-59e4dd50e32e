"""核心业务逻辑：报告入库（幂等 / 冲突拒收 / 原子）与截止时刻单季营收查询。

事务语义：
- 入库：BEGIN IMMEDIATE 一开始就拿到写锁，校验与写入在同一事务内完成，
  任何冲突 → ROLLBACK，不留下部分事实；并发上传被串行化。
- 查询：单个 BEGIN ... COMMIT 读事务内完成四季取数，WAL 下读事务持有
  开始时刻的已提交快照，另一窗口同时上传不会混入本次查询。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .csv_import import ParsedReport
from .timeutil import utc_now


class ConflictError(Exception):
    """业务冲突（对应 HTTP 409），整份拒收。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def store_report(conn: sqlite3.Connection, report: ParsedReport) -> dict[str, Any]:
    """写入一份报告。幂等：同内容重传返回 unchanged；冲突：抛 ConflictError。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id, content_hash FROM reports WHERE company = ? AND report_no = ?",
            (report.company, report.report_no),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == report.content_hash:
                conn.execute("COMMIT")
                return {
                    "status": "unchanged",
                    "message": "同一报告已存在，内容一致，未重复写入",
                    "report_id": existing["id"],
                    "report_no": report.report_no,
                }
            raise ConflictError(
                "report_no_conflict",
                f"报告号 {report.report_no!r}（公司 {report.company!r}）已存在，"
                f"但本次上传内容不一致，整份拒收",
            )

        # 校验同季同披露时刻的值冲突，并识别可复用的既有事实
        conflicts: list[str] = []
        reuse: set[int] = set()
        for r in report.rows:
            hit = conn.execute(
                "SELECT cumulative_revenue FROM facts "
                "WHERE company = ? AND fiscal_year = ? AND quarter = ? AND disclosed_at = ?",
                (report.company, report.fiscal_year, r.quarter, report.disclosed_at),
            ).fetchone()
            if hit is None:
                continue
            if hit["cumulative_revenue"] != r.cumulative_revenue:
                conflicts.append(
                    f"Q{r.quarter} 在 {report.disclosed_at} 已有披露值 "
                    f"{hit['cumulative_revenue']} 元，本次为 {r.cumulative_revenue} 元"
                )
            else:
                reuse.add(r.quarter)
        if conflicts:
            raise ConflictError(
                "fact_conflict",
                "同一季度同一披露时刻出现不同数值，整份拒收：" + "；".join(conflicts),
            )

        report_id = conn.execute(
            "INSERT INTO reports (company, fiscal_year, report_no, disclosed_at, "
            "content_hash, row_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report.company, report.fiscal_year, report.report_no, report.disclosed_at,
             report.content_hash, len(report.rows), utc_now()),
        ).lastrowid
        inserted = 0
        for r in report.rows:
            if r.quarter in reuse:
                continue  # 同一披露事件已存在且数值一致，不重复写事实
            conn.execute(
                "INSERT INTO facts (report_id, company, fiscal_year, quarter, "
                "disclosed_at, cumulative_revenue) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, report.company, report.fiscal_year, r.quarter,
                 report.disclosed_at, r.cumulative_revenue),
            )
            inserted += 1
        conn.execute("COMMIT")
        return {
            "status": "created",
            "message": "报告已接收",
            "report_id": report_id,
            "report_no": report.report_no,
            "inserted_facts": inserted,
            "reused_facts": len(reuse),
        }
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _visible_facts(conn: sqlite3.Connection, company: str, fiscal_year: int,
                   cutoff: str) -> dict[int, dict[str, Any]]:
    """在调用方持有的事务内，取四季各自不晚于 cutoff 的最近披露累计数。"""
    visible: dict[int, dict[str, Any]] = {}
    for q in (1, 2, 3, 4):
        row = conn.execute(
            "SELECT f.quarter, f.cumulative_revenue, f.disclosed_at, "
            "       r.report_no, r.id AS report_id "
            "FROM facts f JOIN reports r ON r.id = f.report_id "
            "WHERE f.company = ? AND f.fiscal_year = ? AND f.quarter = ? "
            "  AND f.disclosed_at <= ? "
            "ORDER BY f.disclosed_at DESC, f.id DESC LIMIT 1",
            (company, fiscal_year, q, cutoff),
        ).fetchone()
        if row is not None:
            visible[q] = dict(row)
    return visible


def single_quarter_view(conn: sqlite3.Connection, company: str, fiscal_year: int,
                        cutoff: str) -> dict[str, Any]:
    """按披露截止时刻核算四季单季营收。

    第一季直接取可见累计数；其余季度 = 本季可见累计 - 上季可见累计；
    本季无数据 → no_data；缺上季 → not_computable。
    四季取数在同一个读事务（同一已提交快照）内完成。
    """
    conn.execute("BEGIN")
    try:
        visible = _visible_facts(conn, company, fiscal_year, cutoff)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    quarters: list[dict[str, Any]] = []
    for q in (1, 2, 3, 4):
        cur = visible.get(q)
        prev = visible.get(q - 1)
        if cur is None:
            status, value = "no_data", None
        elif q == 1:
            status, value = "ok", cur["cumulative_revenue"]
        elif prev is None:
            status, value = "not_computable", None
        else:
            status, value = "ok", cur["cumulative_revenue"] - prev["cumulative_revenue"]

        def src(fact: dict[str, Any] | None) -> dict[str, Any] | None:
            if fact is None:
                return None
            return {
                "report_no": fact["report_no"],
                "disclosed_at": fact["disclosed_at"],
                "cumulative_revenue": fact["cumulative_revenue"],
            }

        quarters.append({
            "quarter": q,
            "status": status,
            "single_quarter_revenue": value,
            "cumulative_source": src(cur),
            "prev_cumulative_source": src(prev) if q > 1 else None,
        })

    return {
        "company": company,
        "fiscal_year": fiscal_year,
        "cutoff": cutoff,
        "quarters": quarters,
    }


def list_reports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.id, r.company, r.fiscal_year, r.report_no, r.disclosed_at, "
        "       r.row_count, r.created_at, COUNT(f.id) AS fact_count "
        "FROM reports r LEFT JOIN facts f ON f.report_id = r.id "
        "GROUP BY r.id ORDER BY r.disclosed_at DESC, r.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_facts(conn: sqlite3.Connection, company: str | None,
               fiscal_year: int | None) -> list[dict[str, Any]]:
    sql = ("SELECT f.company, f.fiscal_year, f.quarter, f.disclosed_at, "
           "       f.cumulative_revenue, r.report_no "
           "FROM facts f JOIN reports r ON r.id = f.report_id")
    cond, params = [], []
    if company:
        cond.append("f.company = ?")
        params.append(company)
    if fiscal_year is not None:
        cond.append("f.fiscal_year = ?")
        params.append(fiscal_year)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY f.disclosed_at, f.quarter"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_companies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT company, fiscal_year FROM reports ORDER BY company, fiscal_year"
    ).fetchall()
    return [dict(r) for r in rows]
