"""分批启用卸货预约的双重差分。

排期把市场映射到启用日（进入试点的自然日，当天计入试点后）。按启用日把市场
划分为批次；同批市场在试点前后共用同一批对照市场。截至某批窗口末日已启用的
市场不能充当对照。没有合格对照的批次只标注原因、不进入整体估计。

仅依赖标准库，分数运算使用 :class:`fractions.Fraction`，展示时才取整。
"""

from datetime import date
from fractions import Fraction
from statistics import mean

from .analysis import _date, _display, _number, _table, validate_rows

_PERIODS = ("试点前段", "试点前后段", "试点后")


# --------------------------------------------------------------------------- #
# 排期
# --------------------------------------------------------------------------- #
def normalize_schedule(raw_schedule):
    """校验并归一化排期，返回 ``{market: iso_date}``，按启用日、市场排序。"""
    if not isinstance(raw_schedule, (list, tuple)) or not raw_schedule:
        raise ValueError("启用排期至少需要一条市场启用日记录。")
    schedule = {}
    for index, item in enumerate(raw_schedule, start=1):
        label = f"排期第 {index} 条"
        if not isinstance(item, dict) or not {"market_id", "activate_date"} <= item.keys():
            raise ValueError(f"{label}须包含市场名称和启用日。")
        market = item["market_id"]
        if not isinstance(market, str) or not market.strip() or len(market.strip()) > 300:
            raise ValueError(f"{label}的市场名称须为 1–300 个字符。")
        market = market.strip()
        activate = _date(item["activate_date"], f"{label}的启用日").isoformat()
        if market in schedule:
            raise ValueError(f"市场「{market}」在排期中重复。")
        schedule[market] = activate
    return dict(sorted(schedule.items(), key=lambda kv: (kv[1], kv[0])))


def validate_schedule_coverage(schedule, readings):
    """排期市场须与数据中的市场完全一致，避免静默漏排或排到不存在的市场。"""
    data_markets = {row["market_id"] for row in readings}
    scheduled = set(schedule)
    missing = sorted(data_markets - scheduled)
    unknown = sorted(scheduled - data_markets)
    problems = []
    if missing:
        problems.append("缺少启用日：" + "、".join(missing))
    if unknown:
        problems.append("排期市场不在数据中：" + "、".join(unknown))
    if problems:
        raise ValueError("启用排期与门岗数据不一致，" + "；".join(problems) + "。")


def _rollout_params(normalized_params):
    allowed = {"window_days", "pretrend_threshold"}
    if not isinstance(normalized_params, dict) or normalized_params.keys() - allowed:
        raise ValueError("分批测算参数格式有误或包含未知参数。")
    days = normalized_params.get("window_days", 7)
    threshold = normalized_params.get("pretrend_threshold", 2)
    if type(days) is not int or not 1 <= days <= 28:
        raise ValueError("每段自然日数须为 1–28 的整数。")
    if not _number(threshold, 0, 1440):
        raise ValueError("试点前偏离阈值须为 0–1440 分钟。")
    return days, Fraction(str(threshold))


# --------------------------------------------------------------------------- #
# 面板
# --------------------------------------------------------------------------- #
def _index_readings(readings):
    indexed = {}
    for row in readings:
        indexed.setdefault(row["market_id"], {})[date.fromisoformat(row["date"]).toordinal()] = row
    return indexed


def _window_coverage(indexed, market, start, end):
    """返回该市场在 ``[start, end)`` 内缺报的自然日（ordinal 列表）。"""
    observed = indexed.get(market, {})
    return [day for day in range(start, end) if day not in observed]


def _mean_wait(indexed, market, start, end):
    observed = indexed[market]
    return mean(Fraction(str(observed[day]["wait_minutes"])) for day in range(start, end))


def _trucks(indexed, markets, start, end):
    return sum(indexed[market][day]["truck_count"]
               for market in markets for day in range(start, end) if day in indexed[market])


def _range_label(start, end):
    first, last = date.fromordinal(start), date.fromordinal(end - 1)
    return f"{first.isoformat()} 至 {last.isoformat()}"


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #
def analyze_rollout(rows, normalized_params, raw_schedule):
    readings = validate_rows(rows)
    schedule = normalize_schedule(raw_schedule)
    validate_schedule_coverage(schedule, readings)
    days, threshold = _rollout_params(normalized_params)
    indexed = _index_readings(readings)
    span_start, span_end = _window_bounds(schedule, days)
    outside = sum(1 for row in readings
                  if not (span_start <= date.fromisoformat(row["date"]).toordinal() < span_end))
    observed_ordinals = sorted({date.fromisoformat(row["date"]).toordinal() for row in readings})
    last_observed = observed_ordinals[-1]
    # 观测期之后才启用的市场属于「尚未启用」对照，不单独形成批次。
    future_activations = [
        {"market_id": market, "activate_date": activate_iso}
        for market, activate_iso in schedule.items()
        if date.fromisoformat(activate_iso).toordinal() > last_observed
    ]
    markets_by_date = {}
    for market, iso_day in schedule.items():
        if date.fromisoformat(iso_day).toordinal() <= last_observed:
            markets_by_date.setdefault(iso_day, []).append(market)

    batches = []
    coverage_rows = []

    if not markets_by_date:
        raise ValueError("所有启用日都晚于数据观测期末日，观测期内没有可分析的启用批次。")

    for activate_iso in sorted(markets_by_date):
        activate = date.fromisoformat(activate_iso).toordinal()
        start, pilot, end = activate - 2 * days, activate, activate + days
        treated = sorted(markets_by_date[activate_iso])
        # 截至窗口末日（end - 1）仍未启用的市场，才有资格作为对照。
        last_day_iso = date.fromordinal(end - 1).isoformat()
        candidates = sorted(m for m, d in schedule.items() if d > last_day_iso)

        treated_missing = {m: _window_coverage(indexed, m, start, end) for m in treated}
        control_missing = {m: _window_coverage(indexed, m, start, end) for m in candidates}
        kept_treated = [m for m in treated if not treated_missing[m]]
        kept_control = [m for m in candidates if not control_missing[m]]

        for market in treated:
            coverage_rows.append(_coverage_row(market, "本批试点", activate_iso, start, end,
                                               treated_missing[market], kept=market in kept_treated))
        for market in candidates:
            coverage_rows.append(_coverage_row(market, "候选对照", activate_iso, start, end,
                                               control_missing[market], kept=market in kept_control))

        batch = {
            "activate_date": activate_iso,
            "treated_markets": treated,
            "control_candidates": candidates,
            "treated_kept": kept_treated,
            "control_kept": kept_control,
            "window": {"start": date.fromordinal(start).isoformat(),
                       "pilot": activate_iso,
                       "end": date.fromordinal(end - 1).isoformat()},
            "comparable": False,
            "reason": "",
        }

        if not treated:
            batch["reason"] = "本批没有试点市场。"
        elif not kept_treated:
            batch["reason"] = "本批试点市场在三个窗口内均缺报，没有完整试点市场。"
        elif not candidates:
            batch["reason"] = f"截至窗口末日 {last_day_iso} 所有市场均已启用，没有未启用市场可作对照。"
        elif not kept_control:
            batch["reason"] = ("未启用市场 " + "、".join(candidates) +
                               " 在窗口内存在缺报，没有完整对照市场。")
        else:
            batch["comparable"] = True
            batch.update(_estimate_batch(indexed, kept_treated, kept_control, start, pilot, end, days))
            placebo = batch["placebo"]
            batch["pretrend_flag"] = abs(placebo) > threshold
        batches.append(batch)

    comparable = [b for b in batches if b["comparable"]]
    overall = _pool(comparable)
    result = _assemble(batches, comparable, overall, coverage_rows, outside, days, threshold,
                       schedule, future_activations)
    return result


def _window_bounds(schedule, days):
    ordinals = [date.fromisoformat(value).toordinal() for value in schedule.values()]
    pilot = min(ordinals)
    latest = max(ordinals)
    return pilot - 2 * days, latest + days


def _coverage_row(market, role, activate_iso, start, end, missing, kept):
    return {
        "market": market,
        "role": role,
        "activate_date": activate_iso,
        "observed_days": end - start - len(missing),
        "expected_days": end - start,
        "status": "纳入" if kept else "排除",
        "missing": "、".join(date.fromordinal(day).isoformat() for day in missing) if missing else "无",
    }


def _estimate_batch(indexed, treated, control, start, pilot, end, days):
    def block_means(markets):
        early = mean(_mean_wait(indexed, m, start, pilot - days) for m in markets)
        late = mean(_mean_wait(indexed, m, pilot - days, pilot) for m in markets)
        after = mean(_mean_wait(indexed, m, pilot, end) for m in markets)
        return early, late, after

    t_early, t_late, t_after = block_means(treated)
    c_early, c_late, c_after = block_means(control)
    t_before, c_before = mean((t_early, t_late)), mean((c_early, c_late))
    t_change, c_change = t_after - t_before, c_after - c_before
    effect = t_change - c_change
    placebo = (t_late - c_late) - (t_early - c_early)
    gaps = [t_early - c_early, t_late - c_late, t_after - c_after]
    return {
        "means": {
            "treated": [_display(t_early), _display(t_late), _display(t_after)],
            "control": [_display(c_early), _display(c_late), _display(c_after)],
        },
        "before": {"treated": _display(t_before), "control": _display(c_before)},
        "after": {"treated": _display(t_after), "control": _display(c_after)},
        "change": {"treated": _display(t_change), "control": _display(c_change)},
        "effect": _display(effect),
        "effect_raw": _display(effect),
        "placebo": _display(placebo),
        "placebo_raw": _display(placebo),
        "gaps": [_display(gap) for gap in gaps],
        "gaps_raw": [_display(gap) for gap in gaps],
        "market_days": {"treated": len(treated) * (end - start), "control": len(control) * (end - start)},
        "trucks": {"treated": _trucks(indexed, treated, start, end),
                   "control": _trucks(indexed, control, start, end)},
        "range_labels": {"early": _range_label(start, pilot - days),
                         "late": _range_label(pilot - days, pilot),
                         "after": _range_label(pilot, end),
                         "before": _range_label(start, pilot)},
    }


def _pool(comparable):
    """整体估计只在可比较批次间做市场等权聚合（各批 DiD 按批内市场数加权）。"""
    if not comparable:
        return None
    weights = [len(b["treated_kept"]) + len(b["control_kept"]) for b in comparable]
    total_weight = sum(weights)
    effect = sum(b["effect_raw"] * w for b, w in zip(comparable, weights)) / total_weight
    placebo = sum(b["placebo_raw"] * w for b, w in zip(comparable, weights)) / total_weight
    treated_markets = sorted({m for b in comparable for m in b["treated_kept"]})
    control_markets = sorted({m for b in comparable for m in b["control_kept"]})
    return {
        "effect": _display(effect),
        "effect_raw": _display(effect),
        "placebo": _display(placebo),
        "placebo_raw": _display(placebo),
        "weights": weights,
        "total_weight": total_weight,
        "treated_markets": treated_markets,
        "control_markets": control_markets,
        "batch_count": len(comparable),
    }


def _assemble(batches, comparable, overall, coverage_rows, outside, days, threshold, schedule,
              future_activations):
    warnings = []
    ineligible = [b for b in batches if not b["comparable"]]
    if ineligible:
        warnings.append(f"{len(ineligible)} 个批次无合格对照，未进入整体估计。")
    if not comparable:
        warnings.append("没有可比较批次，无法给出整体双重差分。")
    if overall and any(b.get("pretrend_flag") for b in comparable):
        warnings.append("存在批次试点前安慰剂差分超过阈值，复核平行趋势假设。")
    sparse = [b for b in comparable
              if min(len(b["treated_kept"]), len(b["control_kept"])) < 2]
    if sparse:
        warnings.append("至少一个可比较批次的某一侧仅一家市场，估计易受单个市场影响。")

    headline = (f"整体双重差分为 {overall['effect']:+g} 分钟（{overall['batch_count']} 个可比较批次）。"
                if overall else "没有可比较批次，本版排期无法给出整体双重差分。")
    note = (f"共 {len(batches)} 个启用批次，{len(comparable)} 个可比较。"
            + (f"窗口外 {outside} 条。" if outside else "")
            + "".join(warnings))

    metrics = []
    if overall:
        metrics.append({"label": "整体双重差分（分钟）", "value": overall["effect"],
                        "detail": "仅基于可比较批次，按批内纳入市场数加权；负值表示相对缩短。"})
        metrics.append({"label": "整体试点前安慰剂差分（分钟）", "value": overall["placebo"],
                        "detail": f"可比较批次加权，阈值 {float(threshold):g} 分钟。"})
        metrics.append({"label": "可比较批次数", "value": overall["batch_count"],
                        "detail": f"共 {len(batches)} 个批次，{len(ineligible)} 个无合格对照。"})
        metrics.append({"label": "整体纳入市场数", "value": len(overall["treated_markets"]) + len(overall["control_markets"]),
                        "detail": f"试点 {len(overall['treated_markets'])} 家、对照 {len(overall['control_markets'])} 家（按出现去重）。"})
    else:
        metrics.append({"label": "整体双重差分（分钟）", "value": None,
                        "detail": "没有可比较批次，未计算整体估计。"})
        metrics.append({"label": "可比较批次数", "value": 0,
                        "detail": f"共 {len(batches)} 个批次，全部无合格对照。"})

    batch_table_rows = []
    for batch in batches:
        if batch["comparable"]:
            reason = f"对照 {len(batch['control_kept'])} 家、试点 {len(batch['treated_kept'])} 家，进入整体估计"
            effect, placebo = batch["effect"], batch["placebo"]
        else:
            reason = batch["reason"]
            effect = placebo = None
        batch_table_rows.append({
            "activate_date": batch["activate_date"],
            "treated": "、".join(batch["treated_markets"]),
            "control": "、".join(batch["control_kept"]) if batch["comparable"]
                       else ("、".join(batch["control_candidates"]) or "无候选"),
            "status": "可比较" if batch["comparable"] else "无合格对照",
            "effect": effect,
            "placebo": placebo,
            "reason": reason,
        })

    cell_rows, diagnostic_rows = [], []
    for batch in comparable:
        labels = batch["range_labels"]
        for side, group in (("treated", "试点组"), ("control", "对照组")):
            cell_rows.append({
                "activate_date": batch["activate_date"], "group": group,
                "before": batch["before"][side], "after": batch["after"][side],
                "change": batch["change"][side],
                "markets": len(batch["treated_kept"] if side == "treated" else batch["control_kept"]),
                "market_days": batch["market_days"][side], "trucks": batch["trucks"][side],
                "before_dates": labels["before"], "after_dates": labels["after"],
            })
        diagnostic_rows.append({
            "activate_date": batch["activate_date"],
            "early_treated": batch["means"]["treated"][0], "late_treated": batch["means"]["treated"][1],
            "early_control": batch["means"]["control"][0], "late_control": batch["means"]["control"][1],
            "placebo": batch["placebo"],
            "status": "超过复核阈值" if batch["pretrend_flag"] else "在复核阈值内",
            "early_dates": labels["early"], "late_dates": labels["late"],
        })

    charts = []
    if comparable:
        charts.append({"title": "各可比较批次双重差分（分钟）", "kind": "bar", "series": [
            {"label": f"批次 {b['activate_date']}", "value": b["effect"]} for b in comparable]})
        charts.append({"title": "各可比较批次组间差值（试点组减对照组，分钟）", "kind": "line", "series": [
            {"label": f"{period} · {b['activate_date']}", "value": gap}
            for b in comparable for period, gap in zip(_PERIODS, b["gaps"])]})

    sections = [
        {"title": "分批口径", "text": "按启用日划分批次，同批市场前后使用同一批对照。"
         f"截至批次窗口末日已启用的市场不作对照；每段 {days} 个自然日，启动日计入试点后。"
         "仅三个窗口逐日完整的市场进入该批估计。"},
        {"title": "整体估计", "text": (
            f"整体双重差分 {overall['effect']:+g} 分钟，只聚合 {overall['batch_count']} 个可比较批次，"
            "按各批纳入市场数加权；无合格对照的批次不参与。"
            if overall else "所有批次都没有合格对照，本版排期只给出逐批诊断，不给出整体估计。")},
        {"title": "解释限制", "text": "因果解释依赖平行趋势、无提前响应和无跨市场分流。"
         "晚启用批次在早批次窗口中充当对照，若存在预期效应或分流，对照会被污染；"
         "缺报剔除与短窗口同样限制代表性。"},
    ]

    return {
        "state": "warning" if warnings else "ok",
        "headline": headline,
        "note": note,
        "metrics": metrics,
        "tables": [
            _table("分批启用与对照资格", (
                ("activate_date", "启用日"), ("treated", "本批试点市场"),
                ("control", "实际对照市场"), ("status", "批次状态"),
                ("effect", "双重差分（分钟）"), ("placebo", "安慰剂差分（分钟）"),
                ("reason", "说明 / 无对照原因"),
            ), batch_table_rows),
            _table("可比较批次四格均值", (
                ("activate_date", "启用日"), ("group", "分组"),
                ("before_dates", "试点前日期"), ("before", "试点前均值（分钟）"),
                ("after_dates", "试点后日期"), ("after", "试点后均值（分钟）"),
                ("change", "后减前（分钟）"), ("markets", "市场数"),
                ("market_days", "市场日数"), ("trucks", "有效车辆数"),
            ), cell_rows),
            _table("试点前趋势与安慰剂", (
                ("activate_date", "启用日"), ("early_dates", "前段日期"),
                ("early_treated", "前段试点组"), ("early_control", "前段对照组"),
                ("late_dates", "后段日期"), ("late_treated", "后段试点组"),
                ("late_control", "后段对照组"), ("placebo", "安慰剂差分（分钟）"),
                ("status", "阈值判定"),
            ), diagnostic_rows),
            _table("市场观测覆盖（按批次）", (
                ("activate_date", "批次启用日"), ("market", "市场"), ("role", "在该批角色"),
                ("observed_days", "已报日数"), ("expected_days", "应报日数"),
                ("status", "样本状态"), ("missing", "缺报日期"),
            ), coverage_rows),
        ],
        "charts": charts,
        "sections": sections,
        "batches": [_batch_view(b) for b in batches],
        "overall": overall,
        "schedule": [{"market_id": m, "activate_date": d} for m, d in schedule.items()],
        "future_activations": future_activations,
        "window_days": days,
        "pretrend_threshold": float(threshold),
    }


def _batch_view(batch):
    view = {key: batch[key] for key in (
        "activate_date", "treated_markets", "control_candidates", "treated_kept",
        "control_kept", "comparable", "reason", "window")}
    if batch["comparable"]:
        view.update(effect=batch["effect"], placebo=batch["placebo"],
                    pretrend_flag=batch["pretrend_flag"])
    return view
