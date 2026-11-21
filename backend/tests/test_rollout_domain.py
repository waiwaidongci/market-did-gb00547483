"""分批启用双重差分的领域测试（标准库 unittest，可在无 Docker 环境运行）。"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.rollout import (  # noqa: E402
    analyze_rollout,
    normalize_schedule,
    validate_schedule_coverage,
)

MARKETS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
BASELINE = {"M1": 30, "M2": 32, "M3": 34, "M4": 28, "M5": 26, "M6": 36, "M7": 40}
# 两批：M1/M2 在 11 月 2 日启用，M3/M4 在 11 月 9 日启用；M5/M6/M7 窗口内不启用。
SCHEDULE = [
    {"market_id": "M1", "activate_date": "2026-11-02"},
    {"market_id": "M2", "activate_date": "2026-11-02"},
    {"market_id": "M3", "activate_date": "2026-11-09"},
    {"market_id": "M4", "activate_date": "2026-11-09"},
    {"market_id": "M5", "activate_date": "2027-01-04"},
    {"market_id": "M6", "activate_date": "2027-01-04"},
    {"market_id": "M7", "activate_date": "2027-01-11"},
]
START = date(2026, 10, 19)
DAYS = 7


def build_rows(schedule, treatment_effect=-8, drop=None, noise=0, end_offset=49):
    """构造平衡面板：启用日及之后启用市场等候下降 treatment_effect。"""
    activate = {item["market_id"]: date.fromisoformat(item["activate_date"]) for item in schedule}
    drop = drop or set()
    rows = []
    for offset in range(end_offset):
        day = START + timedelta(days=offset)
        for market in MARKETS:
            if (market, day) in drop:
                continue
            value = BASELINE[market] + noise
            if activate[market] <= day:
                value += treatment_effect
            rows.append({"market_id": market, "date": day.isoformat(),
                         "group": "试点组" if activate[market] <= date(2026, 11, 16) else "对照组",
                         "wait_minutes": float(value), "truck_count": 20})
    return rows


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.rows = build_rows(SCHEDULE)

    def test_batches_and_controls(self):
        result = analyze_rollout(self.rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        batches = result["batches"]
        self.assertEqual([b["activate_date"] for b in batches], ["2026-11-02", "2026-11-09"])
        first, second = batches
        # 第一批窗口末日 11-08：M3/M4（11-09 启用）与 M5/M6/M7 都可作对照。
        self.assertEqual(first["control_kept"], ["M3", "M4", "M5", "M6", "M7"])
        self.assertEqual(first["treated_kept"], ["M1", "M2"])
        # 第二批窗口末日 11-15：M1/M2 已启用，不能作对照；只剩 M5/M6/M7。
        self.assertEqual(second["control_kept"], ["M5", "M6", "M7"])
        self.assertEqual(second["treated_kept"], ["M3", "M4"])
        self.assertTrue(first["comparable"] and second["comparable"])

    def test_effect_recovers_minus_eight(self):
        result = analyze_rollout(self.rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        for batch in result["batches"]:
            self.assertEqual(batch["effect"], -8.0)
            self.assertEqual(batch["placebo"], 0.0)
        self.assertEqual(result["overall"]["effect"], -8.0)
        self.assertEqual(result["overall"]["batch_count"], 2)

    def test_already_activated_never_control(self):
        # 第二批对照中绝不能出现第一批市场。
        result = analyze_rollout(self.rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        second = result["batches"][1]
        self.assertNotIn("M1", second["control_kept"])
        self.assertNotIn("M2", second["control_kept"])

    def test_last_batch_without_control_marked(self):
        # 所有市场都在最晚批次窗口末日之前启用 → 最晚批次无对照。
        schedule = [dict(item) for item in SCHEDULE]
        for item in schedule:
            if item["market_id"] in {"M5", "M6", "M7"}:
                item["activate_date"] = "2026-11-10"
        rows = build_rows(schedule)
        result = analyze_rollout(rows, {"window_days": DAYS, "pretrend_threshold": 2}, schedule)
        last = result["batches"][-1]
        self.assertFalse(last["comparable"])
        self.assertIn("没有未启用市场可作对照", last["reason"])
        # 整体估计只含可比较批次。
        self.assertEqual(result["overall"]["batch_count"],
                         sum(1 for b in result["batches"] if b["comparable"]))

    def test_missing_report_excludes_market(self):
        # M5 在第二批窗口内缺报 → 从第二批对照剔除，第一批窗口不受影响。
        rows = build_rows(SCHEDULE, drop={("M5", date(2026, 11, 12))})
        result = analyze_rollout(rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        first, second = result["batches"]
        self.assertIn("M5", first["control_kept"])
        self.assertNotIn("M5", second["control_kept"])
        coverage = [row for row in result["tables"][3]["rows"]
                    if row["market"] == "M5" and row["activate_date"] == "2026-11-09"][0]
        self.assertEqual(coverage["status"], "排除")
        self.assertIn("2026-11-12", coverage["missing"])

    def test_control_all_missing_marks_reason(self):
        # 第二批仅有的对照 M5/M6/M7 全部缺报 → 批次不可比较并给出原因。
        drop = {("M5", date(2026, 11, 12)), ("M6", date(2026, 11, 13)), ("M7", date(2026, 11, 14))}
        rows = build_rows(SCHEDULE, drop=drop)
        result = analyze_rollout(rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        second = result["batches"][1]
        self.assertFalse(second["comparable"])
        self.assertIn("缺报", second["reason"])
        # 第一批仍可比较，整体估计照常给出。
        self.assertTrue(result["batches"][0]["comparable"])
        self.assertEqual(result["overall"]["batch_count"], 1)

    def test_no_comparable_batch_overall_none(self):
        # 单一启用日 + 所有市场同日启用 → 唯一批次无对照。
        schedule = [{"market_id": m, "activate_date": "2026-11-02"} for m in MARKETS]
        rows = build_rows(schedule)
        result = analyze_rollout(rows, {"window_days": DAYS, "pretrend_threshold": 2}, schedule)
        self.assertIsNone(result["overall"])
        self.assertFalse(result["batches"][0]["comparable"])
        self.assertEqual(result["metrics"][0]["value"], None)

    def test_pretrend_threshold_flag(self):
        # 给试点市场在启用前第二段加 5 分钟漂移 → 安慰剂超阈值。
        rows = build_rows(SCHEDULE)
        drift_start = date(2026, 10, 26)
        for row in rows:
            if row["market_id"] in {"M1", "M2"} and drift_start <= date.fromisoformat(row["date"]) < date(2026, 11, 2):
                row["wait_minutes"] += 5
        result = analyze_rollout(rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        first = result["batches"][0]
        self.assertTrue(first["pretrend_flag"])
        self.assertGreater(abs(first["placebo"]), 2)

    def test_schedule_validation(self):
        with self.assertRaises(ValueError):
            normalize_schedule([])
        bad = [dict(SCHEDULE[0]), dict(SCHEDULE[0])]
        with self.assertRaises(ValueError):
            normalize_schedule(bad)
        normalized = normalize_schedule(SCHEDULE)
        one_market = [row for row in self.rows if row["market_id"] == "M1"]
        with self.assertRaises(ValueError):
            validate_schedule_coverage(normalized, one_market)

    def test_params_validation(self):
        with self.assertRaises(ValueError):
            analyze_rollout(self.rows, {"window_days": 0}, SCHEDULE)
        with self.assertRaises(ValueError):
            analyze_rollout(self.rows, {"unknown": 1}, SCHEDULE)

    def test_correction_changes_batch_and_overall(self):
        # 排期更正：M3 从 11-09 提前到 11-02 → 批次归属与对照集合都变化。
        corrected = [dict(item) for item in SCHEDULE]
        corrected[2]["activate_date"] = "2026-11-02"
        old = analyze_rollout(self.rows, {"window_days": DAYS, "pretrend_threshold": 2}, SCHEDULE)
        new = analyze_rollout(self.rows, {"window_days": DAYS, "pretrend_threshold": 2}, corrected)
        old_first = old["batches"][0]
        new_first = new["batches"][0]
        self.assertNotIn("M3", old_first["treated_kept"])
        self.assertIn("M3", new_first["treated_kept"])
        # M3 成为试点后，第一批候选对照减少。
        self.assertNotIn("M3", new_first["control_kept"])
        # 两版结果结构独立，旧版不被覆盖。
        self.assertIn("M3", old_first["control_kept"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
