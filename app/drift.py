"""时钟漂移估计：以阈值穿越事件为锚点，最小二乘拟合 t_参考 = a·t_设备 + b。

符号约定（全项目一致）：
- 时间都用「相对窗口起点的秒」，避免绝对 epoch 参与拟合导致 b 失去意义。
- t_ref = a · t_dev + b（a≈1、b≈-offset）。
- offset_s = 设备时钟读数 − 参考时钟读数（窗口起点处），正值表示设备表快。
- rate_ppm = 设备走时相对参考的偏差，正值表示设备走时偏快。
- rmse_s = 内点锚点的拟合残差均方根，即「按 a、b 校正后」的剩余时间误差。

三点工程取舍（写在 README 里）：
1. 只用相邻采样间隔 <= 90s 的区段产生锚点：更稀疏的区段插值出的穿越时刻不可靠。
2. 先用最近邻滞后中位数求粗对齐，再按滞后配对，避免「缺一个锚点就全串位」。
3. 拟合后按 MAD 剔除跨拐点的离群锚点，最多迭代 3 轮，并汇报内点/剔除数量。

只用标准库 statistics + 手写最小二乘（不引入 numpy）。
"""
from __future__ import annotations

import math
import statistics

from . import settings


class DriftError(ValueError):
    """漂移估计输入不足或拟合不可用。"""


def threshold_crossings(
    points: list[tuple[float, float]],
    level: float,
    max_dt_s: float = settings.DRIFT_ANCHOR_MAX_DT_S,
) -> list[float]:
    """返回序列穿越 level 的时刻（相邻点线性插值），严格穿越才算。

    相邻采样间隔大于 max_dt_s 的区段跳过（插值时刻不可靠）。
    """
    times: list[float] = []
    for (t1, v1), (t2, v2) in zip(points, points[1:]):
        if t2 - t1 > max_dt_s:
            continue
        d1 = v1 - level
        d2 = v2 - level
        if d1 == 0.0 and d2 != 0.0:
            times.append(t1)
            continue
        if d1 * d2 < 0.0:
            ratio = (level - v1) / (v2 - v1)
            times.append(t1 + (t2 - t1) * ratio)
    return times


def _nearest(sorted_values: list[float], target: float) -> float | None:
    if not sorted_values:
        return None
    import bisect

    pos = bisect.bisect_left(sorted_values, target)
    best = None
    for idx in (pos - 1, pos):
        if 0 <= idx < len(sorted_values):
            cand = sorted_values[idx]
            if best is None or abs(cand - target) < abs(best - target):
                best = cand
    return best


def estimate_lag(
    ref_times: list[float],
    dev_times: list[float],
    tolerance_s: float = settings.DRIFT_ANCHOR_MAX_SKEW_S,
) -> float | None:
    """粗对齐：参考锚点最近的设备锚点差值的鲁棒中位数（= 设备时钟偏移）。"""
    diffs: list[float] = []
    for t_ref in ref_times:
        near = _nearest(dev_times, t_ref)
        if near is not None and abs(near - t_ref) <= tolerance_s:
            diffs.append(near - t_ref)
    if not diffs:
        return None
    return statistics.median(diffs)


def pair_anchors(
    ref_times: list[float],
    dev_times: list[float],
    lag_s: float,
    tolerance_s: float = settings.DRIFT_PAIR_TOLERANCE_S,
) -> list[tuple[float, float]]:
    """按「参考时刻 + 粗偏移」去配对，每台设备锚点最多用一次，返回 (设备时刻, 参考时刻)。"""
    pairs: list[tuple[float, float]] = []
    used: set[float] = set()
    for t_ref in ref_times:
        target = t_ref + lag_s
        pos = None
        best = None
        for cand in dev_times:
            if cand in used:
                continue
            if best is None or abs(cand - target) < abs(best - target):
                best = cand
        if best is None or abs(best - target) > tolerance_s:
            continue
        used.add(best)
        pairs.append((best, t_ref))
    pairs.sort(key=lambda p: p[1])
    return pairs


def fit_affine(xs: list[float], ys: list[float]) -> dict:
    """最小二乘拟合 y = a·x + b，返回 a、b、残差与样本量。"""
    n = len(xs)
    if n < 2:
        raise DriftError("锚点不足 2 个，无法拟合")
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        raise DriftError("锚点时间全部相同，无法拟合")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    a = sxy / sxx
    if a <= 0:
        raise DriftError("拟合斜率非正，锚点配对异常")
    b = mean_y - a * mean_x
    residuals = [y - (a * x + b) for x, y in zip(xs, ys)]
    rmse = math.sqrt(sum(r * r for r in residuals) / n)
    return {
        "a": a,
        "b": b,
        "rmse_s": rmse,
        "max_abs_residual_s": max(abs(r) for r in residuals),
        "anchors": n,
        "residuals": residuals,
    }


def _robust_fit(pairs: list[tuple[float, float]], min_anchors: int = settings.DRIFT_MIN_ANCHORS) -> dict:
    """MAD 剔除离群锚点后重拟合（最多 3 轮）。"""
    current = pairs
    dropped = 0
    fit: dict = {}
    for _ in range(3):
        if len(current) < min_anchors:
            break
        fit = fit_affine([p[0] for p in current], [p[1] for p in current])
        residuals = fit["residuals"]
        center = statistics.median(residuals)
        mad = statistics.median([abs(r - center) for r in residuals])
        limit = max(3.0 * 1.4826 * mad, 0.5)
        keep = [p for p, r in zip(current, residuals) if abs(r - center) <= limit]
        if len(keep) == len(current) or len(keep) < min_anchors:
            break
        dropped += len(current) - len(keep)
        current = keep
    if len(current) < min_anchors:
        raise DriftError(f"内点锚点只剩 {len(current)} 个（< {min_anchors}），无法给出可靠漂移估计")
    fit = fit_affine([p[0] for p in current], [p[1] for p in current])
    fit["dropped_anchors"] = dropped
    return fit


def estimate_drift(
    ref_points: list[tuple[float, float]],
    dev_points: list[tuple[float, float]],
    window_start: float,
    threshold_s: float = settings.DEFAULT_OFFSET_THRESHOLD_S,
    level: float | None = None,
) -> dict:
    """估计设备时钟相对参考序列的偏移与走时偏差。

    ref_points / dev_points: [(epoch_seconds, value), ...]，需已按时间排序。
    """
    if len(ref_points) < 4 or len(dev_points) < 4:
        raise DriftError("序列点数不足，无法估计时钟漂移")

    raw_level = level if level is not None else statistics.median([v for _, v in ref_points])
    ref_hits = [t - window_start for t in threshold_crossings(ref_points, raw_level)]
    dev_hits = [t - window_start for t in threshold_crossings(dev_points, raw_level)]
    if len(ref_hits) < settings.DRIFT_MIN_ANCHORS or len(dev_hits) < settings.DRIFT_MIN_ANCHORS:
        raise DriftError(
            f"可用穿越锚点不足（参考 {len(ref_hits)} / 设备 {len(dev_hits)}）；"
            "请换一个变化幅度更大的指标，或确认该设备时间未偏离一个采样周期以上"
        )

    lag = estimate_lag(ref_hits, dev_hits)
    if lag is None:
        raise DriftError(
            f"参考与设备的穿越锚点无法在 ±{settings.DRIFT_ANCHOR_MAX_SKEW_S:g}s 内配对，"
            "时钟偏移可能远超容差，需先人工确认设备时间"
        )
    pairs = pair_anchors(ref_hits, dev_hits, lag)
    if len(pairs) < settings.DRIFT_MIN_ANCHORS:
        raise DriftError(f"配对成功锚点仅 {len(pairs)} 个（< {settings.DRIFT_MIN_ANCHORS}），无法估计漂移")

    fit = _robust_fit(pairs)
    a = fit["a"]
    b = fit["b"]
    offset_s = -b / a          # 窗口起点处：设备读数 − 参考读数
    rate_ppm = (1.0 / a - 1.0) * 1e6
    drifted = abs(offset_s) > threshold_s
    residual = fit["rmse_s"]

    if drifted:
        sign = "快" if offset_s > 0 else "慢"
        suggestion = (
            f"设备时钟比参考{sign} {abs(offset_s):.1f}s（走时偏差 {rate_ppm:+.0f}ppm）。"
            f"对齐建议：该设备 ts 统一减去 {offset_s:.1f}s 后再落到 1 分钟网格；"
            f"校正后残差 RMSE {residual:.2f}s，可满足 1 分钟桶对齐需求。"
        )
        verdict = f"判定为时钟漂移：|偏移| {abs(offset_s):.1f}s > 阈值 {threshold_s:g}s"
    else:
        suggestion = (
            f"设备时钟偏移 {offset_s:+.1f}s 未超过阈值 {threshold_s:g}s，"
            "无需校时，可直接进入 1 分钟网格对齐。"
        )
        verdict = f"时钟正常：|偏移| {abs(offset_s):.1f}s ≤ 阈值 {threshold_s:g}s"

    return {
        "threshold_s": threshold_s,
        "level": raw_level,
        "a": a,
        "b": b,
        "offset_s": offset_s,
        "rate_ppm": rate_ppm,
        "rmse_s": residual,
        "max_abs_residual_s": fit["max_abs_residual_s"],
        "anchors": fit["anchors"],
        "dropped_anchors": fit.get("dropped_anchors", 0),
        "ref_anchors": len(ref_hits),
        "dev_anchors": len(dev_hits),
        "coarse_lag_s": lag,
        "drifted": drifted,
        "verdict": verdict,
        "suggestion": suggestion,
        "correction": {
            "shift_seconds": offset_s,
            "scale": a,
            "formula": "t_ref = a · t_dev + b",
        },
    }
