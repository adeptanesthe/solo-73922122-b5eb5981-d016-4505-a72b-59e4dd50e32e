# 单季营收披露时点核算（FastAPI + SQLite）

为历史回测准备单季营收：严格按**当时已披露**的数据核算，后来的重述不会泄漏进旧时点。
上传同一公司、财年、披露时刻、报告号的累计营收 CSV，按披露截止时刻查看四季单季营收及来源。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据持久化在名为 `appdata` 的卷中（容器内路径 `/data/app.db`，可用环境变量
`APP_DB_PATH` 覆盖）。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# 打开 http://127.0.0.1:8000
```

## 运行测试

```bash
pip install pytest
python -m pytest tests/ -q
```

## CSV 格式

每行对应一个季度，整份文件必须属于**同一公司 / 财年 / 披露时刻 / 报告号**，金额为整数元：

```csv
company,fiscal_year,disclosed_at,report_no,quarter,cumulative_revenue
示例科技,2023,2023-08-30T10:00:00Z,FY2023-H1,2,250000000
```

- 表头支持中文别名：`公司,财年,披露时刻,报告号,季度,累计营收`
- `disclosed_at`：ISO-8601，不带时区按 UTC 处理；存储规范化为 UTC，比较按时刻先后
- `quarter`：1–4，文件内不得重复；`cumulative_revenue`：整数元（不接受小数）
- 样例文件见 `samples/`（含一次 Q1 重述的完整演示序列）

## 核算规则

给定披露截止时刻 T（不晚于 T，即 `disclosed_at <= T`）：

1. 每季取**不晚于 T 最近披露**的累计数及其来源（报告号 + 披露时刻）；
2. Q1 单季 = 其可见累计数；
3. Q2–Q4 单季 = 本季可见累计 − 上季可见累计；缺前季则该季标为**不可算**，本季无披露标为**无数据**；
4. 前季重述是一条披露时刻更晚的新事实，只会改变其披露之后的时点：旧截止时刻仍取旧值，
   新截止时刻取重述值，相邻季随之改变（如 Q1 重述会改变 Q2 单季）。

## 入库规则（幂等 / 拒收 / 原子）

- **同一报告重传不重复**：按（公司, 报告号）定位，内容哈希一致 → 返回 `unchanged`，不写重复事实；
- **报告号异内容**：同一（公司, 报告号）已存在但内容哈希不同 → 409 整份拒收；
- **同季同披露时刻异值**：与既有事实冲突 → 409 整份拒收（同值则复用既有事实，不重复入库）；
- **失败不留下部分事实**：校验与写入在同一事务（`BEGIN IMMEDIATE` … `COMMIT`）内，
  任何冲突整体 `ROLLBACK`。

## 并发一致性

- 上传：`BEGIN IMMEDIATE` 使校验+写入串行化，并发冲突上传恰好一份成功；
- 查询：四季取数在**同一个读事务**内完成；WAL 模式下读事务持有开始时刻的已提交快照，
  另一窗口上传期间，一次查询的四季必然来自同一已提交视图（要么整份可见、要么整份不可见），
  且读写互不阻塞。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 操作网页 |
| POST | `/api/reports` | 上传 CSV（multipart 字段名 `file`）→ `created` / `unchanged` / 409 / 422 |
| GET | `/api/single-quarter?company=&fiscal_year=&cutoff=` | 四季单季营收、状态与来源 |
| GET | `/api/reports` | 已接收报告列表 |
| GET | `/api/facts?company=&fiscal_year=` | 披露事实流水 |
| GET | `/api/companies` | 已知公司/财年 |

交互式文档：`/docs`。

## 目录结构

```
app/
  main.py        FastAPI 路由
  service.py     入库与核算核心逻辑（事务边界）
  csv_import.py  CSV 解析校验 + 内容哈希
  db.py          SQLite 连接（WAL、显式事务）与 schema
  timeutil.py    时刻解析与 UTC 规范化
  static/index.html  单页操作界面
samples/         演示用 CSV（含重述场景）
tests/           API 语义测试 + 并发一致性测试
Dockerfile / docker-compose.yml
```
