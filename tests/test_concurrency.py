"""并发一致性测试：另一窗口上传期间，一次查询的四季必须来自同一已提交视图。

直接对服务层起多线程（每线程独立连接同一文件库），模拟：
- 写线程：原子地一次上传"同一披露时刻的 Q1+Q2"报告，共 N 份；
- 读线程：持续做截止时刻查询。
断言每次查询返回的 Q1、Q2 可见事实都来自同一份报告（披露时刻一致），
即读到的永远是"整份报告已提交"或"整份未提交"，绝不出现半份报告。
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as dbmod  # noqa: E402
from app.csv_import import parse_report_csv  # noqa: E402
from app.service import ConflictError, single_quarter_view, store_report  # noqa: E402

COMPANY = "并发科技"
YEAR = 2024
N_REPORTS = 30
N_READERS = 4
READS_PER_READER = 200


def make_report(i):
    text = (
        "company,fiscal_year,disclosed_at,report_no,quarter,cumulative_revenue\n"
        f"{COMPANY},{YEAR},2024-05-01T00:{i:02d}:00Z,R{i},1,{1000 + i}\n"
        f"{COMPANY},{YEAR},2024-05-01T00:{i:02d}:00Z,R{i},2,{2000 + i}\n"
    )
    return parse_report_csv(text.encode("utf-8"))


class TestConcurrentSnapshot(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        conn = dbmod.connect(self.db_path)
        dbmod.init_db(conn)
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_readers_never_see_partial_report(self):
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(N_REPORTS):
                    conn = dbmod.connect(self.db_path)
                    try:
                        store_report(conn, make_report(i))
                    finally:
                        conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"writer: {exc!r}")
            finally:
                stop.set()

        def reader():
            try:
                for _ in range(READS_PER_READER):
                    if stop.is_set():
                        break
                    conn = dbmod.connect(self.db_path)
                    try:
                        view = single_quarter_view(
                            conn, COMPANY, YEAR, "2024-12-31T00:00:00.000000+00:00")
                    finally:
                        conn.close()
                    q1, q2 = view["quarters"][0], view["quarters"][1]
                    s1, s2 = q1["cumulative_source"], q2["cumulative_source"]
                    if (s1 is None) != (s2 is None):
                        errors.append(f"半份报告可见: q1={s1} q2={s2}")
                        return
                    if s1 and s1["disclosed_at"] != s2["disclosed_at"]:
                        errors.append(f"四季来自不同已提交视图: q1={s1} q2={s2}")
                        return
                    if s1:
                        # 单季值也必须与该快照内的累计值自洽
                        expect = s2["cumulative_revenue"] - s1["cumulative_revenue"]
                        if q2["single_quarter_revenue"] != expect:
                            errors.append("单季值与快照内累计值不自洽")
                            return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reader: {exc!r}")

        threads = [threading.Thread(target=writer)]
        threads += [threading.Thread(target=reader) for _ in range(N_READERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

        # 终态：N 份报告、2N 条事实，最终可见最新一份
        conn = dbmod.connect(self.db_path)
        try:
            view = single_quarter_view(conn, COMPANY, YEAR, "2024-12-31T00:00:00.000000+00:00")
            last = N_REPORTS - 1
            self.assertEqual(view["quarters"][0]["single_quarter_revenue"], 1000 + last)
            self.assertEqual(view["quarters"][1]["single_quarter_revenue"], 1000)
            n_facts = conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
            n_reports = conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"]
            self.assertEqual((n_facts, n_reports), (2 * N_REPORTS, N_REPORTS))
        finally:
            conn.close()

    def test_concurrent_conflicting_uploads_exactly_one_wins(self):
        report_a = parse_report_csv((
            "company,fiscal_year,disclosed_at,report_no,quarter,cumulative_revenue\n"
            f"{COMPANY},{YEAR},2024-05-01T00:00:00Z,SAME-NO,1,100\n"
        ).encode("utf-8"))
        report_b = parse_report_csv((
            "company,fiscal_year,disclosed_at,report_no,quarter,cumulative_revenue\n"
            f"{COMPANY},{YEAR},2024-05-01T00:00:00Z,SAME-NO,1,200\n"
        ).encode("utf-8"))

        results = {"created": 0, "unchanged": 0, "conflict": 0, "other": []}
        lock = threading.Lock()

        def upload(report):
            conn = dbmod.connect(self.db_path)
            try:
                outcome = store_report(conn, report)
                with lock:
                    results[outcome["status"]] += 1
            except ConflictError:
                with lock:
                    results["conflict"] += 1
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results["other"].append(repr(exc))
            finally:
                conn.close()

        threads = [threading.Thread(target=upload, args=(r,))
                   for r in [report_a, report_b] * 5]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results["other"], [])
        # 同报告号两份异内容并发：恰好一份先入库，其余 4 份异内容被拒、
        # 5 份同内容重传幂等返回 unchanged
        self.assertEqual(results["created"], 1, "同报告号异内容：只能有一份成功")
        self.assertEqual(results["unchanged"], 4)
        self.assertEqual(results["conflict"], 5)
        conn = dbmod.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
