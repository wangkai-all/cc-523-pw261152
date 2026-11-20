# 特征漂移监控 · Feature Drift Monitor

按**人群（segment）× 时间窗口（window）× 业务版本（business version）**
对特征分布、缺失率和异常值进行监控，支持**版本化基线**、**可配置告警策略**、
**数据修订后仅重评受影响窗口**，并通过 Web 页面展示分布对比、告警生命周期、
证据样本和历史回溯结果。

整个项目只依赖 **Python 3.11 标准库**（`sqlite3` + `http.server`），前端是
零依赖的原生 HTML/CSS/JS，无需 `npm install` / `pip install` 即可运行。

## 快速开始

```bash
# 1. 初始化一个覆盖全部能力的演示场景（漂移 / 空分群 / 修订 / 回溯）
python -m drift_monitor.demo --db demo.db

# 2. 启动 API + 仪表盘
python -m drift_monitor.api --db demo.db --port 8080
# 打开 http://localhost:8080

# 或一步到位：播种后直接起服务
python -m drift_monitor.demo --db demo.db --serve 8080
```

## 目录结构

```
drift_monitor/
├── schema.py      # SQLite 表结构：版本化、只追加、受影响窗口标记
├── metrics.py     # 纯统计函数：PSI、JS 散度、分位数、IQR 异常值、直方图
├── store.py       # 维度/摄入/修订/窗口物化/基线/策略 的持久化与领域逻辑
├── engine.py      # 评估引擎：告警生命周期、脏窗口重评、历史回溯
├── api.py         # 标准库 HTTP 服务（JSON API + 静态仪表盘）
├── demo.py        # 端到端演示数据与流程
└── web/           # index.html / styles.css / app.js（原生 SVG 图表）
tests/             # 34 个 unittest（同时兼容 pytest）
```

## 核心设计与需求对应

### 1. 三维度比较：人群 × 时间 × 业务版本
每条观测带 `segment_id / business_version_id / window_start`，所有统计和
告警都在这三元组上唯一确定。页面的四个下拉框切换任一字段即重新拉取
`/api/distribution`，实现**时间切换、分群切换、版本切换**（见
`tests/test_store_flow.py::TimeSwitchTests`）。

### 2. 分布、缺失率、异常值
* 数值特征：均值/标准差/分位数/IQR 围栏，基于**基线围栏**统计异常值；
  PSI 与 JS 散度都在**基线直方图的同一组 bin 边界**上计算（共享支撑集）。
* 类别特征：类别计数 + PSI/JS，自动对齐基线中不存在的新类别。
* 缺失率作为独立指标，同时参与告警（缺失率恶化方向才触发）。

### 3. 基线更新生成新版本，不重写历史
`baselines` 表对 `(feature, segment, business_version, version)` 唯一，
每次 `create_baseline` 都插入新版本号并把旧版本置为 `active=0`，**旧行
永不更新或删除**。告警通过 `baseline_id` 与具体基线版本绑定，新基线
不会改变历史告警（`tests/test_lifecycle.py::BaselineVersioningTests`）。

### 4. 告警生命周期只追加
`alerts` 保存不可变身份（特征/分群/版本/窗口/基线五要素唯一），所有
状态迁移（created → updated → acknowledged → resolved / reopened /
suppressed / backfilled）追加到 `alert_events`，没有任何删除或覆盖。
重复评估不会产生重复告警；窗口恢复时告警自动 `resolved` 并保留全量记录。

### 5. 数据修订只重评受影响窗口
* 修订走 `revisions` 新修订号：旧观测置 `superseded_by`（**保留而非删除**，
  可审计），新观测以新修订号写入。
* 只有被修订的窗口在 `window_states.dirty=1`，`evaluate(only_dirty=True)`
  只处理这些窗口，评估审计行 `evaluation_runs.kind='revision'` 记录实际
  处理窗口数（测试断言为 1，未触碰窗口的告警事件保持只有 `created`）。

### 6. 空分群处理
窗口没有任何有效观测时记录 `empty_window=1`，引擎生成 `info` 级告警而
**不会误报漂移**，重复评估不重复告警；无基线时空人群告警同样成立
（`baseline_id` 可空，仅用于 info 告警）。页面显示"分群为空"徽标与说明。

### 7. 回溯（backfill）
新基线产生后可对历史窗口区间重算，回溯告警打 `is_backfill=1`；同一基线
重复回溯不会产生重复告警，原有历史告警原样保留。

### 8. 告警策略
`alert_policies` 同样版本化（旧策略保留、置 inactive），支持按
特征/分群/业务版本设定作用域，最具体匹配优先；阈值包括 PSI、JS 散度、
缺失率/异常值率增量、均值 Z 值、最小样本量、空窗口通知开关。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/dimensions` | 特征/分群/业务版本/修订号 |
| GET | `/api/windows?dirty=1` | 窗口状态（空窗口、脏标记） |
| POST | `/api/ingest` | 新修订号摄入一批观测 |
| POST | `/api/revise` | 修订：旧行 supersede + 仅相关窗口置脏 |
| GET | `/api/distribution?feature=&segment=&business_version=&window=` | 分布对比 + 漂移指标 |
| POST | `/api/baselines` | 生成**新版本**基线 |
| GET | `/api/baselines` | 基线版本列表（含 archived） |
| POST | `/api/policies` | 新版本告警策略 |
| POST | `/api/evaluate` | 评估（`only_dirty` 支持受影响窗口重评） |
| POST | `/api/backfill` | 历史窗口回溯 |
| GET | `/api/alerts` | 告警列表（可按状态/维度过滤） |
| GET | `/api/alerts/{id}` | 告警详情含证据样本 |
| GET | `/api/alerts/{id}/events` | 生命周期事件时间线 |
| PATCH | `/api/alerts/{id}` | `acknowledge` / `suppress` |
| GET | `/api/runs` | 评估/修订/回溯审计记录 |

## 运行测试

```bash
python -m unittest discover -s tests -v   # 标准库，无需安装
# 或
pytest tests/                             # 同样兼容
```

34 个测试覆盖：统计函数、摄入与脏标记、空分群、时间切换、类别漂移、
基线版本化、告警生命周期只追加、修订受影响窗口隔离、回溯幂等、
策略版本化与匹配，以及 8 个真实 HTTP 端到端用例（含并发连接安全）。

## 关键实现备注

* SQLite 连接以 `check_same_thread=False` + `RLock` 串行化，供线程化
  HTTP 服务安全共享。
* PSI 只对**结构性零 bin** 做半伪计数平滑，避免对小样本窗口中非零
  小比例的 epsilon 地板造成指标虚高；小样本场景优先参考有界的 JS 散度。
* 图表配色经过 dataviz 调色板校验（CVD 安全），并提供表格视图作为
  对比度不足色槽的无障碍补偿，支持明暗双主题。
