"""FastAPI 入口：网页 + REST API。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from . import db as dbmod
from .csv_import import CsvError, parse_report_csv
from .service import (ConflictError, list_companies, list_facts, list_reports,
                      single_quarter_view, store_report)
from .timeutil import normalize_moment

STATIC_DIR = Path(__file__).parent / "static"


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or dbmod.path_from_env()
    init_conn = dbmod.connect(path)
    try:
        dbmod.init_db(init_conn)
    finally:
        init_conn.close()

    app = FastAPI(title="单季营收披露时点核算", version="1.0.0")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = dbmod.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/reports")
    async def upload_report(file: UploadFile = File(...),
                            conn: sqlite3.Connection = Depends(get_conn)):
        data = await file.read()
        try:
            report = parse_report_csv(data)
        except CsvError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return store_report(conn, report)
        except ConflictError as exc:
            raise HTTPException(status_code=409,
                                detail={"code": exc.code, "message": exc.message}) from exc

    @app.get("/api/single-quarter")
    def single_quarter(company: str = Query(...),
                       fiscal_year: int = Query(...),
                       cutoff: str = Query(..., description="ISO-8601 披露截止时刻"),
                       conn: sqlite3.Connection = Depends(get_conn)):
        try:
            normalized_cutoff = normalize_moment(cutoff)
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail=f"cutoff 不是合法 ISO-8601 时刻: {cutoff!r}") from exc
        return single_quarter_view(conn, company, fiscal_year, normalized_cutoff)

    @app.get("/api/reports")
    def reports(conn: sqlite3.Connection = Depends(get_conn)):
        return {"reports": list_reports(conn)}

    @app.get("/api/facts")
    def facts(company: str | None = None, fiscal_year: int | None = None,
              conn: sqlite3.Connection = Depends(get_conn)):
        return {"facts": list_facts(conn, company, fiscal_year)}

    @app.get("/api/companies")
    def companies(conn: sqlite3.Connection = Depends(get_conn)):
        return {"companies": list_companies(conn)}

    return app


app = create_app()
