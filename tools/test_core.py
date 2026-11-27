"""离线验证训练项打分 / 器材计分 / 主训识别 / 高风险改练规则（不碰游戏、不用截图）。

    ..\\ok-bsg\\.venv\\Scripts\\python.exe tools\\test_core.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.tasks.AutoTask import AutoTask  # noqa: E402


class Stub:
    """只挂被测方法需要的最小状态，不初始化 ok 框架。"""

    shop_has_stamina = None
    stamina = None

    def log_info(self, msg):
        print(f"    info | {msg}")

    def log_warning(self, msg):
        print(f"    warn | {msg}")


Stub._score_item = AutoTask._score_item
Stub._force_skill_when_risky = AutoTask._force_skill_when_risky
Stub._main_char_of_round = AutoTask._main_char_of_round
Stub._apply_fail_limit = AutoTask._apply_fail_limit
Stub._apply_consciousness_boost = AutoTask._apply_consciousness_boost

CFG = {"teammate": 1.0, "level_bonus": {"Lv.1": 0.0, "Lv.2": 0.0, "MAX": 0.0},
       "fail_weight": 1.0, "resident_ratio": 1.0,
       "equipment": 0.5, "equipment_tier": {"金": 1.0, "彩": 1.5, "无": 0.5},
       "force_when_risky": {"enabled": True, "fail_over": 30, "skill": "意识"}}


def row(name):
    return {"t": name, "cy": 0, "cx": 0}


def item(name, mates, level, pct):
    s = Stub()
    score = s._score_item([f"队友{i}" for i in range(mates)], level, pct, CFG)
    return {"row": row(name), "mates": mates, "level": level, "pct": pct,
            "score": score, "valid": True}


def check(title, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {title}: {got}" + ("" if ok else f" (期望 {want})"))
    return ok


def main():
    s = Stub()
    allok = True

    print("--- 计分：等级不计分，队友数 × 失败率折扣 ---")
    allok &= check("2 队友 MAX 0%失败", round(s._score_item(["a", "b"], "MAX", 0, CFG), 3), 2.0)
    allok &= check("2 队友 Lv.1 0%失败（等级不加工）",
                   round(s._score_item(["a", "b"], "LV.1", 0, CFG), 3), 2.0)
    allok &= check("2 队友 40%失败 → ×0.6", round(s._score_item(["a", "b"], "LV.1", 40, CFG), 3), 1.2)
    allok &= check("3 队友 50%失败 → ×0.5", round(s._score_item(["a", "b", "c"], "LV.2", 50, CFG), 3), 1.5)

    print("--- 器材计分：按暴击档位（玩家：普 0.5 / 金 1 / 彩 1.5）---")
    allok &= check("0 队友 + 1 张金卡", round(s._score_item([], "LV.1", 0, CFG, ["金"]), 3), 1.0)
    allok &= check("0 队友 + 1 张彩卡", round(s._score_item([], "LV.1", 0, CFG, ["彩"]), 3), 1.5)
    allok &= check("0 队友 + 1 张普通卡", round(s._score_item([], "LV.1", 0, CFG, ["普"]), 3), 0.5)
    allok &= check("1 队友 + 2 张金卡", round(s._score_item(["a"], "LV.1", 0, CFG, ["金", "金"]), 3), 3.0)
    allok &= check("1 队友 + 1 金 1 彩 50%失败 → (1+1+1.5)×0.5",
                   round(s._score_item(["a"], "LV.1", 50, CFG, ["金", "彩"]), 3), 1.75)
    allok &= check("2 普 1 彩 = 0.5+0.5+1.5", round(s._score_item([], "LV.1", 0, CFG, ["普", "普", "彩"]), 3), 2.5)
    allok &= check("器材传 None 不炸", round(s._score_item(["a"], "LV.1", 0, CFG, None), 3), 1.0)

    print("--- 高风险规则：失败率全部 >30 且商店无精力药 → 改练意识 ---")
    risky = [item("投射", 2, "LV.1", 45), item("意识", 1, "LV.1", 35)]
    s.shop_has_stamina = False
    got = s._force_skill_when_risky(risky, CFG)
    allok &= check("无药 + 全高风险 → 意识", got and got["row"]["t"], "意识")

    s.shop_has_stamina = True
    allok &= check("有精力药 → 不强制", s._force_skill_when_risky(risky, CFG), None)

    s.shop_has_stamina = None
    allok &= check("没进过店 → 不强制", s._force_skill_when_risky(risky, CFG), None)

    s.shop_has_stamina = False
    mixed = [item("投射", 2, "LV.1", 45), item("意识", 1, "LV.1", 20)]
    allok &= check("有一个 ≤30% → 不强制", s._force_skill_when_risky(mixed, CFG), None)

    no_mind = [item("投射", 2, "LV.1", 45), item("体能", 1, "LV.1", 40)]
    allok &= check("意识今天没角标 → 不强制", s._force_skill_when_risky(no_mind, CFG), None)

    no_pct = [item("投射", 2, "LV.1", None), item("意识", 1, "LV.1", 40)]
    allok &= check("失败率读不到 → 不强制", s._force_skill_when_risky(no_pct, CFG), None)

    print("--- 失败率硬门槛：有 0% 的就不练 >0% 的（玩家：从不失败）---")
    cfgl = dict(CFG, fail_limit=0)
    pool = [item("投射", 3, "LV.1", 0), item("防守", 2, "LV.1", 30),
            item("弹跳", 2, "LV.1", 0), item("意识", 1, "LV.1", None)]
    kept = s._apply_fail_limit(pool, cfgl)
    allok &= check("只剩 0% 的两项", sorted(d["row"]["t"] for d in kept), ["弹跳", "投射"])

    allbad = [item("投射", 2, "LV.1", 40), item("防守", 2, "LV.1", 50)]
    # 2026-11-24 新口径（strict_fallback: min_fail）：全都 >0% 时不再「保留全部随便赌」，
    #   只留失败率最低的那档。全部 >30% 时 _force_skill_when_risky 会改练意识。
    allok &= check("全都 >0% → 只留最低失败率那档",
                   len(s._apply_fail_limit(allbad, cfgl)), 1)
    allok &= check("最低档确实是 40% 那项",
                   sorted(d["row"]["t"] for d in s._apply_fail_limit(allbad, cfgl)), ["投射"])
    # 老行为（strict_fallback: old）仍可切回来：退回原排名，全部保留
    cfgl_old = dict(cfgl, strict_fallback="old")
    allok &= check("strict_fallback=old → 退回全部",
                   len(s._apply_fail_limit(allbad, cfgl_old)), 2)

    print("--- 失败率 30 以内可以赌（玩家 2026-11-23：收益高的话 <30 都能去）---")
    cfg30 = dict(CFG, fail_limit=30)
    pool30 = [item("投射", 3, "LV.1", 0), item("防守", 2, "LV.1", 25),
              item("弹跳", 2, "LV.1", 30), item("意识", 1, "LV.1", 40)]
    kept30 = s._apply_fail_limit(pool30, cfg30)
    allok &= check("≤30% 全留（可赌），>30% 踢掉",
                   sorted(d["row"]["t"] for d in kept30), ["弹跳", "投射", "防守"])

    # 期望收益的实质：4 队友 ×25% 失败 = 3.0 分，仍然赢过「2 队友 0 失败 = 2.0 分」
    high = s._score_item(["a", "b", "c", "d"], "LV.1", 25, CFG)
    safe = s._score_item(["a", "b"], "LV.1", 0, CFG)
    allok &= check("高收益值得赌：25% 失败的 4 人场赢过 0 失败的 2 人场",
                   high > safe, True)
    # 反过来：收益差不多就不该赌
    same = s._score_item(["a", "b"], "LV.1", 25, CFG)
    allok &= check("收益一样就别赌：25% 失败输给 0 失败", same < safe, True)

    allok &= check("不配 fail_limit → 不过滤", len(s._apply_fail_limit(pool, CFG)), 4)

    print("--- 意识加权：精力 < 45 时拉满（不耗精力、小幅回精力）---")
    cfgc = dict(CFG, consciousness={"enabled": True, "skill": "意识",
                                    "stamina_below": 45, "boost": 99})
    picks = [item("防守", 3, "LV.1", 0), item("意识", 1, "LV.1", 0)]
    s.stamina = 20
    s._apply_consciousness_boost(picks, cfgc)
    best = max(picks, key=lambda d: d["score"])["row"]["t"]
    allok &= check("精力 20 → 意识反超 3 队友的防守", best, "意识")

    picks2 = [item("防守", 3, "LV.1", 0), item("意识", 1, "LV.1", 0)]
    s.stamina = 100
    s._apply_consciousness_boost(picks2, cfgc)
    allok &= check("精力 100 → 不加权，防守仍第一",
                   max(picks2, key=lambda d: d["score"])["row"]["t"], "防守")

    picks3 = [item("防守", 3, "LV.1", 0), item("体能", 2, "LV.1", 0)]
    s.stamina = 20
    s._apply_consciousness_boost(picks3, cfgc)
    allok &= check("意识今天没角标 → 原排名不变",
                   max(picks3, key=lambda d: d["score"])["row"]["t"], "防守")
    s.stamina = None

    print("--- 主训人员：每个训练项都在场的名字（不写死、且仍算队友）---")
    skills = [{"names": {"佐久间", "闪堂"}}, {"names": {"希雅", "闪堂"}},
              {"names": {"闪堂"}}, {"names": {"闪堂"}}]
    allok &= check("认出主训 = 闪堂", s._main_char_of_round(skills, CFG), "闪堂")
    allok &= check("主训仍算队友（不剔除）", [len(d["names"]) for d in skills], [2, 2, 1, 1])

    one = [{"names": {"闪堂", "希雅"}}]
    allok &= check("只采到一个训练项 → 认不出主训", s._main_char_of_round(one, CFG), None)

    odd = [{"names": {"闪堂", "希雅"}}, {"names": {"闪堂"}}, {"names": {"闪堂", "佐久间"}}]
    allok &= check("闪堂三场全有 → 主训是闪堂", s._main_char_of_round(odd, CFG), "闪堂")

    none_full = [{"names": {"希雅"}}, {"names": {"佐久间"}}, {"names": {"闪堂"}}]
    allok &= check("没人满场 → 认不出", s._main_char_of_round(none_full, CFG), None)

    print("\n结果:" + (" 全部通过" if allok else " 有失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
