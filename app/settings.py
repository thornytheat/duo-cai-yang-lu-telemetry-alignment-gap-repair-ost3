"""常量与路径：全部业务阈值集中在此，便于测试与说明引用。"""
from __future__ import annotations

import os
import pathlib

APP_NAME = "多采样率遥测对齐与缺口修复"
SHORT_NAME = "tsgrid"

# 1 分钟统一网格
BUCKET_SECONDS = 60

# 时钟漂移判定：设备时钟偏移绝对值阈值（秒）
DEFAULT_OFFSET_THRESHOLD_S = 5.0

# 缺口/沿用策略
HOLD_MAX_BRACKET_BUCKETS = 3   # 前后原始格间距 <= 3 格 -> 视为低频采样段，hold 沿用（carried）
INTERPOLATE_MAX_CELLS = 3      # 缺口 <= 3 格 -> 线性插补；> 3 格 -> unrepaired

# 质量分级
GRADE_A_COVERAGE = 0.98
GRADE_B_COVERAGE = 0.95

METRICS = ("temperature", "pressure")
METRIC_LABEL = {"temperature": "温度", "pressure": "压力"}
METRIC_UNIT = {"temperature": "℃", "pressure": "bar"}

# 漂移配准
DRIFT_ANCHOR_MAX_SKEW_S = 300.0   # 粗对齐（滞后中位数）允许的最大偏移（秒）
DRIFT_PAIR_TOLERANCE_S = 30.0     # 精配对容差（秒）
DRIFT_ANCHOR_MAX_DT_S = 90.0      # 只在相邻采样间隔 <= 90s 的区段取穿越锚点
DRIFT_MIN_ANCHORS = 4             # 少于该锚点数不出结论

_BASE = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(os.environ.get("TSGRID_DATA_DIR", str(_BASE / "data")))
SAMPLE_DIR = pathlib.Path(os.environ.get("TSGRID_SAMPLE_DIR", str(_BASE / "sample_data" / "telemetry")))
DB_PATH = pathlib.Path(os.environ.get("TSGRID_DB_PATH", str(DATA_DIR / "tsgrid.sqlite3")))

SEED_VERSION = "tsgrid-seed-v1"
