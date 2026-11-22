"""把示范录制压缩成「页面清单 + 点击流程」，便于快速读懂。

    .venv\\Scripts\\python.exe tools\\analyze_demo.py [session目录]

输出 digest.md：
    1) 页面清单：每页出现几次、特征文字
    2) 点击流程：页面 --(点击位置)×n--> 页面（连续同页同位置自动合并）
动态数字（金币、评分、时间等）会被过滤，避免同一页面被拆成几十个。
"""
import json
import os
import re
import sys
from collections import Counter

STOP = re.compile(r"^[\W_]+$")


def norm(t):
    return re.sub(r"\s+", "", t or "")


def useful(name):
    """过滤掉对识别页面没帮助的文字：纯符号、数字占比过高的。"""
    if len(name) < 2 or STOP.match(name):
        return False
    digits = sum(c.isdigit() for c in name)
    if digits / len(name) > 0.4:
        return False
    return True


def key_of(texts):
    s = set()
    for t in texts:
        n = norm(t[0])
        if useful(n):
            s.add(n)
    return s


def key_sets(events):
    """两遍法：先统计每个词出现在多少帧里，再去掉「到处都有」的 HUD 文字。
    六项属性名（体能/运控/投射…）几乎每屏都在，不剔掉的话聚类会全糊在一起。"""
    raw, df = [], Counter()
    for ev in events:
        s = key_of(ev.get("texts") or [])
        raw.append(s)
        df.update(s)
    n = max(1, len(raw))
    hub = {w for w, c in df.items() if c > n * 0.30}
    noise = {w for w, c in df.items() if c == 1 and len(w) <= 3}
    drop = hub | noise
    return [s - drop for s in raw], sorted(hub)[:15]


def sim(a, b):
    """重合度（不是 Jaccard）：页面文字多寡不一时更稳。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else None
    if not root:
        cands = sorted((d for d in os.listdir("demo") if d.startswith("session_")),
                       key=lambda d: os.path.getmtime(os.path.join("demo", d)))
        root = os.path.join("demo", cands[-1]) if cands else None
    if not root or not os.path.exists(os.path.join(root, "timeline.json")):
        print("没找到 timeline.json")
        return 1

    with open(os.path.join(root, "timeline.json"), encoding="utf-8") as f:
        data = json.load(f)
    events = data.get("events") or []

    # ---- 聚类 ----
    sets, hub = key_sets(events)
    clusters = []
    assign = []
    for ev, s in zip(events, sets):
        best, best_v = None, 0.0
        for c in clusters:
            v = sim(s, c["set"])
            if v > best_v:
                best, best_v = c, v
        if best and best_v >= 0.62:
            best["members"].append(s)
            assign.append(best["id"])
        else:
            clusters.append({"id": len(clusters) + 1, "set": s, "members": [s]})
            assign.append(len(clusters))

    catalog = {}
    for c in clusters:
        cnt = Counter()
        for m in c["members"]:
            cnt.update(m)
        need = max(1, int(len(c["members"]) * 0.5))
        catalog[c["id"]] = {"key": [w for w, n in cnt.most_common() if n >= need][:8],
                            "visits": len(c["members"])}

    # ---- 点击流程（只保留点击，连续同页同位置合并）----
    steps = []
    for ev, cid in zip(events, assign):
        c = ev.get("click") or {}
        if c.get("rx") is None:
            continue
        pos = (round(c["rx"], 2), round(c["ry"], 2))
        if steps and steps[-1]["page"] == cid and steps[-1]["pos"] == pos:
            steps[-1]["n"] += 1
            steps[-1]["t_end"] = ev.get("t")
            continue
        steps.append({"page": cid, "pos": pos, "n": 1, "t": ev.get("t"),
                      "t_end": ev.get("t"), "img": ev.get("img")})

    out = [f"# 流程摘要 {os.path.basename(root)}", "",
           f"事件 {len(events)} → 聚类 {len(clusters)} 个页面 → 合并成 {len(steps)} 个点击步骤", "",
           "## 页面清单", ""]
    for cid, info in sorted(catalog.items(), key=lambda kv: -kv[1]["visits"]):
        out.append(f"- **P{cid}**（{info['visits']} 次）：{' / '.join(info['key']) or '（无文字）'}")

    out += ["", "## 点击流程", "", "| # | 页面 | 点击位置 | 次数 | 帧 |", "|---|---|---|---|---|"]
    for i, s in enumerate(steps, 1):
        out.append(f"| {i} | P{s['page']} | {s['pos'][0]}, {s['pos'][1]} | {s['n']} | {s['img']} |")

    path = os.path.join(root, "digest.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"页面 {len(clusters)}，点击步骤 {len(steps)} → {path}")
    for cid, info in sorted(catalog.items(), key=lambda kv: -kv[1]["visits"])[:12]:
        print(f"  P{cid}({info['visits']}次): {'/'.join(info['key'][:6])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
