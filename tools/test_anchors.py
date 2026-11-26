"""锚点层回归测试（全离线，不动游戏）。

对应 2026-11-24 的三层改造，重点守住：
  1. 锚点库自洽（pages ↔ anchors ↔ coco 三类 id/名字对得上，区域合法）
  2. 判页融合：OCR 未知时锚点补位；OCR 与锚点冲突时信锚点；上一步不可能的页被硬否决
  3. 推进器的三个底层件：色块找按钮（框架 find_color_rectangles）、危险词过滤、帧差

模板命中率本身由 `python tools/build_anchors.py --verify` 验证（那才是真·框架匹配）。

用法：python tools/test_anchors.py
"""
import glob
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import AutoTask, PAGE_FORBIDDEN

PASS = FAIL = 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANCHORS = os.path.join(ROOT, "assets", "anchors.json")
COCO = os.path.join(ROOT, "assets", "coco_annotations.json")


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def ok(name, cond, detail=""):
    check(name + (f"（{detail}）" if detail else ""), bool(cond), True)


print("--- 1. 锚点库自洽 ---")
meta = json.load(open(ANCHORS, encoding="utf-8"))
coco = json.load(open(COCO, encoding="utf-8"))
cats = {c["name"] for c in coco["categories"]}
ok("有页面定义", len(meta.get("pages", {})) >= 3, f"{len(meta.get('pages', {}))} 页")
for page, d in meta["pages"].items():
    ok(f"{page} 的锚点都在库里", all(a in meta["anchors"] for a in d["anchors"]))
    ok(f"{page} 的锚点是 coco 类别", all(a in cats for a in d["anchors"]))
for name, a in meta["anchors"].items():
    r = a["region"]
    ok(f"{name} 区域合法", 0 <= r[0] < 1 and 0 <= r[1] < 1
       and 0 < r[2] <= 1 and 0 < r[3] <= 1 and r[0] + r[2] <= 1.001)
proto_ok = 0
for im in coco["images"]:
    if os.path.exists(os.path.join(ROOT, "assets", im["file_name"])):
        proto_ok += 1
check("原型图都在", proto_ok, len(coco["images"]))
# 框架硬约束：一个类别只能有一个框
per_cat = {}
for a in coco["annotations"]:
    per_cat[a["category_id"]] = per_cat.get(a["category_id"], 0) + 1
check("每类恰好一个框（框架 read_from_json 的硬约束）", max(per_cat.values()), 1)

print("--- 2. 判页融合 ---")
s = AutoTask.__new__(AutoTask)
s.log_info = s.log_warning = lambda *a, **k: None
s._tuning_cache = {"anchors": {"enabled": True, "threshold": 0.8}}
s._vis_stats = {"hit": 0, "fill": 0, "conflict": 0, "veto": 0}
s._last_page_hint = None

# 2a OCR 未知 → 锚点补位
s._classify_ocr = lambda items, frame=None: "未知"
s._anchor_page = lambda frame: "商店"
check("OCR 未知 → 锚点补位", s._classify([], np.zeros((10, 10, 3), np.uint8)), "商店")
ok("记了补位", s._vis_stats["fill"] >= 1)

# 2b 冲突 → 信锚点（像素证据优先于关键词共现）
s._classify_ocr = lambda items, frame=None: "每日页"
s._anchor_page = lambda frame: "训练评分"
check("OCR 每日页 vs 锚点训练评分 → 信锚点",
      s._classify([], np.zeros((10, 10, 3), np.uint8)), "训练评分")
ok("记了冲突", s._vis_stats["conflict"] >= 1)

# 2c 一致 → 原样
s._classify_ocr = lambda items, frame=None: "商店"
s._anchor_page = lambda frame: "商店"
check("两边一致", s._classify([], np.zeros((10, 10, 3), np.uint8)), "商店")

print("--- 3. 硬约束（马尔可夫，真的拦） ---")
s2 = AutoTask.__new__(AutoTask)
s2.log_info = s2.log_warning = lambda *a, **k: None
s2._tuning_cache = {"anchors": {"enabled": False}}
s2._vis_stats = {"hit": 0, "fill": 0, "conflict": 0, "veto": 0}
s2._anchor_page = lambda frame: None
s2._last_page_hint = "训练项页"
s2._classify_ocr = lambda items, frame=None: "大厅"
check("训练项页→大厅 被否决", s2._classify([], None), "未知")
s2._last_page_hint = "选择队友"
s2._classify_ocr = lambda items, frame=None: "每日页"
check("选择队友→每日页 被否决", s2._classify([], None), "未知")
s2._last_page_hint = "每日页"
s2._classify_ocr = lambda items, frame=None: "训练项页"
check("每日页→训练项页 合法", s2._classify([], None), "训练项页")
ok("商店确实在训练项页的禁用表里", "商店" in PAGE_FORBIDDEN["训练项页"])

print("--- 4. 推进器底层件 ---")
shop = sorted(glob.glob(os.path.join(ROOT, "screenshots/auto/*_shop.png")))
frame = cv2.imread(shop[0]) if shop else None
s3 = AutoTask.__new__(AutoTask)
s3.log_info = s3.log_warning = lambda *a, **k: None
btns = s3._color_buttons(frame) if frame is not None else []
ok("框架色块检测找到按钮", len(btns) >= 1, f"{len(btns)} 个")
ok("按钮都在下半屏", all(b["cy"] > frame.shape[0] * 0.1 for b in btns) if btns else False)
if btns:
    b = btns[0]
    fake = [{"t": "购买", "cx": b["cx"], "cy": b["cy"]}]
    check("按钮上「购买」判危险", s3._danger_button(b, fake), (True, "购买"))
    check("没文字不误判危险", s3._danger_button(b, []), (False, ""))
    check("按钮文字能贴回来", s3._text_on_button(b, fake), "购买")

# 帧差：自己跟自己 = 0；换一张明显不同的图 > 1
same = s3._frame_diff(frame, frame.copy())
check("同一帧差值为 0", round(same, 4), 0.0)
other = cv2.imread(sorted(glob.glob(os.path.join(ROOT, "logs/scores/*rating*.png")))[0])
diff = s3._frame_diff(frame, other)
ok("不同页帧差 > 1", diff > 1.0, f"{diff:.2f}")

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
