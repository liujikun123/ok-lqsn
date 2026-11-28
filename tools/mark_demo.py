"""把一次录制里的点击/拖拽直接画回原图，便于人眼确认点的是哪个控件。

用法：
    ../ok-bsg/.venv/Scripts/python.exe tools/mark_demo.py demo/session_1127_152728
产出：
    <session>/marked/<序号>_<kind>.png   原尺寸标注图（红圈=点击，红箭头=拖拽）
    <session>/marked/index.txt           每张图对应的动作说明
"""
import json
import os
import sys

import cv2

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "demo/session_1127_152728"
    evs = [json.loads(l) for l in open(os.path.join(root, "events.jsonl"), encoding="utf-8")]
    out = os.path.join(root, "marked")
    os.makedirs(out, exist_ok=True)
    lines = []
    for ev in evs:
        if ev["kind"] not in ("click", "drag"):
            continue
        img = cv2.imread(os.path.join(root, ev["img"]))
        if img is None:
            continue
        h, w = img.shape[:2]
        p = (int(ev["rx"] * w), int(ev["ry"] * h))
        if ev["kind"] == "click":
            cv2.circle(img, p, 34, (0, 0, 255), 5)
            cv2.line(img, (p[0] - 46, p[1]), (p[0] + 46, p[1]), (0, 0, 255), 2)
            cv2.line(img, (p[0], p[1] - 46), (p[0], p[1] + 46), (0, 0, 255), 2)
            cv2.putText(img, f"{ev['i']} click ({ev['rx']:.3f},{ev['ry']:.3f})", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
        else:
            q = (int(ev["ux"] * w), int(ev["uy"] * h))
            for a, b in zip(ev["path"][:-1], ev["path"][1:]):
                cv2.line(img, (int(a[0] * w), int(a[1] * h)), (int(b[0] * w), int(b[1] * h)),
                         (0, 255, 255), 2)
            cv2.arrowedLine(img, p, q, (0, 0, 255), 6, tipLength=0.05)
            cv2.circle(img, p, 14, (0, 255, 255), 4)
            cv2.putText(img, f"{ev['i']} drag dy={ev['uy'] - ev['ry']:+.3f}", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
        name = f"{ev['i']:04d}_{ev['kind']}.png"
        cv2.imwrite(os.path.join(out, name), img)
        lines.append(f"{name}  {ev['kind']}  px({p[0]},{p[1]})  rel({ev['rx']:.3f},{ev['ry']:.3f})"
                     + (f" -> rel({ev['ux']:.3f},{ev['uy']:.3f})" if ev["kind"] == "drag" else ""))
    with open(os.path.join(out, "index.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[+] {len(lines)} 张标注图 -> {os.path.abspath(out)}")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
