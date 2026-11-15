# tsgrid · 多采样率遥测对齐与缺口修复

产线设备遥测统一到 **1 分钟网格**：定量估计设备时钟漂移、重采样（聚合 / hold 沿用）、
按**设备启用记录（在役区间）**对齐生命周期、判定并有限修复缺口、出小时摘要与覆盖率分级。

使用者：设备运维分析组。样例为产线 A 四台设备 6 小时数据（1s / 5s / 60s 三种采样率，
其中 `LINE-A-03` 时钟快 **+37 秒**；`LINE-A-04` 在批次中途 **02:00 投运、05:30 停用**，
停用后还有 4 条迟到记录）。

## 能力

| 模块 | 页面区块 | 端点 / 函数 |
|---|---|---|
| 数据导入 | 首页设备概览、工作台数据源 | `POST /api/import` · `app/telemetry.py` |
| 启用记录对齐 | 复核页「设备在役范围」、结果页在役/区间外标注 | `GET/POST /api/service-records` · `app/service.py` · `app/service_admin.py` |
| 漂移估计 | 结果页「一、时钟漂移估计」 | `app/drift.py` |
| 网格重采样 | 结果页「二、网格预览（前 60 分钟）」 | `app/grid.py` |
| 缺口判定 | 结果页「三、缺口明细」 | `app/gaps.py` |
| 摘要 | 结果页「四、小时摘要」 | `app/summary.py` |
| 质量报告 | `/review`、`/api/report(.csv)` | `app/quality.py` |

## 核心规则（全部后端真算，纯函数模块可直接单测）

1. **时钟漂移估计**：以「指标阈值穿越时刻」为锚点，先用最近邻滞后中位数粗对齐，
   再配对做最小二乘拟合 `t_ref = a · t_dev + b`。
   - 偏移 `offset = −b/a`（窗口起点，正值=设备表快），走时偏差 `(1/a − 1)·1e6` ppm；
   - 残差 `RMSE` 即「按 a、b 校正后」的剩余时间误差；
   - `|offset| > 阈值`（默认 5s）判定漂移并给出校正建议；
   - 只用相邻采样间隔 ≤ 90s 的区段取锚点，拟合后按 MAD 剔除跨拐点离群锚点（最多 3 轮）。
2. **统一网格（1 分钟桶）**：采样率高于桶（`1s`/`5s`）→ 桶内 `mean/max/min` + 计数（`agg`）；
   采样率低于桶或该段降级上报 → `hold` 沿用前值并标记 `carried=true`（计数）。
   hold 仅在「前后原始格相距 ≤ 3 格」且「前后采样点间距 > 1.5 桶」时生效——
   1s 设备偶尔丢一格不会被静默补值，长断采也不会被掩盖。
3. **缺口**：网格内无原始点且前后都有原始点 → `gap`；`≤3` 格线性插补、`>3` 格标记 `unrepaired`；
   序列**首尾不外推**（开头/结尾空缺只登记为未修复，绝不填值）。
4. **摘要**：每小时 `min/max/mean/p95` + 缺口格数 + carried 格数（p95 为线性插值分位数），
   只统计含在役桶的小时。
5. **质量分**：`coverage = 有效格 / 应到格`，**应到格 = 设备实际在役分钟桶数**
   （有效 = 原始 ∪ 沿用 ∪ 插补），分级 `≥0.98 A / ≥0.95 B / else C`；
   另给出 raw 覆盖率（不含沿用与插补）以便区分「有数据」和「补出来的数据」。
6. **启用记录（在役区间）对齐**：启用记录文件列固定为 `device_id,start_ts,end_ts`
   （`end_ts` 留空表示至今）。同一设备的多条记录**合并重叠/相接区间**后：
   - 分钟桶起点落在任一在役区间内才算在役；投运前 / 停用后的桶保留在网格里并标
     `in_service=false`，**不计缺口、不进覆盖率分母**，健康设备不会因生命周期外空白被降级；
   - 区间外原始点（停用后迟到 `late` / 投运前提前 `early`）**原样保留**在对应桶的
     `excluded_points` 中并标明不参与，hold / 插值 / 摘要都不会把曲线延伸到生命周期外；
   - 漂移估计只用在役时段的点配准。
7. **启用记录更正（原子切换）**：`POST /api/service-records` 提交更正 →
   合并区间 → 对整批「设备 × 指标」重算 → 整批一致性核对（应到格 == 在役桶数、
   覆盖率可由有效/应到复算、区间外点全部进 excluded 计数）→ 单个 SQLite 事务内
   写新版本、置 active、落报告快照；任一步失败事务回滚、旧版继续生效。
   重跑同一批（合并结果与 active 一致）不新增版本/报告；页面、JSON 与 CSV 共用同一份
   报告快照，在役范围与覆盖率口径完全一致。

## 启动

```bash
docker compose up -d --build
open http://127.0.0.1:18218/          # 或 curl http://127.0.0.1:18218/health
docker compose run --rm app python -m pytest -q
```

端口默认 `18218`，可用 `.env` 里的 `APP_PORT` 覆盖。SQLite 落在命名卷 `tsgrid-data:/app/data`；
启动时按内容 `sha256` 幂等 seed 样例数据，重复启动不产生重复数据集。
重建库：`docker compose down -v && docker compose up -d --build`。

## 演示步骤

1. `/` 首页看场景与设备概览（含每台设备的在役范围）→ 点「校验并载入样例遥测」（幂等，重复点不新增数据集）。
2. `/workbench` 选 `LINE-A-03` + 温度 + 阈值 5 → 「执行对齐」，页面显示 6 个阶段真实耗时与结论。
3. 结果页 `LINE-A-03` 温度：偏移 **≈ +37.01s**、走时 **≈ +55ppm**、校正后残差 RMSE **≈ 0.04s**、
   **2 处缺口**（3 格插补 / 5 格未修复）、覆盖率 98.61% → **A**。
4. 切 `LINE-A-02` 温度：3 分钟降级段被 hold 沿用（carried 30 格），覆盖率 95.28% → **B**。
5. 切 `LINE-A-03` 压力：01:40–02:05 断采，覆盖率 91.67% → **C**。
6. 选 `LINE-A-04` 温度：02:00 投运 / 05:30 停用，**应到 210 在役分钟**（网格共 360 格），
   停用后 4 条迟到记录全部标 `late` 保留不参与，无生命周期外缺口，覆盖率 97.62% → **B**。
7. `/review` 看四台设备 A/B/C 分级、在役范围与跨设备对比，导出 CSV（含 `in_service_cells`、
   `excluded_cells`、`late_points`、`service_ranges` 列）。
8. 调 `POST /api/service-records` 更正启用记录（请求体见 API 节），观察整批重算与原子切换；
   重复提交同一批不会产生新版本或新报告。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/import` | 幂等导入样例遥测，返回数据集/设备清单/启用版本 |
| POST | `/api/align` | `{device_id, metric, offset_threshold?}` → 对齐结果（含依据与阶段耗时） |
| GET | `/api/runs/{run_id}` | 取某次对齐结果 JSON |
| GET | `/api/service-records` | 当前生效启用版本、每台设备合并后的在役区间与版本历史 |
| POST | `/api/service-records` | `{records: [{device_id, start_ts, end_ts?}], note?}` 更正启用记录：整批重算、核对、原子切换，失败保留旧版；同批重跑不新增 |
| GET | `/api/report` | 全部「设备 × 指标」的质量报告 JSON（含在役范围与区间外点计数） |
| GET | `/api/report.csv` | 质量报告 CSV（`device_id,metric,...,in_service_cells,expected_cells,excluded_cells,late_points,...,service_ranges,...`） |
| GET | `/result/{run_id}` | 结果页（漂移依据、网格预览含区间外标注、缺口明细、小时摘要、覆盖率） |
| GET | `/`、`/workbench`、`/review` | 首页 / 工作台 / 质量报告 |

错误输入统一 4xx 中文提示：不存在的设备、设备没有该指标、阈值非数字或越界、请求体非 JSON。

## 界面（v3 工作台外壳）

左侧固定导航 + 顶部面包屑/操作条 + 独立滚动工作区 + 底部状态条（`app/static/shell.css`，与 `_shared/design/shell.css` 一致）。

| 页面 | 路由 | 形态 |
|---|---|---|
| 总览 | `/` | KPI 条（设备/指标/序列/最近对齐/平均覆盖率）+ 最近对齐表 + 设备概览 + 快捷操作；场景与步骤说明收进折叠 |
| 对齐工作台 | `/workbench` | 左侧 sticky「对齐任务」（数据集 / 设备·指标 / 漂移阈值 / 缺口策略 + 底部执行按钮），右侧按当前选择真算的**预演**（不落库）：漂移估计、网格预览、缺口判定、小时摘要 |
| 对齐结果 | `/result/{run_id}` | 头部结论 + KPI 条 + 分段详情（网格预览 / 漂移依据 / 缺口明细 / 小时摘要 / 覆盖率与耗时），长依据表在容器内滚动 |
| 质量复核 | `/review` | 工具栏（设备 / 指标 / 搜索 / 导出）+ 跨设备质量对比表 + 右侧 sticky 质量报告 |

侧栏「业务模块」6 个锚点对应流水线：数据导入 `#m1` → 漂移估计 `#m2` → 网格重采样 `#m3` → 缺口判定 `#m4` → 小时摘要 `#m5` → 质量报告 `/review#m6`。

## 目录

```text
app/
  main.py        FastAPI 路由（薄）
  settings.py    阈值/路径常量
  telemetry.py   数据导入、字段校验、采样率识别、设备清单、在役桶口径
  service.py     启用记录解析校验、重叠区间合并、在役桶/区间外点判定
  service_admin.py 更正编排：重算受影响设备 → 整批核对 → 原子切换/回滚
  drift.py       时钟漂移估计（锚点配对 + 鲁棒最小二乘，仅用在役点）
  grid.py        1 分钟网格重采样（聚合 / hold carried / 区间外标注）
  gaps.py        缺口判定与有限插补（首尾不外推，只看在役段）
  summary.py     小时摘要（min/max/mean/p95 + 缺口 + carried）
  quality.py     覆盖率、分级、跨设备汇总、CSV
  pipeline.py    6 步编排 + 阶段耗时（在役区间贯穿全链路）
  store.py       SQLite（数据集、运行结果、启用版本、报告快照、seed 版本）
  seed.py        幂等 seed（遥测 + 启用记录首版引导）
  templates/     index / workbench / result / review
  static/        app.js、style.css、shell.css（原生，无框架无 CDN）
sample_data/telemetry/  4 台设备 6 小时 CSV + service_records.csv + _generate.py（确定性生成脚本）
tests/                  test_drift / test_resample / test_gaps / test_service / test_service_api / test_api
```

## 已知限制

- 漂移估计依赖指标存在周期性变化；平坦序列无法取锚点，会明确报错而不是给 0。
- 锚点按「阈值穿越」配对，若设备时钟偏移超过 ±300s（`DRIFT_ANCHOR_MAX_SKEW_S`）需先人工确认。
- hold 只做「沿用前值」不做趋势外推；首尾缺口一律不填值。
- 覆盖率把沿用与插补算作有效格，因此同时输出 raw 覆盖率与 carried/插补计数供复核。
- 区间外点只保留不参与计算，不在页面上画进趋势曲线（结果页与工作台以标签列出明细）。
- 启用记录的时间戳按设备上报同一时区口径解释；跨时区部署需先统一为带时区 ISO-8601。
- 样例数据为确定性生成（三角波 + 极小噪声），非真实产线采集；1s × 6 小时共 21120 行，
  仓库体积约 2.6MB，属于该场景固有体量。
