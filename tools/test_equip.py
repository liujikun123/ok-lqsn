"""器材（编队支援卡）检测回归测试 —— 纯离线，喂**真实跑出来的审计帧**。

    ..\\ok-bsg\\.venv\\Scripts\\python.exe tools\\test_equip.py

历史：这个测试原来只喂 `screenshots/sweep/`（那批素材 2026-11-24 已不存在），
      于是整段断言变成「0 张核心帧全部 1 张金卡」的空转，旧实现在实机 263 帧
      全部检出 0 张的问题一直没被发现。现在改成**人工标注过的真实帧**。

标注（2026-11-24 逐帧看图核对，文件名时间戳唯一）：
  101011_core2_Def  1 张 金
  204802_core1_Jump 3 张 金金金（哑铃 / 鞋子 / 人物卡）
  095607_core3_Def  3 张 彩金金（左边哑铃是紫粉彩光 MAX）
  173154_core2_Def  2 张 金彩（右边篮球卡是紫粉彩光）
  171750_core2_Phys 2 张 金金（薯片袋 + 鞋子，两张都是金边）
  095844_core4_Dribble 0 张（卡已淡出 —— 用来验证不会凭空造卡）
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.core.cards import card_glow, find_cards  # noqa: E402
from src.tasks.AutoTask import AutoTask  # noqa: E402

# (时间戳前缀, 期望张数, 期望光效；None = 只校验张数)
# 2026-11-24 全部改成人工看图标注的真值：这些卡都是**平涂黄框**（普），
# 只有 095607 左边那张哑铃和 173154 的篮球是彩虹 MAX（彩）。
# 旧表把它们全标成「金」，等于把「检测器把所有黄框都当金光」这个 bug 写进了期望值里。
CASES = [
    ("101011_core2_Def", 1, ["普"]),
    ("204802_core1_Jump", 3, ["普", "普", "普"]),
    ("095607_core3_Def", 3, ["彩", "普", "普"]),
    ("173154_core2_Def", 2, ["普", "彩"]),
    ("171750_core2_Phys", 2, ["普", "普"]),
    # 这帧是玩家亲自指出问题的现场：4 张全是平涂黄框，旧检测器 4 张全判「金」。
    # 留着当反例，防止以后又把「黄底」当成「金光」。
    ("111959_core4_IQ", 4, ["普", "普", "普", "普"]),
    ("095844_core4_Dribble", 0, []),
]


def rd(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), 1)


def check(title, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {title} {detail}")
    return ok


def locate(stamp):
    hits = glob.glob(os.path.join(ROOT, "logs", "audit", "*", "*", stamp + ".png"))
    return hits[0] if hits else None


def main():
    allok = True

    print("--- 人工标注帧：张数 + 光效 ---")
    missing = []
    for stamp, n, glows in CASES:
        p = locate(stamp)
        if not p:
            missing.append(stamp)
            continue
        im = rd(p)
        cs = find_cards(im)
        got = [card_glow(im, c) for c in cs]
        ok = len(cs) == n and (not glows or got == glows)
        allok &= check(f"{stamp:22s} 期望 {n} 张{glows}", ok, f"实测 {len(cs)} 张{got}")
    if missing:
        print(f"  (素材缺失 {len(missing)} 条：{missing})")

    print("--- 全量审计帧：不允许出现第 5 张（误检红线）---")
    # 红线原来是 >3，2026-11-24 11:19 的实跑帧 111959_core4_IQ.png 推翻了这个假设：
    # 那局**真的带了 4 个器材**（薯片/球鞋/篮球/药瓶，四张卡并排，人工看图确认过）。
    # 所以上限放到 4；>4 才说明把地板/人物当成卡了。
    fs = glob.glob(os.path.join(ROOT, "logs", "audit", "*", "*", "*core*.png"))
    over = []
    for p in fs:
        im = rd(p)
        if im is None:
            continue
        n = len(find_cards(im))
        if n > 4:
            over.append(f"{os.path.basename(p)}={n}")
    allok &= check(f"{len(fs)} 帧里没有 >4 张的误检", not over, str(over[:3]))

    print("--- 空帧不造卡 ---")
    blank = np.zeros((900, 1600, 3), np.uint8)
    blank[:] = (120, 160, 200)     # 木地板色（旧实现在这种纯地板上会整片误检）
    allok &= check("纯地板色帧 0 张", len(find_cards(blank)) == 0, str(find_cards(blank)))

    print("--- 计分接入 ---")
    cfg = {"teammate": 1.0, "level_bonus": {}, "fail_weight": 1.0,
           "equipment": 0.5, "equipment_tier": {"金": 1.0, "彩": 1.5, "无": 0.5}}
    got = AutoTask._score_item(None, ["a"], "LV.1", 0, cfg, ["金"])
    allok &= check("1 队友 + 1 金卡 = 2.0", abs(got - 2.0) < 1e-9, f"got {got}")

    print("\n结果:" + (" 全部通过" if allok else " 有失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
