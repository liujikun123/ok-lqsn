"""从已有截图 + OCR 自动生成框架锚点库（assets/coco_annotations.json + anchors.json）。

思路（2026-11-24 玩家拍板「全用框架的」）：
    框架 ok.feature.FeatureSet 支持 COCO 标注 → 多尺度模板匹配。
    但它需要「整张截图 + bbox + 类别名」的标注，手工点太慢。
    这里反过来：先用 OCR 找到某个稳定文字（如「决定」「训练评分」）的框，
    把它裁成锚点（bbox 写进 COCO），运行时框架只做像素匹配，**不再读文字**。
    OCR 只在建库这一步用，跑起来是纯视觉。

框架的一个硬约束（看 FeatureSet.read_from_json 源码确认）：
    一个 category 只能有一个框，同名两个框直接 raise「Multiple boxes found」。
    所以每个锚点只存 1 张原型，另存一份「搜索区域」（多个素材 bbox 的并集），
    运行时用相对坐标把匹配范围限制在这块区域里 —— 又快又不会满屏乱匹配。

用法：
    python tools/build_anchors.py            # 重新生成（会备份旧 json）
    python tools/build_anchors.py --verify   # 只验证：用框架自己的匹配器测命中
"""
import glob
import json
import os
import shutil
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
IMAGES = os.path.join(ASSETS, "images")
COCO = os.path.join(ASSETS, "coco_annotations.json")
ANCHORS = os.path.join(ASSETS, "anchors.json")
CACHE = os.path.join(ASSETS, "ocr_cache.json")

# (页面, 锚点名, 关键词, 素材 glob, 该页需要命中几个锚点才算认定)
SPEC = [
    # ===== 2026-11-24 补：开训前三连 + 大厅 + 结算 =====
    # 这三页 OCR 一直不稳：器材页/笔记页左侧标题常被读成「选择器材 @200,59」这种带噪字的串，
    # 选队友页的标题干脆就是 tab 名「训练核心」（和训练核心页同名 → 判页容易串）。
    # 用像素锚点就不读字了。**放在 SPEC 最前面**：anchors.json 的页面顺序 = 这里的顺序，
    # 命中多个页面时取第一个，越具体的越要往前放。
    ("选择队友", "选择队友_历史阵容", "历史玩家阵容", "screenshots/auto/*_slots_pick.png", 1),
    ("笔记选择", "笔记选择_标题",   "选择笔记",   "screenshots/auto/*_slots_note.png", 1),
    ("器材选择", "器材选择_标题",   "选择器材",   "screenshots/auto/*_slots_equip.png", 1),
    # 大厅：用底部功能栏两个固定词，need=2 → 两个都命中才认，避免单字误配
    ("大厅",     "大厅_剧情",       "剧情",       "screenshots/auto/*_done_hall.png", 2),
    ("大厅",     "大厅_角色",       "角色",       "screenshots/auto/*_done_hall.png", 2),
    ("恭喜获得", "恭喜获得_标题",   "恭喜获得",   "logs/scores/*gain*.png", 1),
    # ===== 原有 =====
    # 「决定」是右侧黄色主按钮，位置/底纹稳定，实测命中 0.82+。
    # 「失败率0%」不能当锚点：OCR 框里带着百分比数字，0%/5%/12% 像素全不一样，
    # 实测跨截图匹配失败 —— 这类「数值会变」的文字天生不适合做锚点。
    ("训练核心", "训练核心_决定",   "决定",   "logs/audit/*/W*D*/*core*.png", 1, True),
    # 商店页按钮文字 OCR 常读不到（没选中商品时没有「购买」二字），
    # 但标题「商店」和右上角关闭 X 一直在，用这两个更稳。
    ("商店",     "商店_标题",       "商店",   "screenshots/auto/*_shop.png", 1),
    ("商店",     "商店_关闭",       "X",      "screenshots/auto/*_shop.png", 1),
    ("训练评分", "训练评分_标题",   "训练评分", "logs/scores/*rating*.png", 1),
    ("训练评分", "训练评分_继续",   "点击屏幕继续", "logs/scores/*rating*.png", 1),
]

CANON = (1600, 900)   # 只收这个尺寸的素材 = 框架实际出帧尺寸（resize_to 1600x900）
MAX_SPLIT = 3      # 同一页面最多拆几套版式锚点（框架一类别只能一张原型，靠多锚点覆盖多版式）
PAD = 3            # 裁框外扩像素（留点边，模板匹配更稳）
REGION_PAD = 0.06  # 搜索区域在并集基础上再放宽一点（相对整幅）


def _engine():
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr
    return ONNXPaddleOcr(use_angle_cls=False, use_npu=True, use_openvino=True)


def ocr_rows(eng, path):
    """返回 [(x1,y1,x2,y2,text,conf)]；按文件路径缓存，重跑不用再算。"""
    cache = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    key = os.path.abspath(path)
    if key in cache:
        return [tuple(r) for r in cache[key]]
    img = cv2.imread(path)
    if img is None:
        return []
    res = eng.ocr(img)
    rows = []
    for pos, item in (res[0] if res else []):
        txt, conf = item if isinstance(item, (tuple, list)) else (item, 1.0)
        xs = [p[0] for p in pos]
        ys = [p[1] for p in pos]
        rows.append((float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)),
                     str(txt).strip(), float(conf)))
    cache[key] = rows
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return [tuple(r) for r in rows]


def pick_box(rows, keyword):
    """在一堆 OCR 结果里找关键词所在的框。优先完全等于，其次包含。"""
    exact = sorted([r for r in rows if r[4] == keyword], key=lambda r: -r[5])
    part = sorted([r for r in rows if keyword in r[4]], key=lambda r: -r[5])
    hit = (exact or part or [None])[0]
    return hit[:4] if hit else None


def build():
    # 1) 收集每个锚点在所有素材里的候选框
    found = {}
    for spec in SPEC:
        page, name, kw, pattern, need = spec[:5]
        files = sorted(glob.glob(os.path.join(ROOT, pattern)))
        if not files:
            print(f"  跳过 {name}：没有素材 {pattern}")
            continue
        found.setdefault(name, {"page": page, "need": need, "boxes": [], "size": (0, 0)})
    if not found:
        print("SPEC 里一个能用的素材都没有")
        return 1

    eng = _engine()
    for spec in SPEC:
        page, name, kw, pattern, need = spec[:5]
        if name not in found:
            continue
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            img = cv2.imread(path)
            if img is None:
                continue
            box = pick_box(ocr_rows(eng, path), kw)
            if box is None:
                continue
            h, w = img.shape[:2]
            # 混入其它分辨率的截图会让模板尺度对不上（实测 1920 的素材匹配 1600 的帧全灭），
            # 所以只收和实际出帧同尺寸的素材。
            if (w, h) != CANON:
                continue
            x1 = max(0, int(box[0]) - PAD)
            y1 = max(0, int(box[1]) - PAD)
            x2 = min(w, int(box[2]) + PAD)
            y2 = min(h, int(box[3]) + PAD)
            if x2 - x1 < 12 or y2 - y1 < 8:
                continue
            found[name]["boxes"].append((path, x1, y1, x2, y2))
            found[name]["size"] = (w, h)

    # 2) 每个锚点挑最有代表性的原型 + 算搜索区域（候选框并集）
    os.makedirs(IMAGES, exist_ok=True)
    images, annotations, categories = [], [], []
    anchor_meta = {}
    img_id = 0

    def emit(name, page, need, w, h, boxes):
        """把一簇同位置候选做成一个锚点（一张原型图 + 一个 COCO 框 + 搜索区域）。"""
        nonlocal img_id
        ux1 = min(b[1] for b in boxes)
        uy1 = min(b[2] for b in boxes)
        ux2 = max(b[3] for b in boxes)
        uy2 = max(b[4] for b in boxes)
        cx, cy = (ux1 + ux2) / 2.0, (uy1 + uy2) / 2.0
        # 原型 = 离并集中心最近的那一框（最常见的出现位置）
        path, x1, y1, x2, y2 = min(
            boxes, key=lambda b: abs((b[1] + b[3]) / 2 - cx) + abs((b[2] + b[4]) / 2 - cy))

        img_id += 1
        fname = f"anchor_{img_id:03d}.png"
        shutil.copyfile(path, os.path.join(IMAGES, fname))
        images.append({"id": img_id, "file_name": f"images/{fname}", "width": w, "height": h})
        cid = len(categories) + 1
        categories.append({"id": cid, "name": name, "supercategory": page})
        annotations.append({
            "id": len(annotations) + 1, "image_id": img_id, "category_id": cid,
            "bbox": [x1, y1, x2 - x1, y2 - y1],
            "area": (x2 - x1) * (y2 - y1), "iscrowd": 0,
        })

        px, py = REGION_PAD * w, REGION_PAD * h
        rx1 = max(0, ux1 - px) / w
        ry1 = max(0, uy1 - py) / h
        rx2 = min(w, ux2 + px) / w
        ry2 = min(h, uy2 + py) / h
        anchor_meta[name] = {
            "page": page, "need": need,
            "region": [round(rx1, 4), round(ry1, 4), round(rx2 - rx1, 4), round(ry2 - ry1, 4)],
            "proto": os.path.basename(path),
            "candidates": len(boxes),
            "srcs": sorted({os.path.relpath(b[0], ROOT).replace("\\", "/") for b in boxes}),
        }
        print(f"  + {name:18s} 原型={os.path.basename(path)} 候选={len(boxes)} "
              f"区域=({rx1:.2f},{ry1:.2f},{rx2-rx1:.2f},{ry2-ry1:.2f})")

    for spec in SPEC:
        name, pattern = spec[1], spec[3]
        split = bool(spec[5]) if len(spec) > 5 else False
        info = found.get(name)
        if not info:
            continue
        boxes = info["boxes"]
        if not boxes:
            print(f"  ! {name}: 所有素材都没找到关键词，跳过")
            continue
        # 只保留「同一个 UI 元素」的那一群：同名关键词会撞到剧情文字/弹窗里的同一串字
        # （「决定」就撞上过「球队经理的决定」），它们的尺寸和位置都跟按钮差很远。
        # 按尺寸分桶取最大那一簇 —— 按钮/标题永远是同一块区域、同一个字号。
        buckets = {}
        for b in boxes:
            key = (round((b[3] - b[1]) / 10), round((b[4] - b[2]) / 10))
            buckets.setdefault(key, []).append(b)
        main = max(buckets.values(), key=len) if buckets else boxes
        if len(main) < len(boxes):
            print(f"  · {name}: {len(boxes)} 个候选里有 {len(boxes) - len(main)} 个尺寸不同（疑似撞词），已剔除")
        # 再按**出现位置**聚类：同一个按钮在不同截图里位置几乎不变（±十几个像素），
        # 剧情文字里的同名串则会散落在屏幕各处。
        pos = {}
        for b in main:
            key = (round((b[1] + b[3]) / 2 / 14), round((b[2] + b[4]) / 2 / 14))
            pos.setdefault(key, []).append(b)
        clusters = sorted(pos.values(), key=len, reverse=True)
        if len(clusters) > 1:
            print(f"  · {name}: 位置聚成 {len(clusters)} 簇（最大的 {len(clusters[0])}/{len(main)}）")
        w, h = info["size"]
        if split and len(clusters) > 1:
            # **一个页面的同一个按钮会有好几套版式**（训练核心按技能不同，决定键位置不一样），
            # 框架一个类别只能存一张原型，所以：
            #   ① 先出一个「宽域」锚点（并集区域，覆盖多套版式，命中最广）；
            #   ② 再按位置簇各出一个「窄域」锚点（区域小、几乎不会误配）。
            # 页面 need=1 → 命中任意一个就认这页。
            # 这是 2026-11-24 实测 263 帧得出的结论：宽域一个能覆盖大部分，
            # 少数版式（IQ 页）靠窄域补；窄域单独用只有 35%。
            emit(name, info["page"], 1, w, h, main)
            for k, cluster in enumerate(clusters[:MAX_SPLIT]):
                emit(f"{name}_{k + 1}", info["page"], 1, w, h, cluster)
        else:
            emit(name, info["page"], info["need"], w, h, clusters[0])

    if not annotations:
        print("没生成任何锚点")
        return 1

    for p in (COCO, ANCHORS):
        if os.path.exists(p):
            shutil.copyfile(p, p + ".bak")
    json.dump({"info": {"description": "bsg-auto 页面锚点（OCR 自动生成）"},
               "images": images, "annotations": annotations, "categories": categories},
              open(COCO, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    pages = {}
    for name, meta in anchor_meta.items():
        d = pages.setdefault(meta["page"], {"anchors": [], "need": meta["need"]})
        d["anchors"].append(name)
        d["need"] = max(d["need"], meta["need"])
    json.dump({"version": 1, "pages": pages, "anchors": anchor_meta},
              open(ANCHORS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n写入 {COCO}：{len(categories)} 个锚点")
    print(f"写入 {ANCHORS}：{len(pages)} 个页面")
    for page, d in pages.items():
        print(f"  {page}: 需命中 {d['need']}/{len(d['anchors'])} → {d['anchors']}")
    return 0


def _canon(pat):
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, pat))):
        im = cv2.imread(f)
        if im is not None and im.shape[:2][::-1] == CANON:
            out.append(f)
    return out


def _fs():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(ROOT))))
    from ok.feature.FeatureSet import FeatureSet
    return FeatureSet(debug=False, coco_json=COCO,
                      default_horizontal_variance=0.002,
                      default_vertical_variance=0.002, default_threshold=0.8)


def _hit(fs, meta, name, path):
    img = cv2.imread(path)
    if img is None or img.shape[:2][::-1] != CANON:
        return None
    r = meta["anchors"][name]["region"]
    return fs.find_feature(img, name, 0.002, 0.002, 0.8,
                           x=r[0], y=r[1], width=r[2], height=r[3])


def verify():
    """离线验证（2026-11-24 重写）：按 anchors.json 实际内容跑，不再手工维护测试表。

    两件事都要看：
      1) **命中率** —— 该锚点在自己那批素材（srcs）里能命中几成，<70% 说明原型挑得不好；
      2) **误命中** —— 拿别的页面的帧去匹配这个锚点，必须一个都不中
         （锚点抢判页比「不命中」危险得多：不命中只是退回 OCR，误命中是直接判错页）。
    """
    meta = json.load(open(ANCHORS, encoding="utf-8"))
    fs = _fs()
    anchors = meta.get("anchors") or {}

    def frames_of(name):
        return [os.path.join(ROOT, s) for s in (anchors[name].get("srcs") or [])]

    bad = 0
    print("== 1. 命中率（自己那批素材，最多取 6 帧）==")
    for name, a in anchors.items():
        files = [p for p in frames_of(name) if os.path.exists(p)]
        if not files:
            print(f"  SKIP {name:20s} 素材路径找不到（logs/ 下的帧在别的目录）")
            continue
        sample = files[::max(1, len(files) // 6)][:6]
        hit = sum(1 for p in sample if _hit(fs, meta, name, p))
        rate = hit / len(sample)
        if rate < 0.7:
            bad += 1
            print(f"  WEAK {name:20s} {hit}/{len(sample)} = {rate:.0%}（素材共 {len(files)} 帧）")
        else:
            print(f"  PASS {name:20s} {hit}/{len(sample)} = {rate:.0%}（素材共 {len(files)} 帧）")

    print("\n== 2. 误命中（拿别的页面的帧来匹配，必须 0）==")
    by_page = {}
    for name, a in anchors.items():
        by_page.setdefault(a["page"], []).extend(frames_of(name))
    for name, a in anchors.items():
        others = []
        for page, fl in by_page.items():
            if page == a["page"]:
                continue
            fl = [p for p in fl if os.path.exists(p)]
            others.extend(fl[::max(1, len(fl) // 3)][:3])
        hits = [os.path.basename(p) for p in others if _hit(fs, meta, name, p)]
        if hits:
            bad += 1
            print(f"  FAIL {name:20s} 误命中 {len(hits)} 帧：{hits[:3]}")
        else:
            print(f"  PASS {name:20s} 与其它页面的 {len(others)} 帧无一误命中")
    print(f"\n{'全部通过' if not bad else f'有 {bad} 项要处理'}")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(verify())
    sys.exit(build())
