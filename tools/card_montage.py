import json, os
from PIL import Image, ImageDraw

root = "demo/session_1122_200929"
d = json.load(open(os.path.join(root, "timeline.json"), encoding="utf-8"))
seen, cores = set(), []
for e in d["events"]:
    s = " ".join(t[0] for t in e.get("texts", []))
    if ("失败率" in s or "决定" in s) and e["img"] not in seen:
        seen.add(e["img"]); cores.append(e)
print("训练核心不同帧数", len(cores))
sel = cores[::max(1, len(cores)//12)][:12]
os.makedirs("demo/montage", exist_ok=True)
crops = []
for e in sel:
    p = os.path.join(root, e["img"])
    if not os.path.exists(p): continue
    im = Image.open(p).convert("RGB"); w, h = im.size
    c = im.crop((int(w*0.18), int(h*0.48), int(w*0.64), int(h*0.88))).resize((560, 260))
    crops.append((e["img"], c))
if crops:
    H = sum(c.size[1]+22 for _, c in crops)
    sheet = Image.new("RGB", (580, H), (255,255,255)); dr = ImageDraw.Draw(sheet); y = 0
    for name, c in crops:
        dr.text((6, y+5), name, fill=(0,0,0)); sheet.paste(c, (10, y+20)); y += c.size[1]+22
    sheet.save("demo/montage/core_cards.png")
    print("saved demo/montage/core_cards.png", sheet.size, len(crops))
