"""端到端 API 测试：核算语义、幂等、冲突拒收、原子性。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

CSV_HEADER = "company,fiscal_year,disclosed_at,report_no,quarter,cumulative_revenue\n"


def csv_text(rows, header=CSV_HEADER):
    return header + "".join(",".join(str(c) for c in r) + "\n" for r in rows)


def q1(company="示例科技", year=2023, moment="2023-04-28T10:00:00Z",
       no="FY2023-Q1", amount=100_000_000):
    return csv_text([(company, year, moment, no, 1, amount)])


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, text):
        return self.client.post(
            "/api/reports",
            files={"file": ("report.csv", text.encode("utf-8"), "text/csv")},
        )

    def query(self, company, year, cutoff):
        resp = self.client.get("/api/single-quarter", params={
            "company": company, "fiscal_year": year, "cutoff": cutoff})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def seed_full_year(self):
        """Q1=1亿, H1累计=2.5亿, Q3累计=4.2亿, 全年累计=6亿。"""
        assert self.upload(q1()).json()["status"] == "created"
        assert self.upload(csv_text([("示例科技", 2023, "2023-08-30T10:00:00Z",
                                      "FY2023-H1", 2, 250_000_000)])).json()["status"] == "created"
        assert self.upload(csv_text([("示例科技", 2023, "2023-10-30T10:00:00Z",
                                      "FY2023-Q3", 3, 420_000_000)])).json()["status"] == "created"
        assert self.upload(csv_text([("示例科技", 2023, "2024-04-25T10:00:00Z",
                                      "FY2023-AR", 4, 600_000_000)])).json()["status"] == "created"


class TestSingleQuarter(ApiTestBase):
    def test_full_year_values_and_sources(self):
        self.seed_full_year()
        data = self.query("示例科技", 2023, "2024-12-31T23:59:59Z")
        values = {q["quarter"]: q["single_quarter_revenue"] for q in data["quarters"]}
        self.assertEqual(values, {1: 100_000_000, 2: 150_000_000,
                                  3: 170_000_000, 4: 180_000_000})
        for q in data["quarters"]:
            self.assertEqual(q["status"], "ok")
        self.assertEqual(data["quarters"][1]["cumulative_source"]["report_no"], "FY2023-H1")
        self.assertEqual(data["quarters"][1]["prev_cumulative_source"]["report_no"], "FY2023-Q1")

    def test_cutoff_selects_latest_visible_disclosure(self):
        self.seed_full_year()
        # 截止在半年报之前：只有 Q1 可见
        data = self.query("示例科技", 2023, "2023-06-01T00:00:00Z")
        st = {q["quarter"]: q["status"] for q in data["quarters"]}
        self.assertEqual(st, {1: "ok", 2: "no_data", 3: "no_data", 4: "no_data"})
        self.assertEqual(data["quarters"][0]["single_quarter_revenue"], 100_000_000)

    def test_cutoff_boundary_is_inclusive(self):
        self.seed_full_year()
        # 恰好在披露时刻（不晚于 = <=）应可见
        data = self.query("示例科技", 2023, "2023-08-30T10:00:00Z")
        self.assertEqual(data["quarters"][1]["status"], "ok")
        self.assertEqual(data["quarters"][1]["single_quarter_revenue"], 150_000_000)
        # 早 1 微秒则不可见
        data = self.query("示例科技", 2023, "2023-08-30T10:00:00Z".replace("10:00:00", "09:59:59"))
        self.assertEqual(data["quarters"][1]["status"], "no_data")

    def test_missing_prev_quarter_not_computable(self):
        # 只披露 Q2 累计，缺 Q1 → Q2 不可算
        self.upload(csv_text([("示例科技", 2023, "2023-08-30T10:00:00Z",
                               "FY2023-H1", 2, 250_000_000)]))
        data = self.query("示例科技", 2023, "2023-12-31T00:00:00Z")
        self.assertEqual(data["quarters"][0]["status"], "no_data")
        self.assertEqual(data["quarters"][1]["status"], "not_computable")
        self.assertIsNone(data["quarters"][1]["single_quarter_revenue"])

    def test_restatement_changes_adjacent_quarter_only_after_its_moment(self):
        self.seed_full_year()
        # 2024-01-15 重述 Q1 累计为 9500 万
        self.upload(csv_text([("示例科技", 2023, "2024-01-15T09:00:00Z",
                               "FY2023-Q1-RESTATE", 1, 95_000_000)]))

        before = self.query("示例科技", 2023, "2024-01-14T00:00:00Z")
        self.assertEqual(before["quarters"][0]["single_quarter_revenue"], 100_000_000)
        self.assertEqual(before["quarters"][1]["single_quarter_revenue"], 150_000_000)

        after = self.query("示例科技", 2023, "2024-01-16T00:00:00Z")
        self.assertEqual(after["quarters"][0]["single_quarter_revenue"], 95_000_000)
        # 前季重述改变相邻季：Q2 = 2.5亿 - 0.95亿
        self.assertEqual(after["quarters"][1]["single_quarter_revenue"], 155_000_000)
        self.assertEqual(after["quarters"][1]["prev_cumulative_source"]["report_no"],
                         "FY2023-Q1-RESTATE")
        # Q3 不受 Q1 重述影响（Q3 = Q3累计 - Q2累计）
        self.assertEqual(after["quarters"][2]["single_quarter_revenue"], 170_000_000)


class TestUploadSemantics(ApiTestBase):
    def test_idempotent_reupload(self):
        self.assertEqual(self.upload(q1()).json()["status"], "created")
        again = self.upload(q1())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["status"], "unchanged")
        facts = self.client.get("/api/facts").json()["facts"]
        self.assertEqual(len(facts), 1, "重传不得产生重复事实")
        reports = self.client.get("/api/reports").json()["reports"]
        self.assertEqual(len(reports), 1)

    def test_same_report_no_different_content_rejected(self):
        self.upload(q1())
        conflict = q1(amount=120_000_000)  # 同报告号，金额不同
        resp = self.upload(conflict)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["code"], "report_no_conflict")
        # 不留下部分事实
        self.assertEqual(len(self.client.get("/api/facts").json()["facts"]), 1)
        self.assertEqual(len(self.client.get("/api/reports").json()["reports"]), 1)

    def test_same_moment_same_quarter_different_value_rejected(self):
        self.upload(q1())
        # 不同报告号，但同季同披露时刻异值
        other = q1(no="FY2023-Q1-DUP", amount=101_000_000)
        resp = self.upload(other)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["code"], "fact_conflict")
        self.assertEqual(len(self.client.get("/api/facts").json()["facts"]), 1)
        self.assertEqual(len(self.client.get("/api/reports").json()["reports"]), 1)

    def test_conflict_inside_multirow_report_is_atomic(self):
        self.upload(q1())
        # 两行：Q1 与既有值冲突，Q2 是新的 —— 整份拒收，Q2 也不得入库
        text = csv_text([
            ("示例科技", 2023, "2023-04-28T10:00:00Z", "FY2023-MIX", 1, 999),
            ("示例科技", 2023, "2023-04-28T10:00:00Z", "FY2023-MIX", 2, 250_000_000),
        ])
        resp = self.upload(text)
        self.assertEqual(resp.status_code, 409)
        facts = self.client.get("/api/facts").json()["facts"]
        self.assertEqual(len(facts), 1, "冲突上传不得留下任何部分事实")
        self.assertEqual(len(self.client.get("/api/reports").json()["reports"]), 1)

    def test_same_value_same_moment_dedupes_across_reports(self):
        self.upload(q1())
        # 不同报告号、同季同时刻同值 → 接收但复用事实，不重复
        resp = self.upload(q1(no="FY2023-Q1-AGAIN"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "created")
        self.assertEqual(body["inserted_facts"], 0)
        self.assertEqual(body["reused_facts"], 1)
        self.assertEqual(len(self.client.get("/api/facts").json()["facts"]), 1)
        self.assertEqual(len(self.client.get("/api/reports").json()["reports"]), 2)

    def test_multi_quarter_single_file(self):
        text = csv_text([
            ("示例科技", 2023, "2023-10-30T10:00:00Z", "FY2023-Q1Q2", 1, 100),
            ("示例科技", 2023, "2023-10-30T10:00:00Z", "FY2023-Q1Q2", 2, 250),
        ])
        self.assertEqual(self.upload(text).json()["status"], "created")
        data = self.query("示例科技", 2023, "2023-11-01T00:00:00Z")
        self.assertEqual(data["quarters"][0]["single_quarter_revenue"], 100)
        self.assertEqual(data["quarters"][1]["single_quarter_revenue"], 150)


class TestCsvValidation(ApiTestBase):
    def assert_422_and_clean(self, text):
        resp = self.upload(text)
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(len(self.client.get("/api/facts").json()["facts"]), 0)
        self.assertEqual(len(self.client.get("/api/reports").json()["reports"]), 0)

    def test_non_integer_amount_rejected(self):
        self.assert_422_and_clean(csv_text([("示例科技", 2023, "2023-04-28T10:00:00Z",
                                             "R1", 1, "100.5")]))

    def test_bad_quarter_rejected(self):
        self.assert_422_and_clean(csv_text([("示例科技", 2023, "2023-04-28T10:00:00Z",
                                             "R1", 5, 100)]))

    def test_duplicate_quarter_in_file_rejected(self):
        text = csv_text([
            ("示例科技", 2023, "2023-04-28T10:00:00Z", "R1", 1, 100),
            ("示例科技", 2023, "2024-04-28T10:00:00Z", "R1", 1, 100),
        ])
        # 元数据不一致（两个披露时刻）→ 422
        self.assert_422_and_clean(text)

    def test_mixed_metadata_rejected(self):
        text = csv_text([
            ("示例科技", 2023, "2023-04-28T10:00:00Z", "R1", 1, 100),
            ("示例科技", 2023, "2023-04-28T10:00:00Z", "R2", 2, 250),
        ])
        self.assert_422_and_clean(text)

    def test_bad_moment_rejected(self):
        self.assert_422_and_clean(csv_text([("示例科技", 2023, "not-a-time", "R1", 1, 100)]))

    def test_chinese_header_accepted(self):
        text = "公司,财年,披露时刻,报告号,季度,累计营收\n示例科技,2023,2023-04-28T10:00:00Z,R1,1,100\n"
        resp = self.upload(text)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "created")

    def test_bad_cutoff_param(self):
        resp = self.client.get("/api/single-quarter", params={
            "company": "x", "fiscal_year": 2023, "cutoff": "bad"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
