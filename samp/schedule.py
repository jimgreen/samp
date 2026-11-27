"""采样启动方式（周期 / 定点 / 触发）规范化、定点时刻匹配与下次触发计算。"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta
from typing import Any

MODE_TO_TYPE = {"periodic": 1, "fixed": 2, "trigger": 3}
TYPE_TO_MODE = {1: "periodic", 2: "fixed", 3: "trigger"}
FIXED_UNITS = ("hour", "day", "week", "month")

_IDENT_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")


def _as_int_in_range(val: Any, name: str, lo: int, hi: int, default: int | None = None) -> int:
    if val is None or val == "":
        if default is not None:
            return default
        raise ValueError(f"定点配置缺少字段 {name}")
    try:
        iv = int(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"定点字段 {name} 必须是整数") from exc
    if iv < lo or iv > hi:
        raise ValueError(f"定点字段 {name} 必须在 {lo}..{hi} 之间")
    return iv


def _parse_time_str(raw: str, expect_hour: bool) -> dict[str, int]:
    parts = [p.strip() for p in raw.strip().split(":")]
    if expect_hour:
        if len(parts) == 2:
            return {"hour": int(parts[0]), "minute": int(parts[1]), "second": 0}
        if len(parts) == 3:
            return {"hour": int(parts[0]), "minute": int(parts[1]), "second": int(parts[2])}
        raise ValueError(f"时间格式无效（需 HH:MM:SS）: {raw}")
    if len(parts) == 2:
        return {"minute": int(parts[0]), "second": int(parts[1])}
    raise ValueError(f"时间格式无效（需 MM:SS）: {raw}")


def normalize_schedule(
    start_mode: str | None = None,
    schedule_config: dict[str, Any] | None = None,
    samp_type: int | None = None,
) -> tuple[str, int, dict[str, Any]]:
    """校验并规范化采样启动方式，返回 (start_mode, samp_type, schedule_config)。"""
    raw_mode = str(start_mode or "").strip().lower()
    if not raw_mode:
        if samp_type in TYPE_TO_MODE:
            raw_mode = TYPE_TO_MODE[int(samp_type)]
        else:
            raw_mode = "periodic"
    if raw_mode not in MODE_TO_TYPE:
        raise ValueError(f"不支持的采样启动方式: {start_mode}（仅支持 periodic / fixed / trigger）")

    cfg = schedule_config if isinstance(schedule_config, dict) else {}

    if raw_mode == "periodic":
        return "periodic", 1, {}

    if raw_mode == "fixed":
        unit = str(cfg.get("unit") or "day").strip().lower()
        if unit not in FIXED_UNITS:
            raise ValueError(f"定点采样单位无效: {unit}（仅支持 hour / day / week / month）")
        raw_points = cfg.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            raise ValueError("定点采样至少需要配置 1 个启动时间点")
        norm_points: list[dict[str, int]] = []
        seen: set[tuple[int, ...]] = set()
        for item in raw_points:
            if isinstance(item, str):
                parsed = _parse_time_str(item, expect_hour=(unit != "hour"))
                if unit in ("week", "month"):
                    raise ValueError(f"按{unit}定点需提供结构化时间点对象")
                item_dict: dict[str, Any] = parsed
            elif isinstance(item, dict):
                item_dict = item
            else:
                raise ValueError("定点启动时间点格式无效")

            if unit == "hour":
                m = _as_int_in_range(item_dict.get("minute"), "minute", 0, 59, 0)
                s = _as_int_in_range(item_dict.get("second"), "second", 0, 59, 0)
                key = (m, s)
                if key not in seen:
                    seen.add(key)
                    norm_points.append({"minute": m, "second": s})
            elif unit == "day":
                h = _as_int_in_range(item_dict.get("hour"), "hour", 0, 23, 0)
                m = _as_int_in_range(item_dict.get("minute"), "minute", 0, 59, 0)
                s = _as_int_in_range(item_dict.get("second"), "second", 0, 59, 0)
                key = (h, m, s)
                if key not in seen:
                    seen.add(key)
                    norm_points.append({"hour": h, "minute": m, "second": s})
            elif unit == "week":
                wd = _as_int_in_range(item_dict.get("weekday"), "weekday", 1, 7)
                h = _as_int_in_range(item_dict.get("hour"), "hour", 0, 23, 0)
                m = _as_int_in_range(item_dict.get("minute"), "minute", 0, 59, 0)
                s = _as_int_in_range(item_dict.get("second"), "second", 0, 59, 0)
                key = (wd, h, m, s)
                if key not in seen:
                    seen.add(key)
                    norm_points.append({"weekday": wd, "hour": h, "minute": m, "second": s})
            else:  # month
                dy = _as_int_in_range(item_dict.get("day"), "day", 1, 31)
                h = _as_int_in_range(item_dict.get("hour"), "hour", 0, 23, 0)
                m = _as_int_in_range(item_dict.get("minute"), "minute", 0, 59, 0)
                s = _as_int_in_range(item_dict.get("second"), "second", 0, 59, 0)
                key = (dy, h, m, s)
                if key not in seen:
                    seen.add(key)
                    norm_points.append({"day": dy, "hour": h, "minute": m, "second": s})

        norm_points.sort(
            key=lambda p: (
                p.get("day", 0),
                p.get("weekday", 0),
                p.get("hour", 0),
                p.get("minute", 0),
                p.get("second", 0),
            )
        )
        return "fixed", 2, {"unit": unit, "points": norm_points}

    # trigger (3)
    source = str(cfg.get("source") or "builtin").strip().lower()
    if source not in ("builtin", "external"):
        raise ValueError("触发采样标志位来源仅支持 builtin 或 external")
    trig_val = str(cfg.get("trigger_value") if cfg.get("trigger_value") not in (None, "") else "1").strip()
    reset_val = str(cfg.get("reset_value") if cfg.get("reset_value") is not None else "0").strip()
    auto_reset = bool(cfg.get("auto_reset", True))

    if source == "builtin":
        return (
            "trigger",
            3,
            {
                "source": "builtin",
                "trigger_value": trig_val,
                "reset_value": reset_val,
                "auto_reset": auto_reset,
            },
        )

    database = str(cfg.get("database") or "").strip()
    table = str(cfg.get("table") or "").strip()
    column = str(cfg.get("column") or "").strip()
    flt = str(cfg.get("filter") or "").strip()
    if not database:
        raise ValueError("外部触发采样必须指定标志位数据库")
    if not table or not _IDENT_RE.match(table):
        raise ValueError(f"外部触发标志位表名不合法: {table}")
    if not column or not _IDENT_RE.match(column):
        raise ValueError(f"外部触发标志位字段名不合法: {column}")

    return (
        "trigger",
        3,
        {
            "source": "external",
            "database": database,
            "table": table,
            "column": column,
            "filter": flt,
            "trigger_value": trig_val,
            "reset_value": reset_val,
            "auto_reset": auto_reset,
        },
    )


def _iter_months(start_dt: datetime, end_dt: datetime):
    year, month = start_dt.year, start_dt.month
    while (year, month) <= (end_dt.year, end_dt.month):
        yield year, month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def fixed_due_slots(schedule_config: dict[str, Any], window_start: int, window_end: int) -> list[int]:
    """返回落在 (window_start, window_end] 半开区间内的所有定点触发时间戳（秒级升序去重）。"""
    if window_end < window_start:
        return []
    unit = str(schedule_config.get("unit") or "day").lower()
    points = schedule_config.get("points") or []
    if not points:
        return []

    start_dt = datetime.fromtimestamp(window_start)
    end_dt = datetime.fromtimestamp(window_end)
    hits: set[int] = set()

    if unit == "hour":
        cursor = start_dt.replace(minute=0, second=0, microsecond=0)
        end_hour = end_dt.replace(minute=0, second=0, microsecond=0)
        while cursor <= end_hour:
            for pt in points:
                cand = cursor.replace(minute=int(pt["minute"]), second=int(pt["second"]))
                ts = int(cand.timestamp())
                if window_start < ts <= window_end:
                    hits.add(ts)
            cursor += timedelta(hours=1)
    elif unit == "day":
        cursor = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= end_day:
            for pt in points:
                cand = cursor.replace(hour=int(pt["hour"]), minute=int(pt["minute"]), second=int(pt["second"]))
                ts = int(cand.timestamp())
                if window_start < ts <= window_end:
                    hits.add(ts)
            cursor += timedelta(days=1)
    elif unit == "week":
        cursor = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= end_day:
            wd = cursor.isoweekday()
            for pt in points:
                if int(pt["weekday"]) != wd:
                    continue
                cand = cursor.replace(hour=int(pt["hour"]), minute=int(pt["minute"]), second=int(pt["second"]))
                ts = int(cand.timestamp())
                if window_start < ts <= window_end:
                    hits.add(ts)
            cursor += timedelta(days=1)
    elif unit == "month":
        for year, month in _iter_months(start_dt, end_dt):
            max_day = calendar.monthrange(year, month)[1]
            for pt in points:
                actual_day = min(int(pt["day"]), max_day)
                cand = datetime(year, month, actual_day, int(pt["hour"]), int(pt["minute"]), int(pt["second"]))
                ts = int(cand.timestamp())
                if window_start < ts <= window_end:
                    hits.add(ts)

    return sorted(hits)


def compute_fixed_next_run(schedule_config: dict[str, Any], after_ts: int) -> int | None:
    """计算严格晚于 after_ts 的下一次定点触发时间戳（秒）。"""
    unit = str(schedule_config.get("unit") or "day").lower()
    points = schedule_config.get("points") or []
    if not points:
        return None

    base = datetime.fromtimestamp(after_ts)
    best: int | None = None

    if unit == "hour":
        cursor = base.replace(minute=0, second=0, microsecond=0)
        for offset in range(3):
            hr = cursor + timedelta(hours=offset)
            for pt in points:
                ts = int(hr.replace(minute=int(pt["minute"]), second=int(pt["second"])).timestamp())
                if ts > after_ts and (best is None or ts < best):
                    best = ts
            if best is not None:
                return best
    elif unit == "day":
        cursor = base.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(3):
            dy = cursor + timedelta(days=offset)
            for pt in points:
                ts = int(
                    dy.replace(hour=int(pt["hour"]), minute=int(pt["minute"]), second=int(pt["second"])).timestamp()
                )
                if ts > after_ts and (best is None or ts < best):
                    best = ts
            if best is not None:
                return best
    elif unit == "week":
        cursor = base.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(9):
            dy = cursor + timedelta(days=offset)
            wd = dy.isoweekday()
            for pt in points:
                if int(pt["weekday"]) != wd:
                    continue
                ts = int(
                    dy.replace(hour=int(pt["hour"]), minute=int(pt["minute"]), second=int(pt["second"])).timestamp()
                )
                if ts > after_ts and (best is None or ts < best):
                    best = ts
            if best is not None:
                return best
    elif unit == "month":
        year, month = base.year, base.month
        for _ in range(14):
            max_day = calendar.monthrange(year, month)[1]
            for pt in points:
                actual_day = min(int(pt["day"]), max_day)
                ts = int(
                    datetime(year, month, actual_day, int(pt["hour"]), int(pt["minute"]), int(pt["second"])).timestamp()
                )
                if ts > after_ts and (best is None or ts < best):
                    best = ts
            if best is not None:
                return best
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
    return best
