"""把示范录制还原成「流程线」：每屏是什么、用户点的是哪个按钮、下一步去哪。

原理：timeline.json 里每帧都有 OCR 文字 + 坐标，每个 click 又有绝对点击坐标。
用点击坐标去命中文字框，就能知道「在哪个页面点了哪个按钮」，不用猜。

用法：
  .venv/Scripts/python.exe tools/trace_flow.py                 # 全部输出
  .venv/Scripts/python.exe tools/trace_flow.py 0 60            # 只看前 60 步
  .venv/Scripts/python.exe tools/trace_flow.py 0 999 > demo/flow.txt
"""
import json
import os
import re
import sys

DEMO = "demo/session_1122_200929/timeline.json"

HUD = re.compile(r"^(\d+/\d+|\d+|[\d\.\+%]+|第\d+周|星期[一二三四五六日天]|心.技.体|心.枝.体|"
                 r"力以赴！！！?|赢得比赛|跳过|《|B\+|A\+|S\+|C\+|SS|S|A|B|C)$")
NOISE = {"昆三", "早三", "思三", "数昆三", "教昆三", "民三", "艮三", "夕三", "臺羅北空", "埃昆三"}


def norm(t):
    return re.sub(r"\s+", "", t or "")


def signature(texts):
    """页面签名：去掉 HUD 和单字噪声后的文字集合。"""
    words = []
    for t, *_ in texts:
        t = norm(t)
        if not t or HUD.match(t) or t in NOISE or len(t) < 2:
            continue
        words.append(t)
    return tuple(sorted(set(words)))


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def hit_text(texts, x, y):
    """哪个文字框被点到了（取面积最小的那个，通常是按钮文字）。"""
    best, best_area = None, None
    for t, bx, by, bw, bh in texts:
        if bx - 6 <= x <= bx + bw + 6 and by - 6 <= y <= by + bh + 6:
            area = bw * bh
            if best_area is None or area < best_area:
                best, best_area = t, area
    return best or "（空白/图标）"


def main():
    lo = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    hi = int(sys.argv[2]) if len(sys.argv) > 2 else 10 ** 9
    with open(DEMO, encoding="utf-8") as f:
        ev = json.load(f)["events"]

    clicks = [e for e in ev if e["kind"] == "click"]
    rows = []
    for e in clicks:
        texts = e["texts"]
        c = e["click"]
        # click.x 是桌面屏幕坐标（窗口可能偏到 1920 以外），
        # rx/ry 才是窗口内归一化坐标，乘回 1920x1080 才能和 OCR 框对齐
        px, py = c["rx"] * 1920, c["ry"] * 1080
        rows.append({
            "i": e["i"], "t": e["t"], "img": e["img"],
            "sig": signature(texts),
            "texts": texts,
            "target": hit_text(texts, px, py),
            "xy": (int(px), int(py)),
        })

    # 相同签名视为同一页面，连续合并
    merged = []
    for r in rows:
        if merged and jaccard(merged[-1]["sig"], r["sig"]) >= 0.75:
            m = merged[-1]
            m["n"] += 1
            m["steps"].append((r["target"], r["xy"], r["img"]))
            # 签名取交集，避免越滚越大
            m["sig"] = tuple(sorted(set(m["sig"]) & set(r["sig"]))) or r["sig"]
        else:
            merged.append({"sig": r["sig"], "t": r["t"], "img": r["img"],
                           "n": 1, "steps": [(r["target"], r["xy"], r["img"])]})

    print(f"# 录制流程线：{len(clicks)} 次点击 → 合并后 {len(merged)} 屏\n")
    for idx, m in enumerate(merged):
        if idx < lo or idx > hi:
            continue
        top = [w for w in m["sig"] if len(w) >= 2][:10]
        print(f"## {idx:03d}  t={m['t']:.1f}s  点击{m['n']}次  {m['img']}")
        print(f"   屏上文字: {' | '.join(top) if top else '（无）'}")
        uniq = []
        for tg, xy, img in m["steps"]:
            if tg not in [u[0] for u in uniq]:
                uniq.append((tg, xy, img))
        for tg, xy, img in uniq:
            cnt = sum(1 for s in m["steps"] if s[0] == tg)
            print(f"   点了「{tg}」@({xy[0]},{xy[1]}) ×{cnt}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
