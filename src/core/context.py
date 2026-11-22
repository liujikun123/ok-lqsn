"""读屏数值 → 上下文字典。

原则：宁可报「读不到」，也不要猜。任何一个关键数值读错都会让策略选错方案，
所以这里对每个数值都做区间校验，并且和上一帧比对跳变。
"""
import re
from datetime import datetime

from .policy import load_metrics

_UNIT = {"万": 10000, "w": 10000, "W": 10000, "亿": 100000000, "": 1}


def _norm(text):
    """全角转半角、去掉多余空白，OCR 常混着来。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"[ \t]+", " ", "".join(out))


def _num(raw, unit=""):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v * _UNIT.get((unit or "").strip(), 1)


DEFAULT_SIZE = (1920, 1080)


def _in_region(cx, cy, region):
    if not region:
        return True
    if cx is None or cy is None:
        return True  # 只有文本没有坐标，不拿位置做否决
    x1, y1, x2, y2 = region
    return x1 <= cx <= x2 and y1 <= cy <= y2


def normalize_boxes(items, size=DEFAULT_SIZE):
    """把 ok 的 Box / (text,x,y,w,h) 元组 / 纯字符串，统一成 [{'text','cx','cy'}]。
    cx/cy 为归一化中心点，无坐标时为 None。"""
    W, H = size
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append({"text": it, "cx": None, "cy": None})
            continue
        if hasattr(it, "name"):  # ok 的 Box 对象
            x, y, w, h = it.x, it.y, it.width, it.height
            text = getattr(it, "name", "") or ""
        else:
            try:
                text, x, y, w, h = it[0], float(it[1]), float(it[2]), float(it[3]), float(it[4])
            except Exception:
                continue
        out.append({"text": text, "cx": (x + w / 2) / W, "cy": (y + h / 2) / H})
    return out


def _match_field(spec, boxes):
    """按 pattern 顺序 × 区域内所有文本，返回 (match对象, 命中的正则, box)。"""
    region = spec.get("region")
    for pat in spec.get("pattern") or []:
        rx = re.compile(pat)
        for b in boxes:
            if not _in_region(b["cx"], b["cy"], region):
                continue
            m = rx.search(_norm(b["text"]))
            if m:
                return m, pat, b
    return None, None, None


def parse_fields(items, metrics=None, size=DEFAULT_SIZE):
    """把 OCR 结果解析成 {字段名: 值}，同时返回每个字段的命中情况。"""
    metrics = metrics or load_metrics()
    boxes = normalize_boxes(items, size)
    values, hits = {}, {}
    for name, spec in metrics.items():
        m, pat, box = _match_field(spec, boxes)
        if not m:
            hits[name] = {"ok": False, "reason": "未匹配到"}
            continue
        try:
            post = spec.get("post")
            if post == "date_cn":
                g = [int(x) for x in m.groups() if str(x).isdigit()]
                if len(g) >= 3:
                    val = datetime(g[0], g[1], g[2]).date()
                elif len(g) == 2:
                    val = datetime(datetime.now().year, g[0], g[1]).date()
                else:
                    raise ValueError("日期分组不足")
            else:
                vg = spec.get("value_group", 1)
                unit = ""
                if spec.get("unit_group"):
                    unit = m.group(spec["unit_group"]) or ""
                raw = m.group(vg)
                if raw is None:
                    raise ValueError("取值为空")
                val = _num(raw, unit)
                if val is None:
                    raise ValueError(f"无法转数字: {raw}")
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
        except Exception as e:
            hits[name] = {"ok": False, "reason": f"解析失败: {e}"}
            continue

        lo, hi = (spec.get("range") or [None, None])[:2]
        if lo is not None and val < lo or hi is not None and val > hi:
            hits[name] = {"ok": False, "reason": f"超出合理区间 {lo}~{hi}，判为读错：{val}"}
            continue

        if spec.get("max_group"):
            try:
                mx = _num(m.group(spec["max_group"]))
                if mx:
                    values[name + "上限"] = int(mx) if float(mx).is_integer() else mx
            except Exception:
                pass

        values[name] = val
        hits[name] = {"ok": True, "raw": m.group(0), "pattern": pat,
                      "pos": (round(box["cx"], 3), round(box["cy"], 3)) if box["cx"] is not None else None}
    return values, hits


def build_context(items, prev=None, now=None, metrics=None, size=DEFAULT_SIZE, jump_limit=40):
    """生成策略表达式能用的上下文。items 可以是 ok 的 Box 列表，也可传纯文本（无位置校验）。
    prev 用于跳变检测；stale_seconds 用来标记「这个数值是多久前读到的」。"""
    now = now or datetime.now()
    values, hits = parse_fields(items, metrics, size)
    issues = []

    stamina = values.get("体力")
    stamina_max = values.get("体力上限")
    limit = jump_limit
    if prev and stamina is not None and prev.get("stamina") is not None:
        if abs(stamina - prev["stamina"]) > limit:
            issues.append(f"体力跳变过大：{prev['stamina']} → {stamina}，疑似读错，沿用上一帧")
            stamina = prev["stamina"]

    if stamina is None:
        issues.append("体力没读到（关键数值），按配置应暂停")

    ratio = 0.0
    if stamina is not None and stamina_max:
        ratio = round(stamina / stamina_max, 3)

    game_date = values.get("游戏内日期")
    date_str = (game_date.isoformat() if hasattr(game_date, "isoformat") else None) or now.strftime("%Y-%m-%d")
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        d = now.date()

    ctx = {
        "stamina": stamina,
        "stamina_max": stamina_max,
        "stamina_ratio": ratio,
        "gold": values.get("金币"),
        "diamond": values.get("钻石"),
        "tokens": values.get("剩余次数"),
        "weekday": d.isoweekday(),
        "is_weekend": d.isoweekday() >= 6,
        "days_left": 7 - d.isoweekday(),
        "hour": now.hour,
        "date_str": d.isoformat(),
    }
    return ctx, hits, issues
