"""用语义锚点把录制还原成可读流程：每段变化打印「页面标签 + 点击位置 + 特征文字」。

    .venv\\Scripts\\python.exe tools\\storyline.py [session目录] [--all]

锚点按优先级匹配（上面先匹配），比自动聚类稳，因为游戏的页面标题是固定的。
"""
import json
import os
import re
import sys

ANCHORS = [
    ("扭蛋-选择", ["请选择1个扭蛋"]),
    ("扭蛋-主界面", ["超级扭蛋机"]),
    ("扭蛋-结果", ["转动扭蛋", "购买了扭蛋"]),
    ("经理增益-选择", ["请选择一种加强训练"]),
    ("经理增益-结果", ["加强训练成功"]),
    ("笔记选择", ["选择笔记"]),
    ("器材选择", ["选择器材"]),
    ("器材事件", ["器材事件"]),
    ("周末出行", ["出行", "步行街"]),
    ("商店", ["新品上市", "上新时间"]),
    ("本周总结", ["本周总结"]),
    ("训练事件", ["训练事件"]),
    ("训练剧本", ["训练剧本", "启程"]),
    ("主界面", ["薪资"]),
]


def norm(t):
    return re.sub(r"\s+", "", t or "")


def tag_of(texts):
    names = [norm(t[0]) for t in texts]
    for tag, kws in ANCHORS:
        for kw in kws:
            if any(kw in n for n in names):
                return tag
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    root = args[0] if args else None
    if not root:
        cands = sorted((d for d in os.listdir("demo") if d.startswith("session_")),
                       key=lambda d: os.path.getmtime(os.path.join("demo", d)))
        root = os.path.join("demo", cands[-1])
    with open(os.path.join(root, "timeline.json"), encoding="utf-8") as f:
        events = json.load(f).get("events") or []

    rows, prev_tag, pending_clicks = [], None, 0
    for ev in events:
        tag = tag_of(ev.get("texts") or [])
        c = ev.get("click") or {}
        has_click = c.get("rx") is not None
        if has_click:
            pending_clicks += 1
        changed = tag != prev_tag
        if not (changed or (show_all and has_click)):
            continue
        label = tag or "?"
        chars = [norm(t[0]) for t in (ev.get("texts") or []) if len(norm(t[0])) >= 2]
        # 挑几条有辨识度的文字
        interesting = [x for x in chars if not re.fullmatch(r"[\d./%+·\-\s]+", x)][:7]
        rows.append({
            "t": ev.get("t"), "tag": label, "img": ev.get("img"),
            "click": (round(c["rx"], 2), round(c["ry"], 2)) if has_click else None,
            "clicks_before": pending_clicks if has_click else 0,
            "texts": interesting,
        })
        prev_tag = tag
        if changed:
            pending_clicks = 0

    print(f"{os.path.basename(root)}：{len(events)} 事件，{len(rows)} 段变化\n")
    for i, r in enumerate(rows, 1):
        click = f"点击({r['click'][0]},{r['click'][1]})" if r["click"] else ""
        print(f"{i:3d}. t={r['t']:>6}s  [{r['tag']}]  {click}")
        print(f"       {' | '.join(r['texts'][:6])}")
        print(f"       {r['img']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
