# -*- coding: utf-8 -*-
"""术语统一改名脚本（2026-11-26）。

玩家定的新术语（唯一权威，另见 docs/17-术语表.md）：
  · 六项训练项（投射/弹跳/防守/体能/运控/意识）→ 一律叫「训练项」，不再叫「技能」
  · 点「训练」后右侧竖排六项的那一页        → 「训练项页」，不再叫「训练核心页/核心页」
  · 点训练项后弹出、写着队友/器材/失败率/等级 → 「训练项详情」，不再叫「编队面板/编队」
  · 点训练项后弹的「请选择一种加强训练」三选一 → 「经理增益」，不再叫「加强训练」
  · 「技能」只保留给奖励类型（获得技能 / 获得精力 / 获得属性）
  · 游戏自己的「确认编队」「请选择1个技能」等 OCR 原文一律不动

用法：
  python tools/rename_terms.py --dry      # 只统计命中次数
  python tools/rename_terms.py --apply    # 真改
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 要先保护起来的原文（游戏 UI 上的字 / 奖励词），改完再还原 ----
PROTECT = [
    "请选择一种加强训练",
    "加强训练成功",
    "请选择1个技能",
    "随机比赛技能",
    "确认编队",
    '"编队"',
]

# ---- 改名表：**顺序敏感**，长词在前 ----
RULES = [
    # 页面 / 算法名的机械替换
    ("训练核心页", "训练项页"),
    ("训练核心", "训练项页"),
    ("核心页", "训练项页"),
    ("加强训练策略", "经理增益策略"),
    ("加强训练三选一", "经理增益三选一"),
    ("加强训练页", "经理增益页"),
    ("加强训练", "经理增益"),
    ("编队面板", "训练项详情"),
    ("编队排名", "训练项排名"),
    ("展开编队", "展开训练项详情"),
    ("读到编队", "读到训练项详情"),
    ("编队比较", "训练项比较"),
    ("最优编队", "最优训练项"),
    ("编队采样", "训练项读数"),
    ("编队打分", "训练项打分"),
    ("编队评分", "训练项评分"),
    ("看编队", "看训练项详情"),
    ("配好编队", "配好队伍"),
    ("整次编队", "整次训练项"),
    # 六项训练项：把「技能」从代码里赶出去
    ("技能胶囊", "训练项条目"),
    ("技能采样", "训练项读数"),
    ("技能候选", "训练项候选"),
    ("技能面板", "训练项面板"),
    ("技能标签", "训练项标签"),
    ("技能行", "训练项行"),
    ("技能区", "训练项区"),
    ("技能列", "训练项列"),
    ("技能名", "训练项名"),
    ("技能帧", "训练项帧"),
    ("技能数", "训练项数"),
    ("技能排序", "训练项排序"),
    ("每个技能", "每个训练项"),
    ("所有技能", "所有训练项"),
    ("全部技能", "全部训练项"),
    ("个技能", "个训练项"),
    ("点开技能", "点开训练项"),
    ("点技能", "点训练项"),
    ("技能的", "训练项的"),
    ("技能都", "训练项都"),
    ("技能 ≥4", "训练项 ≥4"),
    ("技能≥4", "训练项≥4"),
    ("技能全 MAX", "训练项全 MAX"),
    ("技能：", "训练项："),
    ("技能够", "训练项够"),
    ("技能失败率", "训练项失败率"),
    ("技能等级", "训练项等级"),
    ("技能 >", "训练项 >"),
]

TARGET_EXT = (".py", ".md", ".json", ".yaml")
SKIP_DIRS = {".git", ".state", "logs", "screenshots", "node_modules", "__pycache__", ".venv", "dist"}

# 这些文件里的「技能/训练核心」是**数据**不是文案，改了会坏：
#   assets/ocr_cache.json      —— 缓存的是屏幕上真正识别出来的字
#   assets/coco_annotations.json —— 标注集的 label
#   profiles/*.json            —— 内嵌 strategy 文本 + 玩家档位
#   tools/rename_terms.py      —— 本脚本自己
SKIP_FILES = {
    "assets/ocr_cache.json",
    "assets/coco_annotations.json",
    "tools/rename_terms.py",
}


def walk():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            if not fn.endswith(TARGET_EXT) or rel in SKIP_FILES:
                continue
            if rel.startswith("profiles/"):
                continue
            yield p


def transform(text):
    holders = {}
    for i, lit in enumerate(PROTECT):
        key = "\x00P%d\x00" % i
        if lit in text:
            holders[key] = lit
            text = text.replace(lit, key)
    stat = {}
    for old, new in RULES:
        n = text.count(old)
        if n:
            stat[old] = stat.get(old, 0) + n
            text = text.replace(old, new)
    for key, lit in holders.items():
        text = text.replace(key, lit)
    return text, stat


def main():
    apply = "--apply" in sys.argv
    total = {}
    touched = []
    for p in walk():
        with io.open(p, encoding="utf-8") as f:
            src = f.read()
        out, stat = transform(src)
        if not stat:
            continue
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        touched.append((rel, sum(stat.values())))
        for k, v in stat.items():
            total[k] = total.get(k, 0) + v
        if apply and out != src:
            with io.open(p, "w", encoding="utf-8", newline="") as f:
                f.write(out)
    print("%s：%d 个文件命中" % ("已改" if apply else "试算", len(touched)))
    for rel, n in sorted(touched, key=lambda x: -x[1]):
        print("  %-46s %d" % (rel, n))
    print("\n规则命中：")
    for k, v in sorted(total.items(), key=lambda x: -x[1]):
        print("  %-14s %d" % (k, v))


if __name__ == "__main__":
    main()
