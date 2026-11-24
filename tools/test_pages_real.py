"""真实素材驱动的页面识别回归测试 —— 全离线，不动游戏。

素材来源（2026-11-23）：
  * logs/pick_list/*, logs/grid_list/*, screenshots/auto/*  —— 真实截图 OCR 实测
  * logs/ok-script.log —— 一整轮实跑的「页面：X | 文字: [...]」记录
把「程序实际读到的文字」固化成夹具，验证 _classify 判对。
这样每次改识别规则，都能立刻知道有没有把某个真实页面判错。

用法：python tools/test_pages_real.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tasks.AutoTask import AutoTask, is_attr_panel

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


def items(texts, w=1920, right_skills=True):
    """模拟 _texts 输出：给每个词一个 x 坐标。
    right_skills=True（默认）——技能名放右侧，模拟训练核心页的技能列；
    right_skills=False ——属性名放中间，模拟底部属性条（开训/成长动画）。
    """
    out = []
    for i, t in enumerate(texts):
        cx = 100 + i * 20
        if t in ("投射", "弹跳", "运控", "体能", "防守", "意识"):
            cx = (1500 + i * 20) if right_skills else (400 + i * 20)
        out.append({"t": t, "cx": cx, "cy": 500, "b": Box()})
    return out


s = AutoTask.__new__(AutoTask)

# ---------------------------------------------------------------- 1. 真实 OCR 的明确页面
# 这些是从素材截图跑 onnxocr 读到的真实文字（已验证），期望分类：
print("--- 1. 真实截图 OCR → 页面分类 ---")
REAL_CASES = [
    ("选择队友", "选择队友 队伍能力 筛选 OFF SSR 大前 训练流 SSR百目鬼 小前 控卫 SSR希雅 中锋 重置 自动选择 玩家阵容 投射 D 193 体能 D 160 弹跳 316 防守 164 下一步 运控 325 意识 188"),
    ("笔记选择", "剩余3次 剩余1次 选择笔记 我的 好友 协 筛选 品质 OFF 中锋 体能 +67 投射 +85 投射上限+148 评分 737 638 631 运控 +74 契合度 良 重置 自动选择 投射D 193 下一步"),
    ("器材选择", "剩余0次 剩余3次 选择器材 我的 好友 协会 筛选 OFF 50 5 12 140 明日 LV 重置 自动选择 投射D 193 弹跳C+316 防守D 下一步 运控C+ 475 意识D 188"),
    ("大厅", "651.3万 1.7万 15/150 Qi 刘平安 糖果屋通关了表演赛决赛场 传奇·S 145993 NEW 活动 任务 助教 商店 商城 飞跃计划 协会赛 表演赛进行中 冠军之路 赛事 社团GO 训练 薪资 协会 剧情 角色 卡册"),
    ("商店", "第1周 星期二 113/130 120 -30 商店 6天 精力饮料 专属饮料 防守秘籍 剩余 2 30 60 体能秘籍 意识秘籍 怪味饮料 精力+20 上新时间 8天后 能力详情 运控 意识"),
    # 15:21 实跑：出行结果剧情页，周切换瞬间「星期-」未刷新，OCR 读到属性涨幅
    ("行程结果", "第3周 星期- 130/130 90 3/7 空之丘女高 坚持不制霜 弹跳+20， 防守+20"),
    # 15:25 实跑：训练后属性成长动画，读不到「心·技·体」，靠 数值/+涨幅 认
    ("训练结果", "+13+13 +6 投射 投射 仏运控 运控 投射 S 1011/+26 体能B490 防守 运控 S 1101/+12", False),
    # 15:27 实跑：开训动画（球场+底部属性条），多属性名+无任何按钮 → 过场
    ("训练结果", "35 投射 211/1928 体能 490 /1670 防守 - 运控 S 1,185 /1745", False),
    # 15:30 实跑：公园野餐确认弹窗，底部也挂着属性条，但有「前往野餐」按钮 → 前往（不能当过场等）
    ("前往", "第4周 星期日 45/130 30 7/9 1天 野餐 精力+70 前往野餐 1158 2538 04 运控 1195/1745 意识 466/1640", False),
]
for case in REAL_CASES:
    want, text = case[0], case[1]
    right = case[2] if len(case) > 2 else True
    got = s._classify(items(text.split(), right_skills=right), None)
    check(f"真实OCR→{want}", got, want)

# 14:20 卡死那张：顶部 OCR 成「训练核心」+「额外队友」+「筛选」，
# 必须判「选择队友」而不是训练核心/训练结果（否则又会对着屏幕中央空转）。
stuck = ("训练核心 训练流 筛选 OFF 额外队友 分卫 100 大前 110 控卫 LV 中锋 100 "
         "SSR希雅 编队 历史玩家阵容 投射E+146 体能D 169 弹跳C+334 下一步 防守 C+ 312 运控D 153 意识D 183")
check("卡死页(额外队友)→选择队友", s._classify(items(stuck.split()), None), "选择队友")

# 16:18 实跑：商店点完购买按钮后弹「购买并使用XX」提示，OCR 只读到顶栏 + 「购买并」
# 共 6 段、没有任何行动按钮。以前被 12.1 当每日页 → _do_daily 找不到按钮点中间 →
# 点回商品重复购买。修复后应判「未知」→ overlay「商店购买浮层」等待消失。
buy_toast = "第6周 星期三 99/130 95 数昆三 购买并".split()
check("商店购买浮层→未知(非每日页)", s._classify(items(buy_toast), None), "未知")
# 反向：真·每日页带「购买并使用XX」残留提示，但右下角有训练按钮 → 仍判每日页
# 按钮必须在右下 (x>0.6w, y>0.45h)，frame 不能是 None（否则 _action_btn 直接失效）
import numpy as _np2
_day_frame = _np2.zeros((1119, 1936, 3), dtype="uint8")
_day_words = "第1周 星期二 113/130 60 数早三 购买并使用防守秘籍 训练".split()
_day_items = [{"t": t, "cx": (1750 if t == "训练" else 200 + i * 20),
               "cy": (950 if t == "训练" else 500), "b": Box()}
              for i, t in enumerate(_day_words)]
check("每日页带购买提示→每日页(非浮层)", s._classify(_day_items, _day_frame), "每日页")

# 16:06 实跑：记者采访二选一，「体能+10 / 50%概率体能+40」挂在画面下部两个选项上。
# 这两条含「属性+数字」，会被 7.6 通用涨幅规则抢成行程结果 → 点中间空转 15 步。
# 用带坐标的 frame 区分：收益说明在 cy>0.78h 且横向分开 → 选项页。
import numpy as _np
_fake_frame = _np.zeros((1119, 1936, 3), dtype="uint8")
def _items_at(texts, coords):
    return [{"t": t, "cx": coords.get(t, (100, 100))[0],
             "cy": coords.get(t, (100, 100))[1], "b": Box()} for t in texts]
choice_words = "第3周 星期四 体能+10 老实回答 黑红也是红 50%概率体能+40".split()
choice_coords = {"体能+10": (676, 1005), "50%概率体能+40": (1261, 1006),
                 "老实回答": (676, 939), "黑红也是红": (1263, 940)}
check("记者采访二选一→选项", s._classify(_items_at(choice_words, choice_coords), _fake_frame), "选项")
trip_words = "第3周 星期- 空之丘女高 弹跳+20, 防守+20".split()
trip_coords = {"弹跳+20,": (400, 500), "防守+20": (600, 500)}
check("出行结果涨幅在正文→行程结果(不被抢)", s._classify(_items_at(trip_words, trip_coords), _fake_frame), "行程结果")

# 21:34 实跑（screenshots/auto/213412_stuck_106.png）：平板支撑事件二选一，
# 收益是**资源词**「精力+30」/「❤体能+30」而不是六项属性。旧 7.6b 词表只认
# 属性 → 只数到 1 条收益（≥2 才判选项）→ 被 7.6 抢成「行程结果」，
# _do_next 找不到主按钮点屏幕中间，连点 15 步收工（本轮 0/1 提前停）。
plate_words = "第7周 星期四 92/130 120 算了，先放过她 让她挑战一下极限 精力+30 ❤体能+30".split()
plate_coords = {"精力+30": (667, 974), "❤体能+30": (1254, 974),
                "算了，先放过她": (670, 908), "让她挑战一下极限": (1256, 906)}
check("平板支撑二选一(资源收益)→选项", s._classify(_items_at(plate_words, plate_coords), _fake_frame), "选项")
# 位置约束：同样的资源收益放在正文区(cy≈0.5h)，7.6b 不得命中。
# 只断言「不判选项」——这帧本身没有六项属性涨幅，落到「未知」是正常行为，
# 不编造一个不存在的「资源型出行结果页」去期待某个分类。
res_trip_words = "第3周 星期- 空之丘女高 精力+20 体力+20".split()
res_trip_coords = {"精力+20": (400, 500), "体力+20": (600, 500)}
check("资源收益在正文→不抢成选项", s._classify(_items_at(res_trip_words, res_trip_coords), _fake_frame) != "选项", True)
# 视觉兜底：结果类误判也能被成排黄卡片纠正回事件页
check("行程结果在黄卡片纠正白名单内", "行程结果" in s.EVENT_CORRECT_PAGES, True)

# 21:59 实跑（screenshots/auto/215917_unknown_1.png）：点完二选一黄卡片后的
# 「回应对话」页，只剩顶栏 + 对话框里一句台词，右下角是角色头像而不是白色▼，
# 8.1 箭头规则接不住 → 判未知页按兜底 pause 收工。8.2 应判剧情事件点对话框推进。
reply_words = "第7周 星期四 3921130 120 6 昆三 还是别让她勉强了，受伤的话可得不偿失啊。".split()
reply_coords = {"还是别让她勉强了，受伤的话可得不偿失啊。": (950, 548)}
_reply_items = _items_at(reply_words, reply_coords)
check("事件回应对话→剧情事件", s._classify(_reply_items, _fake_frame), "剧情事件")
check("事件回应特征命中", s._is_event_reply(_reply_items, _fake_frame), True)
# 反向：中部有多段文字的功能页（每日页/训练核心）不算回应对话
busy_words = "第7周 星期四 92/130 120 训练 休息 商店 投射 弹跳 体能 防守 运控 意识".split()
busy_coords = {t: (200 + i * 60, 500 + (i % 3) * 40) for i, t in enumerate(busy_words)}
check("多段正文不算事件回应", s._is_event_reply(_items_at(busy_words, busy_coords), _fake_frame), False)

# ---------------------------------------------------------------- 2. 训练核心（技能名干净，右侧≥4）
# 真实运行时 OCR 把技能胶囊读成干净的「投射」「弹跳」（日志 27855 行实证）
print("--- 2. 训练核心（右侧技能胶囊）---")
core = "训练核心 第7周 星期2 精力 97/130 筛选 OFF 投射 弹跳 防守 体能 运控 意识 编队 历史 玩家阵容 下一步 决定 失败率".split()
check("右侧6技能→训练核心", s._classify(items(core), None), "训练核心")

# ---------------------------------------------------------------- 3. 属性成长面板（日志里1104次"未知"的主要来源）
# 这些是 ok-script.log 里真实被判成「未知」的文字，应该判「训练结果」
print("--- 3. 属性成长面板（当前误判为未知）---")
ATTR_CASES = [
    "倍率加成 心·技·体 へ",                                    # 只有标题+噪声
    "倍率加成 属性加成 心·枝·材 可表情一致 表情升级 额外队友 B 436/1784 体能C284 弹跳A674 防守 B+570 运控B495 意识 C298",
    "BONUSX2.29 心·技·体 表情一致 投射A691 体能C344 弹跳A759 防守A625 运控 空A661 意识B413",
    "属性加成 心·枝·体 投射 A+831 体能 /1640 防守 625",
    "BONUS X2.1 属性加成 心· 可表情一致 额外友 投射 1020/1784 体能B432 弹跳A+969 防守 A792 运控A897 意识 B481",
    "属性加成 颜外队友 心·技·体 投射 1020/1784 体能 B432 弹跳 1060",
]
for text in ATTR_CASES:
    its = items(text.split())
    panel = is_attr_panel(" ".join(t["t"] for t in its))
    got = s._classify(its, None)
    check(f"属性面板[{text[:12]}...] is_attr_panel", panel, True)
    check(f"属性面板[{text[:12]}...] →训练结果", got, "训练结果")

# 14:59 实跑抓到的过渡帧：末字「体」被 OCR 吞，只剩「心·技」+ 六属性带数值。
# 修复前：XIN_JI_TI 要求「体」→ 不命中 → 判未知 → fallback=pause 提前收工（0/1）。
stub_case = "心·技 35 投射C367/1784 体能C284/1640 弹跳A679/+25 防守B+507/2040 运控B474/1745 意识C298/+12".split()
check("残缺[心·技] is_attr_panel", is_attr_panel(" ".join(stub_case)), True)
check("残缺[心·技] →训练结果(非未知)", s._classify(items(stub_case), None), "训练结果")

# ---------------------------------------------------------------- 4. 其他真实未知页
print("--- 4. 日志里其他真实未知页 ---")
OTHER = [
    ("本周总结", "本周总结 球队评分 6647 球员 位置 评分 投射 弹跳 运控 体能 防守 意识 真崎 控卫"),
    ("点击继续", "新目标 商店 6天 赢得与育英女子学院的比赛 休息 投射 B+ 582 体能 点击屏幕继续 弹跳 A 防守 A"),
]
for want, text in OTHER:
    got = s._classify(items(text.split()), None)
    check(f"{want}", got, want)

# ---------------------------------------------------------------- 5. 反向用例（防误伤）
print("--- 5. 反向用例：不互相误判 ---")
# 真·训练后的属性面板：有「心·技·体」+ 六属性，但没有「筛选」开关、没有「下一步」
pure_attr = "心·技·体 倍率加成 投射 A+831 弹跳 S 防守 B 运控 A 意识 C 体能 B".split()
check("纯属性面板→训练结果(非选人)", s._classify(items(pure_attr), None), "训练结果")
# 真·训练核心：右侧六技能 + 决定 + 失败率，无属性面板标题词
pure_core = "第7周 星期2 精力97/130 投射 弹跳 防守 体能 运控 意识 决定 失败率0%".split()
check("纯训练核心→训练核心(非结果)", s._classify(items(pure_core), None), "训练核心")
# 17:30 实跑：「附加赛目标」弹窗盖住每日页，顶栏被遮读不出，但底部训练/休息/商店还在
extra_banner = "附加赛目标 商店 休息 训练".split()
check("附加赛目标弹窗盖每日页→每日页", s._classify(items(extra_banner), None), "每日页")
# 反向：只有「附加赛目标」一个词、没有行动按钮 → 不认每日页（避免别处误伤）
check("附加赛目标无按钮→未知", s._classify(items(["附加赛目标"]), None), "未知")

# 22:08 实跑：步行街扭蛋三选一里有一张卡叫「精力补充」，正文全是「精力」，
# 旧兜底 `(购买|补充)+(体力|精力)` 把整个扭蛋选择页误判成体力不足弹窗 →
# 找不到「取消」就点左上角，连点 600+ 步空转（第7周星期六卡死）。
check("扭蛋选择页不算购买弹窗",
      s._is_buy_popup("扭蛋选择", items("请选择1个扭蛋 精力恢复++ 精力补充 意识增加+ 确定".split())), False)
check("商店页不算购买弹窗",
      s._is_buy_popup("商店", items("精力饮料 购买 30".split())), False)
# 2026-11-24 修：这两条夹具原来写的是 True —— 也就是把「词共现就弹窗」的 bug
# 当成了正确行为。15:29 实跑就是因为「购买 + 精力」判成体力不足弹窗 →
# 找不到取消 → 点左上角 → 连点 600+ 步空转。买药后的浮层根本不是弹窗。
check("买药后的购买并使用浮层→不是弹窗",
      s._is_buy_popup("未知", items("第5周 星期7 24/130 购买并 精力".split())), False)
check("15:29 连点600步那一次的页面→不是弹窗",
      s._is_buy_popup("每日页", items("第4周 星期5 5/253 购买 体力".split())), False)
# 真弹窗必须有取消类按钮，才允许按「绝不点购买」处理
check("带取消的真体力不足弹窗→弹窗",
      s._is_buy_popup("未知", items("体力不足 购买 取消".split())), True)
check("只有购买没有取消→交给推进器",
      s._is_buy_popup("未知", items("体力不足 购买 150".split())), False)
check("只有补充没有购买→不算弹窗",
      s._is_buy_popup("未知", items("精力补充 体力".split())), False)

# 2026-11-24 08:49 实跑第2周：训练结果「数字滚动」残缺帧，底栏只被 OCR 读到两项属性
# （「投射 B 475」「运控 S 1,194」），凑不够 7.4b 的 >=3 项 -> 判未知页 pause，整轮断在这。
anim = "35 投射 B 475 运控 1,194".split()
check("训练结果计数动画残缺帧→训练结果",
      s._classify(items(anim, right_skills=False), None), "训练结果")
# 反向：训练核心页底部也是同款属性条，但有「决定」按钮，不能被 7.4c 抢走
core_like = "第7周 星期2 精力97/130 投射 B 475 运控 1,194 弹跳 防守 体能 意识 决定".split()
check("带决定的训练核心→训练核心", s._classify(items(core_like), None), "训练核心")

print(f"\n=== 通过 {PASS} / 失败 {FAIL} ===")
sys.exit(1 if FAIL else 0)
