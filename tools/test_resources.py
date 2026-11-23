"""A: 资源识别去硬编码 —— 全离线，不动游戏。

验证：① 页面上下文 hint 优先于数字；② 上限被道具提升后能跟上；
③ OCR 小数抖动不误改上限；④ 溢出值仍认得出；⑤ 随机大数不被当资源。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import AutoTask

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


class Box:
    height = 30


class Stub:
    FOCUS_CAP = 150
    STAMINA_CAP = 130
    CAPS = (150, 130)
    PAGE_RESOURCE = AutoTask.PAGE_RESOURCE
    RESOURCE_ROI = AutoTask.RESOURCE_ROI
    RESOURCE_B_MAX = AutoTask.RESOURCE_B_MAX

    def __init__(self):
        self.stamina = None
        self.stamina_max = None
        self.out_focus = None
        self.out_focus_max = None

    _ab_kind = AutoTask._ab_kind
    _parse_ab = AutoTask._parse_ab
    _settle_cap = AutoTask._settle_cap
    _read_stamina = AutoTask._read_stamina


def items(*texts, kind="stamina"):
    """按真实槽位放坐标：stamina 在顶栏左 (x≈490,y≈75)，focus 在顶栏右 (x≈1450,y≈80)。"""
    x = 490 if kind == "stamina" else 1450
    return [{"t": t, "cx": x + i * 8, "cy": 75, "b": Box()} for i, t in enumerate(texts)]


def body_item(text, y=970):
    """模拟底部属性条上的数字（不在任何资源槽位）。"""
    return {"t": text, "cx": 500, "cy": y, "b": Box()}


frame = __import__("numpy").zeros((1119, 1936, 3), dtype="uint8")

print("--- 1. 页面上下文 hint 优先于数字 ---")
# 每日页：38/148 —— 148 贴着 focus 上限，但每日页一定是局内精力。
# 上限值按 OCR 采纳（148 可能是被道具提升，也可能是读花，但归属必须是精力）。
s = Stub()
s._read_stamina(items("38/148"), frame, page="每日页")
check("每日页 38/148 → 局内精力(不写成focus)", s.stamina, 38)
check("每日页不写局外专注", s.out_focus, None)
check("上限按 OCR 采纳", s.stamina_max, 148)
# 专注弹窗：202/138 —— 138 贴着 stamina 上限，但弹窗一定是局外专注
s = Stub()
s._read_stamina(items("202/138"), frame, page="专注恢复")
check("专注弹窗 202/138 → 局外专注(溢出)", (s.out_focus, s.out_focus_max), (202, 150))
check("专注弹窗不写局内精力", s.stamina, None)

print("--- 2. 上限被道具提升后能跟上 ---")
s = Stub()
s._read_stamina(items("55/160"), frame, page="每日页")
check("精力上限提升到 160 被采纳", s.stamina_max, 160)
check("精力当前值", s.stamina, 55)
# 连续两帧：先 160，再 165 —— 第二次也应该跟上
s._read_stamina(items("50/165"), frame, page="每日页")
check("上限再涨到 165 也跟上", s.stamina_max, 165)

print("--- 3. OCR 小数抖动不误改上限 ---")
s = Stub()
s._read_stamina(items("40/130"), frame, page="每日页")
s._read_stamina(items("38/134"), frame, page="每日页")
check("134 判成抖动，保留 130", s.stamina_max, 130)
s._read_stamina(items("36/127"), frame, page="每日页")
check("127 判成抖动，保留 130", s.stamina_max, 130)

print("--- 4. 无 hint 时数字指纹仍能兜底 ---")
s = Stub()
s._read_stamina(items("90/130"), frame, page=None)
check("无 hint 130 → 局内精力", (s.stamina, s.stamina_max), (90, 130))
s = Stub()
s._read_stamina(items("120/150", kind="focus"), frame, page=None)
check("无 hint 150 → 局外专注", (s.out_focus, s.out_focus_max), (120, 150))

print("--- 5. 随机大数不被当资源 ---")
check("303/164 不认", Stub()._ab_kind(303, 164, hint=None), None)
check("218/271 不认", Stub()._ab_kind(218, 271, hint=None), None)

print("--- 6. 按位置读：底部属性条/弹窗外数字进不来（291/253 异常回归）---")
# 玩家实跑抓到的异常：每日页精力读出 291/253，其实是底部属性条数字混进来。
s = Stub()
bad = items("93/130") + [body_item("1216/2533"), body_item("650/1670"), body_item("284/178")]
s._read_stamina(bad, frame, page="每日页")
check("精力只读顶栏槽，不被底部属性条污染", (s.stamina, s.stamina_max), (93, 130))
check("属性条不写专注", s.out_focus, None)
# 顶栏右槽=专注，即使页面 hint 说 stamina 也按位置判（位置比 hint 硬）
s = Stub()
s._read_stamina(items("30/150", kind="focus"), frame, page="每日页")
check("顶栏右槽按位置判专注(不被hint带偏)", (s.out_focus, s.out_focus_max), (30, 150))
# b>320 的属性条即使恰好落在槽位里也不认
s = Stub()
s._read_stamina(items("284/1784"), frame, page="每日页")
check("b=1784 超硬上限不认", s.stamina, None)
# 错位：精力槽读到 b=290（远超精力合理范围）→ OCR 串行，丢掉
s = Stub()
s._read_stamina(items("93/290"), frame, page="每日页")
check("精力槽读到离谱上限→丢弃", s.stamina, None)
# 正则不许把属性条 284/1784 截断成 284/178
check("284/1784 不被截断误读", Stub()._parse_ab("284/1784"), None)
# 专注弹窗没有顶栏，跳过位置检查仍能读（弹窗里数值在画面中央）
s = Stub()
s._read_stamina([{"t": "202/150", "cx": 960, "cy": 500, "b": Box()}], frame, page="专注恢复")
check("专注弹窗中央数字仍能读", (s.out_focus, s.out_focus_max), (202, 150))

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
