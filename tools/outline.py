"""按时间顺序输出页面大纲：第一次出现的时间、特征文字、代表帧。

    .venv\\Scripts\\python.exe tools\\outline.py [session目录]

digest.md 是按次数排序的清单，这个按时间顺序，用来还原「你到底按什么顺序走」。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from analyze_demo import key_of, sim  # noqa: E402


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else None
    if not root:
        cands = sorted((d for d in os.listdir("demo") if d.startswith("session_")),
                       key=lambda d: os.path.getmtime(os.path.join("demo", d)))
        root = os.path.join("demo", cands[-1])
    with open(os.path.join(root, "timeline.json"), encoding="utf-8") as f:
        events = json.load(f).get("events") or []

    clusters, assign = [], []
    for ev in events:
        s = key_of(ev.get("texts") or [])
        best, bv = None, 0.0
        for c in clusters:
            v = sim(s, c["set"])
            if v > bv:
                best, bv = c, v
        if best and bv >= 0.62:
            best["visits"] += 1
            assign.append(best["id"])
        else:
            clusters.append({"id": len(clusters) + 1, "set": s, "visits": 1,
                             "first": ev.get("t"), "img": ev.get("img"),
                             "key": sorted(s)[:10]})
            assign.append(len(clusters))
    info = {c["id"]: c for c in clusters}

    lines = [f"# 页面大纲 {os.path.basename(root)}", "",
             f"共 {len(events)} 个事件，{len(clusters)} 个页面\n",
             "## 按时间顺序（同一页面只列一次）", ""]
    for c in clusters:
        lines.append(f"- **P{c['id']}**  t={c['first']}s  出现{c['visits']}次  `{c['img']}`")
        lines.append(f"  - {' / '.join(c['key']) or '（无文字）'}")

    # 紧凑跳转链：只保留换了页面的地方
    chain = []
    for ev, cid in zip(events, assign):
        if not chain or chain[-1][0] != cid:
            chain.append([cid, 1, ev.get("click")])
        else:
            chain[-1][1] += 1
    lines += ["", "## 页面跳转链", "", " → ".join(f"P{c}" + (f"×{n}" if n > 1 else "") for c, n, _ in chain[:120])]
    if len(chain) > 120:
        lines.append(f"\n（共 {len(chain)} 段，只显示前 120 段）")

    path = os.path.join(root, "outline.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"页面 {len(clusters)} 个，跳转 {len(chain)} 段 → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
