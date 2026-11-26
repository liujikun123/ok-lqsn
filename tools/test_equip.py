"""器材（编队支援卡）检测回归测试 —— 纯离线，喂**真实跑出来的审计帧**。

    ..\\ok-bsg\\.venv\\Scripts\\python.exe tools\\test_equip.py

历史：这个测试原来只喂 `screenshots/sweep/`（那批素材 2026-11-24 已不存在），
      于是整段断言变成「0 张核心帧全部 1 张金卡」的空转，旧实现在实机 263 帧
      全部检出 0 张的问题一直没被发现。现在改成**人工标注过的真实帧**。

标注（2026-11-26 第四次核对 —— 玩家给了手机截图后校准完成）：

  101011_core2_Def  1 张 金（希雅+大星星，浓橙金粗框）
  204802_core1_Jump 3 张 普·普·金（哑铃 / 球鞋 是浅黄细框，希雅星卡是金框）
  095607_core3_Def  3 张 彩·普·普（左边哑铃是紫粉彩虹 MAX）
  173154_core2_Def  2 张 普·彩（右边篮球卡是紫粉彩虹）
  171750_core2_Phys 2 张 普·普（薯片袋 + 鞋子）
  111959_core4_IQ   4 张 普×4
  095844_core4_Dribble 0 张（卡已淡出 —— 验证不会凭空造卡）

判据（2026-11-26 最终版，见 src/core/cards.py 文件头）：
  采样 = bbox 内 1~3px 那一圈（框线最外沿）；再往里就进卡面了，
  卡面本身也是橙黄（S≈170），会把三档压成单峰。实测 287 张：
    S 120~145 / 150~165  → 普（浅奶油黄细框）      173 张 60%
    S 165~208            → **空档，阈值 185 落在这里**
    S 209~210            → 金（浓橙金粗框 + 外光晕）111 张 39%
    青蓝紫占比 ≥0.30     → 彩（彩虹）                 3 张  1%
  比例正好是玩家说的「普最多、金次之、彩最少」。

  与玩家手机截图对账：截图左卡球鞋 S≈123→普、右卡希雅星卡 S≈209→金，
  与电脑端同器材的读数完全一致。
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

# (时间戳前缀, 期望张数, 期望品质；None = 只校验张数)
CASES = [
    ("101011_core2_Def", 1, ["金"]),
    ("204802_core1_Jump", 3, ["普", "普", "金"]),
    ("095607_core3_Def", 3, ["彩", "普", "普"]),
    ("173154_core2_Def", 2, ["普", "彩"]),
    ("171750_core2_Phys", 2, ["普", "普"]),
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

    print("--- 全量审计帧：最多 5 张（误检红线）---")
    # 上限演进：3 → 4（111959 那帧真的有 4 张）→ **5**（玩家 2026-11-26：
    # 「器材是开局先选好五个…四五个也可能」）。>5 才说明把地板/人物当成卡了。
    fs = glob.glob(os.path.join(ROOT, "logs", "audit", "*", "*", "*core*.png"))
    over = []
    for p in fs:
        im = rd(p)
        if im is None:
            continue
        n = len(find_cards(im))
        if n > 5:
            over.append(f"{os.path.basename(p)}={n}")
    allok &= check(f"{len(fs)} 帧里没有 >5 张的误检", not over, str(over[:3]))

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
