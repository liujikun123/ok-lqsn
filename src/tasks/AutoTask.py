"""自动训练：读屏 → 认页面 → 按规则选最优动作 → 点。

设计原则（来自实际踩坑）：
1. 每步都重新读屏判断，不写死点击序列 —— 训练过程有随机性，序列一定会错位。
2. 认不出页面就停（或按配置兜底），绝不盲点。
3. 每个决策都打日志，事后能复盘为什么选了它。

页面判定顺序很重要：越特殊、字数越独特的规则越靠前。
历史上翻车两次都是「通用兜底」抢在「具体页面」前面。
"""
import json
import os
import re
import time

import numpy as np

from ok import Icon
import cv2

from src.core.cards import card_glow, find_cards
from src.tasks.MyBaseTask import MyBaseTask
from ok.feature.Box import Box, find_box_by_name, find_boxes_by_name
from ok.util.color import find_color_rectangles

# 主按钮候选，按优先级排列（越靠前越优先）
CONFIRM = ["开始训练", "确认编队", "确定", "决定", "确认", "启程", "下一步", "继续", "开始", "进入"]
SKIP = ["跳过", "点击屏幕继续"]
STOP_WORDS = ["关闭", "取消", "返回"]

# 按钮配色（框架 ok.util.color.find_color_rectangles 用 BGR 三通道区间）
BTN_YELLOW = {"b": (0, 150), "g": (120, 255), "r": (170, 255)}
BTN_BLUE = {"b": (120, 255), "g": (55, 210), "r": (0, 145)}
# 通用推进器绝不点的「花钱」字样
DANGER_TEXT = ("购买", "充值", "支付", "￥", "¥", "钻石", "礼包", "首充", "月卡")

WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# ── 显式阶段机（2026-11-23 玩家要求：别再靠「见过什么页」隐式推断局内/局外）──
# 三个阶段表达「一轮训练」的生命周期，替代原来 saw_training + in_training 两个布尔：
#   HALL     = 在大厅，还没开训（或刚跑完一轮回到大厅）
#   TRAINING = 已进局，正在局内页面流转（每日页/训练/选人/事件/比赛…）
#   SETTLING = 已看到收尾页（训练结束/结果/评分），正往大厅走；回到大厅才算一轮完成
# 只有 TRAINING/SETTLING 阶段回到大厅才 +1 轮；停在 HALL 的公告/弹窗不算。
PHASE_HALL = "HALL"
PHASE_TRAINING = "TRAINING"
PHASE_SETTLING = "SETTLING"

# 页面 → 阶段。用于每步校正阶段，不依赖「见过非大厅页」这种宽泛判断。
# 大厅/未知/纯遮挡浮层不改阶段；其余页面按此表进入 TRAINING 或 SETTLING。
PAGE_PHASE = {
    # 明确的收尾页 → SETTLING
    "训练结束": PHASE_SETTLING, "训练评分": PHASE_SETTLING,
    "恭喜获得": PHASE_SETTLING, "比赛MVP": PHASE_SETTLING,
    "结算": PHASE_SETTLING, "目标达成": PHASE_SETTLING,
    "行程结果": PHASE_SETTLING, "点击继续": PHASE_SETTLING,
    # 局内主流程 → TRAINING
    "每日页": PHASE_TRAINING, "训练项页": PHASE_TRAINING,
    "选择队友": PHASE_TRAINING, "笔记选择": PHASE_TRAINING,
    "器材选择": PHASE_TRAINING, "确认编队": PHASE_TRAINING,
    "开始训练确认": PHASE_TRAINING, "训练结果": PHASE_TRAINING,
    "训练确认": PHASE_TRAINING, "行为卡": PHASE_TRAINING,
    "笔记学习": PHASE_TRAINING, "经理增益": PHASE_TRAINING,
    "训练提议": PHASE_TRAINING, "扭蛋选择": PHASE_TRAINING,
    "扭蛋机": PHASE_TRAINING, "出行": PHASE_TRAINING,
    "公园子页": PHASE_TRAINING, "赛前准备": PHASE_TRAINING,
    "选项": PHASE_TRAINING, "剧情事件": PHASE_TRAINING,
    "炒股小游戏": PHASE_TRAINING, "小游戏": PHASE_TRAINING,
    "商店": PHASE_TRAINING, "专注恢复": PHASE_TRAINING,
    "体力恢复": PHASE_TRAINING, "本周总结": PHASE_SETTLING,
}

# 页面级状态转移软约束（2026-11-23 玩家要求）——「上页→当前页」明显不可能时记警告+截图，
# 但**不改判页、不拦动作**，只收集数据。跑几轮看误报率再决定要不要收紧成硬约束。
# 只列「明确不可能」的转移，宁可漏报也别误报刷屏。设计依据玩家举的两个例子：
#   ① 还在选人环节 → 不可能出现每日页的「训练」入口（那是选完回大厅才有的）
#   ② 还没到周日 → 不可能出现「本周总结」这类周收尾页
PAGE_FORBIDDEN = {
    # 选人三连 + 编队确认：只能往训练深处走，不该退回每日页/大厅训练入口
    "选择队友":   ("每日页", "大厅", "商店"),
    "笔记选择":   ("每日页", "大厅", "商店"),
    "器材选择":   ("每日页", "大厅", "商店"),
    "确认编队":   ("每日页", "大厅", "商店"),
    "开始训练确认": ("每日页", "大厅", "商店"),
    "训练项页":   ("大厅", "商店"),   # 训练项页不该直接跳大厅/商店
    "行为卡":     ("每日页", "大厅", "商店"),
}

# 「心·技·体」属性面板 —— OCR 极不稳定：实测读出过「心·支·体」「心·枝·体」，
# 甚至整行只剩「属性加成」「颜外队友」。
# 2026-11-23 14:59 实跑又抓到一种：过渡帧只读到「心·技」（末字「体」被吞），
# 于是识别正则和指纹表都漏掉，判成真未知→按 fallback=pause 收工（那轮 0/1 提前停）。
# 末字「体」改成可选；但「心·技」这种残缺必须同时看到多个属性词才算属性面板，
# 否则单看两个字有极小概率误伤，故这里放宽、最终判页在 _classify 里还要数属性词。
XIN_JI_TI = re.compile(r"心\s*[·・.．]?\s*[技枝伎支]\s*[·・.．]?\s*体?")
ATTR_WORDS = ("投射", "弹跳", "运控", "体能", "防守", "意识")
# 2026-11-23 21:34 实跑：事件二选一的收益说明不只给六项属性，还会给资源
# 「精力+30」「体力+30」「金币+…」。7.6b 原来只认 ATTR_WORDS，于是
# 「精力+30 / 体能+30」这页只数到 1 条收益（≥2 才判选项），被 7.6 抢成
# 「行程结果」→ _do_next 找不到主按钮就点屏幕中间，空转 15 步收工。
# 单独扩一张收益词表，不要污染 ATTR_WORDS —— 那是六项属性，训练结果/
# 属性面板的「属性词计数」规则靠它，混进资源词会把正常页判成训练结果。
GAIN_WORDS = ATTR_WORDS + ("精力", "体力", "专注", "金钱", "金币", "资金")


def is_attr_panel(text):
    """是不是「心·技·体」属性加成面板（含 OCR 读花的变体）。"""
    if XIN_JI_TI.search(text):
        return True
    if any(w in text for w in ("属性加成", "额外队友", "顏外队友", "颜外队友", "倍率加成")):
        return True
    return False


def _game_visible():
    import win32gui
    h = win32gui.FindWindow(None, "篮球少女：飞跃巅峰")
    if not h:
        return False
    if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
        return False
    l, t, r, b = win32gui.GetWindowRect(h)
    return r - l > 200 and b - t > 200 and l > -10000


def _num(text):
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


class AutoTask(MyBaseTask):

    # 底部出现「成排黄卡片」时，允许把误判纠正成事件页的页面集合。
    # 抽成常量便于回归测试断言（21:34 二选一事件页被判「行程结果」卡死）。
    # 2026-11-24 09:01 实跑：把「训练结束」从这个白名单里拿掉 —— 收尾页底部本来就有
    # 一排球员缩略卡（黄底），被 _event_cards 当成事件卡 → 判成剧情事件 →
    # 点中「6181」那张球员卡，弹出「注册球员」属性面板（无前进按钮），
    # 之后连点 12 步不动，第 1 轮就断在这里（训练评分 8521 已经拿到）。
    EVENT_CORRECT_PAGES = ("未知", "每日页", "剧情事件", "选项", "行程结果")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "自动训练 / Auto"
        self.description = "自动跑一轮完整训练：训练 → 事件 → 经理增益 → 比赛 → 训练结束 → 回大厅"
        self.icon = Icon.SYNC
        self.default_config.update({
            "最大步数": 1500,
            "等待秒数": 1.3,
            "扭蛋目标关键词": "笔记,继承",   # 步行街扭蛋三选一里「笔记 / 继承」都算
            "扭蛋最高价": 120,
            "经理增益策略": "最高加成",
            # 训练项读数失败（点开训练项后「训练项详情」没弹出来 / 0 帧有效）怎么办。
            # 玩家 2026-11-26 原话：「为什么读数 0 帧这么多还继续？应该停下来」——
            # 蒙着按默认项继续 = 白扔一次训练，而且事后看不出来。默认：停。
            "读数失败即暂停": True,
            "商店自动购买": True,
            # ↑ 打开不等于乱买：只买 0 元红包 + 精力低于阈值时的精力药，
            #   具体见 _do_shop 与 strategy.yaml 的 tuning.shop。
            "未知页面兜底": "点屏幕中间",
            "排除地点": "游戏厅",
            "小游戏处理": "放弃",
            # 局内「精力」（顶栏 ⚡ a/130）：训练消耗它，低于这个值就不练了。
            # 只有两条恢复路径 —— 局内事件「精力+X」、周末出行「公园」(+30/+70)。
            # 药水对它无效，别搞混。
            "训练精力下限": 40,
            # 局外「专注」（弹窗 a/150，账号级体力）：只决定「能不能开一次训练」。
            # 不足时弹「专注恢复」，这里用药水补；**永远不点钻石**。
            "专注恢复": "用道具",        # 用道具 / 关闭
            "每轮最多补专注次数": 4,
            "跑完一轮就停": True,
            "保存截图": False,
        })
        # 运行期状态统一在这里初始化：CheckTask / StepTask 这类子类不走 run()，
        # 否则会 AttributeError（体检任务第一次跑就踩到了）。
        self.notes_done = False
        # 视觉层（2026-11-24）：页面指纹库懒加载；通用推进器失败计数；
        # 上一步页面（马尔可夫硬约束要用）
        self._anchor_cache = None
        self._anchor_hits = {}
        self._last_page_hint = None
        self._advance_fails = 0
        # 局内精力（每日页 a/b，上限约 130）：训练/休息决策的唯一依据
        self.stamina = None
        self.stamina_max = None
        # 局外「专注」（顶栏 a/b，上限 150）：只决定「够不够开一次训练」，
        # 不参与训练/休息决策。2026-11-23 玩家明确：这是局外账号资源。
        self.out_focus = None
        self.out_focus_max = None
        self.weekday = None
        self.item_checked = False
        self.read_fail_stop = False    # 训练项读数失败 → 主循环停机（玩家 2026-11-26）
        self._item_interrupt = None      # 读数被经理增益/剧情/遮罩打断时的页面名
        self.trip_picked = False
        # 「出行先探笔记，笔记 > 训练」（玩家 2026-11-23）：
        #   每日页同时有「训练」和「出行」时，先进出行页看地点标签里有没有笔记。
        #   有 → 直接出行；没有 → 退回每日页走训练。
        #   用「第N周-星期X」记今天已经探过，避免同一天反复进出。
        self.trip_probe = False          # 本次进出行页是「探笔记」，不是真要出行
        self.trip_probe_done = None      # **真的进过**出行页的那天，例如 "6-7"
        # 2026-11-24 第七周周六漏探笔记的根因：旧代码在**点击之前**就写 done，
        # 点「出行」被 FLUTTERVIEW 挡住时页面还在每日页，却被记成「今天探过了」，
        # 下一帧直接走训练分支。现在 done 只在真正进入出行页时才写，
        # 这里单独记「试了几次还没进去」，最多重试 3 次。
        self.trip_probe_retry = 0        # 当天点了出行但没进页面的次数
        self.trip_probe_retry_day = None # 上面那个计数属于哪一天
        self.trip_probe_disabled = False # 兼容旧状态；现在不再置 True（退回失败只放弃当天）
        self.trip_back_tries = 0         # 本次退出出行页试了几次
        # 探笔记的「两级右上角返回」还剩几层：2=扭蛋三选一页, 1=出行页, 0=无
        self.trip_probe_back = 0
        # 商店：以前 _do_shop 写好了却**从来没被调用过**（每日页没接入口），
        # 整轮跑下来一次店都没进、精力药也没买过（2026-11-23 查 run_1100/1105 日志确认）。
        # 现在每日页在「精力 < 阈值」时进店看一眼，按天记账，一天最多进一次。
        self.shop_day = None
        self.shop_week = None          # 已经例行看过货的那一周（商店 7 天刷新）
        self.unknown_count = 0
        self.known_overlay_hits = {}     # 已知浮层命中计数 {名称: 次数}，收工摘要用
        self.unknown_texts = []          # 真·未知页的文字（最多留 20 条），收工摘要/排复用
        self.minigame_tries = 0
        self.stock_idx = 0
        self.stock_tries = 0
        self.stock_picked = False
        self.slots_autofill = 0
        self.slots_empty = 0
        self.slots_checked_page = None   # 已经确认过「没有空槽」的那一页（只用来打一次日志）
        self.confirm_gray = 0            # 选人页「下一步」连续灰着几次了（配队没满就暂停）
        # 上一场训练在场上看到的器材数（商店买「器材暴击」要先确认器材上有人）
        self.last_equip_count = 0
        # 商店有没有精力道具卖：None=本期还没进过店 / True=有 / False=没有。
        # 「失败率全部>30 且无精力药 → 改练意识」这条规则要用它，没进店就不敢乱判。
        self.shop_has_stamina = None
        self.shop_bought = 0
        # 本轮主训人员（每局会换，由 _main_char_of_round 从画面上认，不写死名字）
        self.main_char_round = None
        # 已知主角名池：只用于剔除 OCR 粘连词（「百希雅」= 百目鬼 + 希雅），
        # 不作为「谁是主训」的判断依据。每轮认出新主训就补进来。
        self.known_main_chars = ["希雅", "闪堂"]
        self.route = {}
        self.in_training = False
        self.saw_training = False
        self.phase = PHASE_HALL
        self.choice_idx = 0
        self.stamina_refills = 0
        self.stamina_misses = 0

    # ---------- 基础 ----------

    def _force_visible(self):
        """把游戏窗口强制恢复并抢回前台。

        2026-11-23 06:27 踩坑：**Windows 输入体验（TextInputHost.exe）** 把前台抢走了，
        ok-script 的 `visible` 字段其实是 is_foreground()，于是它认定窗口不可见 →
        截图整条链路卡死（next_frame 不返回）。SetForegroundWindow 会被前台锁拒绝，
        必须用 AttachThreadInput 把输入队列粘上去才行；粘上还不行就把那个抢前台的
        TextInputHost 结束掉（系统会自动重启它，不影响使用）。
        """
        try:
            import win32gui
            import win32process
            import ctypes
            h = win32gui.FindWindow(None, "篮球少女：飞跃巅峰")
            if not h:
                return False
            u = ctypes.windll.user32
            u.ShowWindow(h, 9)                      # SW_RESTORE
            fg = win32gui.GetForegroundWindow()
            if fg == h:
                return True
            try:
                tid_fg = win32process.GetWindowThreadProcessId(fg)[0]
                tid_me = win32process.GetWindowThreadProcessId(h)[0]
                u.AttachThreadInput(tid_fg, tid_me, True)
                u.BringWindowToTop(h)
                u.SetForegroundWindow(h)
                u.AttachThreadInput(tid_fg, tid_me, False)
            except Exception:
                pass
            if win32gui.GetForegroundWindow() == h:
                self.log_info("已用 AttachThreadInput 抢回前台")
                return True
            # 最后手段：把抢前台的 TextInputHost 杀掉（它只是输入法候选窗宿主，会自动重启）
            try:
                _, pid = win32process.GetWindowThreadProcessId(fg)
                import win32api
                proc = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
                exe = win32process.GetModuleFileNameEx(proc, 0)
                if "TextInputHost.exe" in exe:
                    self.log_warning(f"前台被 {exe} 占用，结束它 pid={pid}")
                    import subprocess
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=5)
                    time.sleep(0.8)
                    u.SetForegroundWindow(h)
                    if win32gui.GetForegroundWindow() == h:
                        self.log_info("抢回前台成功")
                        return True
            except Exception as e:
                self.log_warning(f"清理抢前台的进程失败: {e}")
            self.log_warning(f"窗口仍未回到前台（当前前台：{win32gui.GetWindowText(win32gui.GetForegroundWindow())!r}）")
            return False
        except Exception as e:
            self.log_warning(f"强制恢复窗口失败: {e}")
            return False

    def _ensure_foreground(self):
        """点击前确认游戏在前台。

        为什么必须做（2026-11-23 查框架源码确认）：
          * 截图走 BitBlt_RenderFull(PrintWindow) → **后台也能抓**，被别的窗口挡住无所谓；
          * 点击走 Pynput 的真实鼠标事件，框架的 `clickable()` = 窗口 is_foreground()，
            **窗口不在前台时点击直接被丢弃**（只打一行 info，不报错）。
          表现就是「日志一直在走、游戏一动不动」，很像卡死。
        2026-11-23 17:00 根治焦点问题（玩家用向日葵远程看画面，焦点被切走）：
          原来这里 3 秒节流，节流窗口内焦点被抢走就完全不检查、不重抢，
          于是 Pynput 点击连丢十几步 → 页面卡住不动。现在拆成两段：
            · 廉价检查（每次都做，微秒级）：FindWindow + GetForegroundWindow，
              在前台就立刻返回，不做任何重量操作；
            · 重量抢回（仅不在前台时）：AttachThreadInput + SetForegroundWindow。
          主循环每步、每次 click/send_key 前都调它，保证点击那一刻游戏一定在前台。
        """
        try:
            import win32gui
            h = win32gui.FindWindow(None, "篮球少女：飞跃巅峰")
            if not h or win32gui.GetForegroundWindow() == h:
                return True
        except Exception:
            return False
        # 不在前台：日志做节流（避免向日葵反复切时刷屏），但抢前台动作每次都做
        now = time.time()
        if now - getattr(self, "_last_fg_log", 0.0) >= 3.0:
            self._last_fg_log = now
            try:
                fg_title = win32gui.GetWindowText(win32gui.GetForegroundWindow())
            except Exception:
                fg_title = "?"
            self.log_warning(f"游戏窗口不在前台（当前：{fg_title!r}）→ 点击会被丢弃，抢回中")
        # 抢前台本身也可能失败（AttachThreadInput 被拒等）；吞掉异常让点击继续走
        # ——就算这次点击被框架丢弃，也好过整个任务抛异常崩掉。
        try:
            return self._force_visible()
        except Exception as e:
            self.log_warning(f"抢回前台失败：{e}")
            return False

    def _start_watchdog(self):
        """看门狗：跑的过程中窗口被隐藏/最小化，next_frame() 会**永久阻塞**，
        主循环连一条日志都打不出来（2026-11-23 06:27 实测卡了 2 分钟）。
        单独起个线程盯「多久没走一步」，超时就强制把窗口拉回来。"""
        import threading
        self._last_step_at = time.time()
        self._watchdog_stop = False

        def loop():
            while not self._watchdog_stop:
                time.sleep(3.0)
                gap = time.time() - self._last_step_at
                if gap < 20:
                    continue
                self.log_warning(f"{gap:.0f}s 没有进展（游戏窗口可能被隐藏/最小化），强制恢复窗口")
                self._force_visible()

        threading.Thread(target=loop, daemon=True, name="bsg-watchdog").start()

    def _frame(self, timeout=5.0):
        """取一帧新画面。

        注意：nullable_frame() 只是返回缓存，不会触发采集 —— 用它会导致一直拿到同一帧。
        真正的取帧入口是 next_frame()，它在窗口不可见时会**一直阻塞**，所以先判窗口。

        踩坑（2026-11-23 06:30）：试过把 next_frame() 丢进子线程加超时，结果超时后
        那个线程还卡在采集里，继续持有取帧锁 → 后面每一次截图都超时，整个采集死掉。
        **不能靠线程超时兜底**。正确做法是保证窗口一直是可见/非最小化（看门狗负责）。
        """
        if not _game_visible():
            self._force_visible()
            return None
        try:
            frame = self.next_frame()
        except Exception as e:
            msg = str(e)
            if "pause" in msg.lower():
                self.log_warning(f"执行器处于暂停状态，恢复后重试：{msg}")
                try:
                    self.executor.paused = False
                    self.executor.reset_scene(check_enabled=False)
                except Exception:
                    pass
            else:
                self.log_warning(f"取帧失败：{msg}")
            return None
        # 全黑帧 = 显示器断电/场景切换/采集掉线，不能拿去判页面（会把黑屏当未知页乱点）
        if frame is not None and float(frame.mean()) < 8:
            self.log_warning(f"画面接近全黑（均值 {float(frame.mean()):.1f}），可能显示器断电或切换场景")
            return None
        return frame

    def _foreground(self):
        """把游戏窗口拉回前台；点击用真实鼠标事件，不在前台会点到别处。"""
        try:
            import win32con
            import win32gui
            h = win32gui.FindWindow(None, "篮球少女：飞跃巅峰")
            if h and win32gui.GetForegroundWindow() != h:
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(h)
                self.log_info("已把游戏窗口拉回前台")
        except Exception as e:
            self.log_warning(f"前置窗口失败: {e}")

    SHOT_TAGS = {"选择队友": "pick", "笔记选择": "note", "器材选择": "equip",
                 "训练项页": "core", "每日页": "day", "商店": "shop"}

    def _shot(self, frame, tag):
        try:
            import cv2
            os.makedirs("screenshots/auto", exist_ok=True)
            tag = str(tag)
            for k, v in self.SHOT_TAGS.items():
                tag = tag.replace(k, v)
            # cv2.imwrite 不吃非 ASCII 文件名（实测写出来是 "鍣ㄦ潗閫夋嫨" 这种乱码）
            tag = "".join(c if (c.isascii() and (c.isalnum() or c in "-_")) else "_"
                          for c in tag)
            p = f"screenshots/auto/{time.strftime('%H%M%S')}_{tag}.png"
            cv2.imwrite(p, frame)
            return p
        except Exception:
            return None

    def _audit_path(self, week, day, tag):
        """读数证据目录：logs/audit/<run_id>/W<周>D<星期>/<时刻>_<tag>.png。

        玩家 2026-11-23 16:47 要求「重跑一周，把每天读数的截图和判定结果都保留，
        人工检查」。所以每次训练项页逐项读数都落一份：截图 + 那帧的 OCR 原文 +
        识别出的队友/器材/失败率/等级 + 打分，按「周 / 星期」分目录，绝不覆盖。
        """
        try:
            import cv2
            run = getattr(self, "run_id", None) or "norun"
            w = week if week is not None else 0
            d = day if day is not None else 0
            d = os.path.join("logs", "audit", str(run), f"W{w}D{d}")
            os.makedirs(d, exist_ok=True)
            # 训练项名是中文，cv2/文件名不吃非 ASCII → 先转英文缩写，
            # 否则 core1_投射 变成 core1___，人工分不清是哪个训练项。
            zh2en = {"投射": "Shoot", "弹跳": "Jump", "防守": "Def",
                     "体能": "Phys", "运控": "Dribble", "意识": "IQ"}
            tag = str(tag)
            for zh, en in zh2en.items():
                tag = tag.replace(zh, en)
            tag = "".join(c if (c.isascii() and (c.isalnum() or c in "-_")) else "_"
                          for c in tag)
            return os.path.join(d, f"{time.strftime('%H%M%S')}_{tag}")
        except Exception:
            return None

    def _audit_save(self, week, day, tag, frame, text):
        """存一帧读数证据：同名 .png + .txt（OCR 原文 + 判定）。失败不影响主流程。"""
        audit_cfg = self._tuning().get("audit") or {}
        if audit_cfg.get("enabled") is False:
            return None
        base = self._audit_path(week, day, tag)
        if not base:
            return None
        try:
            import cv2
            if frame is not None:
                cv2.imwrite(base + ".png", frame)
            with open(base + ".txt", "w", encoding="utf-8") as f:
                f.write(text if isinstance(text, str) else str(text))
            return base
        except Exception:
            return None

    def _frame_retry(self, tries=3, wait=0.6):
        """取帧并重试。切场景/白闪的那一两帧会取不到，连拍时不能因为一次失败就少一帧。"""
        for _ in range(tries):
            f = self._frame()
            if f is not None:
                return f
            self.sleep(wait)
        return None

    def _texts(self, frame):
        """框架 OCR 结果 → 轻量文本视图。

        框架 `self.ocr()` 返回的就是 `Box` 列表（name = 识别出的文字），这里只做一层
        「下标 + 中心点」投影，让 100 多处 `it["t"] / it["cx"]` 不用同时改。
        **匹配与点击已经全部走框架**（find_box_by_name / find_boxes_by_name / click_box），
        这一层不再包含任何自己的算法 —— 2026-11-24 去重。
        """
        boxes = self.ocr(frame=frame) or []
        return [{"b": b, "t": (b.name or "").strip(),
                 "cx": (b.x + b.width / 2), "cy": (b.y + b.height / 2)} for b in boxes]

    def _find(self, items, words, exact=False):
        """按词找第一个命中的文字框（框架 find_box_by_name）。

        exact=True  → 整串相等；
        exact=False → 子串（转成正则交给框架，不再自己遍历）。
        与老实现的一个差别：**词的先后顺序现在有优先级**（框架语义）——
        `["取消", "关闭"]` 会优先返回「取消」，而不是屏幕上先出现的那个。
        """
        if not items:
            return None
        if isinstance(words, str):
            words = [words]
        boxes = [it["b"] for it in items if it.get("b") is not None]
        if not boxes:      # 测试里手工构造的裸 dict（只有 t/cx/cy）
            for w in words:
                for it in items:
                    if (it["t"] == w) if exact else (w in it["t"]):
                        return it
            return None
        pats = list(words) if exact else [re.compile(re.escape(w)) for w in words]
        hit = find_box_by_name(boxes, pats)
        if hit is None:
            return None
        return next((it for it in items if it.get("b") is hit), None)

    def _all(self, items, pat):
        """按正则找全部命中的文字框（框架 find_boxes_by_name）。"""
        if not items:
            return []
        boxes = [it["b"] for it in items if it.get("b") is not None]
        if not boxes:
            rx = re.compile(pat)
            return [it for it in items if rx.search(it["t"])]
        hits = set(id(b) for b in find_boxes_by_name(boxes, [re.compile(pat)]))
        return [it for it in items if id(it.get("b")) in hits]

    def _yellow_btn_center(self, frame, it):
        """在文字框周围找**真实的黄色按钮矩形**，返回其中心（找不到返回 None）。

        2026-11-23 实测（行为卡页「请选择1个技能」）：OCR 给「确定」的框中心是 (1567,978)，
        而按钮实体在 y 980~1052、中心 (1583,1016) —— 照着 OCR 坐标点会落在按钮上方空白处。
        这一页连点 15 次都没反应，最后靠「连续 15 步无变化」停机。
        所以凡是"黄底大字"的主按钮（确定/决定/开始训练…），一律以黄色矩形为准。

        2026-11-24 去重：原来自己写 HSV 掩膜 + 闭运算 + 轮廓筛选，
        改成框架 find_color_rectangles（同样的活，带填充率阈值），少一份重复实现。
        """
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1 = max(int(it["cx"] - 0.30 * w), 0)
        x2 = min(int(it["cx"] + 0.30 * w), w)
        y1 = max(int(it["cy"] - 0.06 * h), 0)
        y2 = min(int(it["cy"] + 0.14 * h), h)
        if x2 - x1 < 20 or y2 - y1 < 16:
            return None
        try:
            rects = find_color_rectangles(
                frame, BTN_YELLOW,
                min_width=int(0.08 * w), min_height=int(0.025 * h),
                threshold=0.50, box=Box(x1, y1, x2 - x1, y2 - y1))
        except Exception:
            rects = []
        if not rects:
            return None
        r = max(rects, key=lambda b: b.width * b.height)
        return r.x + r.width / 2.0, r.y + r.height / 2.0

    # ---------- 焦点根治：所有点击/按键前强制确认游戏前台 ----------

    def click(self, *args, **kwargs):
        """覆盖框架 click：点击那一刻必须保证游戏在前台。

        框架 PynputInteraction.click() 开头就 `if not clickable(): return`——
        窗口不在前台时点击被静默丢弃（只打日志、不报错），表现就是「点了一直没反应」。
        click_relative / click_box 内部最终都调到 self.click，覆盖这里一处即可全覆盖。
        向日葵远程、切窗口看资料、输入法候选框都会抢焦点，所以必须每次点击前确认。
        """
        self._ensure_foreground()
        x = args[0] if len(args) > 0 else kwargs.get("x")
        y = args[1] if len(args) > 1 else kwargs.get("y")
        if (isinstance(x, (int, float)) and isinstance(y, (int, float))
                and not isinstance(x, bool) and self._blocked_at(x, y)):
            # 2026-11-24 10:00 实跑踩坑：日志一直点右上角 ↩，画面 15 步零变化。
            # 真相不是「弹窗退不出去」，而是 WorkBuddy 自己的覆盖窗口压住了游戏右上角，
            # 鼠标事件被它截走（WindowFromPoint 打出来是 FLUTTERVIEW）。
            # 判断「窗口在前台」骗过了所有人：前台确实是游戏，但坐标处最顶层的不是它。
            self._click_topmost(x, y)
            return
        return super().click(*args, **kwargs)

    def _game_hwnd(self):
        try:
            import win32gui
            return win32gui.FindWindow(None, "篮球少女：飞跃巅峰") or 0
        except Exception:
            return 0

    def _blocked_at(self, cx, cy):
        """目标点最顶层的窗口是不是游戏自己。False=没被挡，True=被别人盖住了。"""
        h = self._game_hwnd()
        if not h:
            return False
        try:
            import win32gui
            sx, sy = win32gui.ClientToScreen(h, (int(cx), int(cy)))
            top = win32gui.WindowFromPoint((sx, sy))
            if not top:
                return False
            root = top
            while True:
                parent = win32gui.GetParent(root)
                if not parent:
                    break
                root = parent
            return root != h
        except Exception:
            return False

    def _click_topmost(self, cx, cy):
        """被别的窗口盖住时的点击：临时把游戏置顶 → 原生鼠标事件 → 恢复。

        只在这一种情况下动手（正常路径不走这里），避免每次点击都折腾窗口层级。
        """
        h = self._game_hwnd()
        now = time.time()
        if now - getattr(self, "_blocked_log_ts", 0.0) >= 3.0:
            self._blocked_log_ts = now
            try:
                import win32gui
                sx, sy = win32gui.ClientToScreen(h, (int(cx), int(cy)))
                cover = win32gui.GetWindowText(win32gui.WindowFromPoint((sx, sy)))
            except Exception:
                cover = "?"
            self.log_warning(f"目标点 ({int(cx)},{int(cy)}) 被「{cover}」挡住 → 临时置顶游戏再点")
        try:
            import win32api
            import win32con
            import win32gui
            win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002)
            time.sleep(0.12)
            sx, sy = win32gui.ClientToScreen(h, (int(cx), int(cy)))
            win32api.SetCursorPos((sx, sy))
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.12)
            win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002)
        except Exception as e:
            self.log_warning(f"置顶点击失败，退回普通点击：{e}")
            try:
                super().click(cx, cy)
            except Exception:
                pass

    def send_key(self, *args, **kwargs):
        """键盘同理：PynputInteraction.send_key() 也检查 clickable()。"""
        self._ensure_foreground()
        return super().send_key(*args, **kwargs)

    def _click_item(self, it, why=""):
        """点一个文字框：坐标计算交给框架 click_box（它内部会调我们的 click 覆盖层）。"""
        self.log_info(f"点击「{it['t']}」@({int(it['cx'])},{int(it['cy'])}) {why}")
        b = it.get("b")
        if b is not None:
            # after_sleep=0：节奏仍由下面的 sleep 显式控制（和改造前一致）
            self.click_box(b, relative_x=0.5, relative_y=0.5, after_sleep=0)
        else:
            self.click(it["cx"], it["cy"])

    def _btn_center(self, frame, it, reach=0.28):
        """在文字附近找**按钮实体矩形**，返回它的中心；找不到返回 None。

        为什么要这一步（2026-11-23 两次踩坑）：
          · 行为卡「确定」OCR 框中心比按钮实体高 38px，连点 15 次全点在按钮上方空白；
          · 赛前准备「前往比赛」OCR 给 (1706,929)，按钮实体中心 (1653,978)，
            偏 53/49px，同样点空，15 步后停机。
          OCR 框只框住**字**，字在按钮里通常偏上（有的还偏右），所以主按钮一律按
          「色块矩形」定位，而不是按文字框中心。

        认三类按钮底色：金黄 / 浅灰（不可点的主按钮）/ 蓝（次要按钮）。
        """
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1 = max(int(it["cx"] - reach * w), 0)
        x2 = min(int(it["cx"] + reach * w), w)
        y1 = max(int(it["cy"] - 0.10 * h), 0)
        y2 = min(int(it["cy"] + 0.10 * h), h)
        patch = frame[y1:y2, x1:x2]
        if patch.size == 0:
            return None
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        # 按「可信度」分三档逐档尝试：金黄最可信（就是主按钮），其次蓝色，最后浅灰。
        tiers = [
            ((H >= 16) & (H <= 42) & (S >= 110) & (V >= 160)),      # 金黄主按钮
            ((H >= 85) & (H <= 115) & (S >= 70) & (V >= 110)),      # 蓝
            ((S <= 45) & (V >= 165)),                               # 浅灰按钮
        ]
        for mask in tiers:
            m = mask.astype(np.uint8) * 255
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))
            cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best = None
            for c in cnts:
                bx, by, bw, bh = cv2.boundingRect(c)
                if bw < 0.05 * w or bh < 0.022 * h or bw > 0.40 * w:
                    continue
                if not (2.2 <= bw / float(bh) <= 16):
                    continue
                # 必须是「一整块」的实心矩形：按钮底色是纯色，背景/渐变会被这条挡掉
                if cv2.contourArea(c) / float(bw * bh) < 0.60:
                    continue
                cx_, cy_ = bx + x1 + bw / 2.0, by + y1 + bh / 2.0
                dist = abs(cx_ - it["cx"]) + abs(cy_ - it["cy"])
                if dist > 0.14 * w:
                    continue
                key = (bw * bh) - dist * 2.0
                if best is None or key > best[0]:
                    best = (key, cx_, cy_)
            if best:
                return (best[1], best[2])
        return None

    def _click_btn(self, it, why="", frame=None):
        """点主按钮：先按色块矩形定位，定位不到才退回文字框中心。"""
        spot = self._btn_center(frame, it)
        if spot:
            off = abs(spot[0] - it["cx"]) + abs(spot[1] - it["cy"])
            if off > 6:
                self.log_info(f"点击「{it['t']}」@({int(spot[0])},{int(spot[1])}) {why} "
                              f"[按按钮矩形定位，文字框偏了 {int(off)}px]")
                self.click(spot[0], spot[1])
                return
        self._click_item(it, why)

    def _yellow_ratio(self, frame, box, pad=28):
        """框外扩一圈后统计黄色像素占比 —— 按钮是黄底，但文字框本身是黑的，
        直接取平均色会判成「不是黄按钮」，所以看占比更准。"""
        h, w = frame.shape[:2]
        x1 = max(int(box.x) - pad, 0)
        y1 = max(int(box.y) - pad, 0)
        x2 = min(int(box.x + box.width) + pad, w)
        y2 = min(int(box.y + box.height) + pad, h)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        patch = frame[y1:y2, x1:x2]
        b = patch[:, :, 0].astype(int)
        g = patch[:, :, 1].astype(int)
        r = patch[:, :, 2].astype(int)
        mask = (r > 185) & (g > 125) & (b < 150) & ((r - b) > 55)
        return float(mask.mean())

    def _is_yellow_btn(self, frame, it):
        return (self.is_yellow(self.mean_bgr(it["b"], frame))
                or self._yellow_ratio(frame, it["b"]) > 0.45)

    def _click_confirm(self, items, frame):
        """点当前页的主按钮；只有黄色才点，灰的不动。"""
        for w in CONFIRM:
            it = self._find(items, [w], exact=True)
            if not it:
                continue
            bgr = self.mean_bgr(it["b"], frame)
            ratio = self._yellow_ratio(frame, it["b"])
            if self.is_yellow(bgr) or ratio > 0.45:
                self._click_item(it, f"[主按钮 {w} 黄色 占比{ratio:.2f}]")
                return True
            self.log_info(f"「{w}」不是黄色 BGR={bgr}，跳过")
        return False

    # ---------- 空槽位（未选满）检查 ----------

    def _find_empty_slots(self, frame, cfg=None, debug=False):
        """找「还没选东西」的槽位，返回相对坐标列表。

        长这样：**金色粗边框 + 近白底色 + 中间一个黄色十字**（器材页第 5 格实测），
        跟已选中的卡片（整块金黄、饱和度很高）区分明显。

        只统计有金色描边的方形、内部足够白、且内部还留着黄色十字的框，
        三个条件同时成立才认，避免把金色徽章/图标误判成空槽。
        """
        cfg = cfg or (self._tuning().get("pick_slots") or {})
        h, w = frame.shape[:2]
        box = cfg.get("region") or [0.02, 0.42, 0.68, 0.78]
        x0, y0, x1, y1 = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, w), min(y1, h)
        if x1 - x0 < 40 or y1 - y0 < 40:
            return []
        roi = frame[y0:y1, x0:x1]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        gold = cv2.inRange(hsv, (15, 80, 120), (40, 255, 255))
        gold = cv2.morphologyEx(gold, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
        cnts, _ = cv2.findContours(gold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_h = float(cfg.get("min_height") or 0.09) * h
        max_h = float(cfg.get("max_height") or 0.24) * h
        min_white = float(cfg.get("min_white") or 0.55)

        out = []
        rejects = []
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            if not (min_h <= bh <= max_h):
                rejects.append(f"{bw}x{bh} 高度不在 {min_h:.0f}~{max_h:.0f}")
                continue
            if not (0.72 < bw / bh < 1.35):
                rejects.append(f"{bw}x{bh} 不是方的({bw / max(bh, 1):.2f})")
                continue
            if cv2.contourArea(c) / float(bw * bh) < 0.35:
                rejects.append(f"{bw}x{bh} 空心(填充率低)")
                continue
            # 内部中间 46%：必须是近白
            ix, iy = x + int(bw * 0.27), y + int(bh * 0.27)
            ip = hsv[iy:iy + int(bh * 0.46), ix:ix + int(bw * 0.46)]
            if ip.size == 0:
                continue
            white = float(((ip[:, :, 1] < 45) & (ip[:, :, 2] > 215)).mean())
            if white < min_white:
                rejects.append(f"{bw}x{bh} 不够白 {white:.2f}<{min_white}")
                continue
            # 内部还得留着黄色十字
            cx, cy = x + int(bw * 0.35), y + int(bh * 0.35)
            cp = hsv[cy:cy + int(bh * 0.30), cx:cx + int(bw * 0.30)]
            if cp.size == 0:
                continue
            cross = float((cv2.inRange(cp, (15, 80, 120), (40, 255, 255)) > 0).mean())
            if cross < 0.08:
                rejects.append(f"{bw}x{bh} 白但没黄十字 {cross:.2f}")
                continue
            out.append(dict(cx=(x + x0 + bw / 2.0) / w, cy=(y + y0 + bh / 2.0) / h,
                            w=bw / float(w), h=bh / float(h),
                            white=round(white, 2), cross=round(cross, 2)))
        if debug:
            self.log_info(f"加号检测：金框候选 {len(cnts)} 个 → 命中 {len(out)} 个"
                          f"｜ROI({x0},{y0})-({x1},{y1}) 高度门槛 {min_h:.0f}~{max_h:.0f}px"
                          f"｜被拒：{rejects[:8]}")
        out.sort(key=lambda d: d["cx"])
        return out

    def _slots_ready(self, page, items, frame):
        """开训前选人页的「选满了没」闸门。

        有空槽（金色框 + 白底 + 黄加号）= 还有东西没选。

        玩家 2026-11-23 12:47 明确：
          「如果有空的，你不要自己点自动选择，而且停止并且弹出提示」
        所以现在**绝不代为选择**：发现空槽 → 截图 → 桌面置顶弹窗 → 暂停，
        等玩家自己把编队配好再继续。想恢复「先点一次自动选择」，
        把 config/strategy.yaml 里 pick_slots.auto_fill_first 改成 true。
        返回 "ok" / "wait"（刚点完自动选择，下一帧再看）/ "pause"（停下等人工）。
        """
        cfg = self._tuning().get("pick_slots") or {}
        if cfg.get("check") is False:
            return "ok"
        pages = cfg.get("pages") or ["选择队友", "笔记选择", "器材选择"]
        if page not in pages:
            return "ok"

        first_time = self.slots_checked_page != page
        slots = self._find_empty_slots(frame, cfg, debug=first_time)
        if first_time and cfg.get("snapshot", True):
            # 每页第一次进来留一张原图，方便事后离线校准「加号检测」
            # （以前只有检测到空槽才存图，结果没报空槽时根本分不清是没空槽还是没检出）
            self._shot(frame, f"slots_{page}")
        self.slots_empty = len(slots)
        if not slots:
            # 每一页第一次通过时打一条，用来确认「加号检测」真的跑了
            # （以前只有空槽才打日志，没日志时根本分不清是「没空槽」还是「检测没跑」）
            if self.slots_checked_page != page:
                self.slots_checked_page = page
                self.log_info(f"{page}：已检查加号槽位，没有空位")
            return "ok"

        pos = ", ".join(f"({s['cx']:.2f},{s['cy']:.2f})" for s in slots)
        self.log_warning(f"{page}：发现 {len(slots)} 个空槽（还没选）@ {pos}")
        self._shot(frame, f"empty_slots_{page}")

        # 自动补空槽：两个开关，默认都不开（玩家的原话是「有空槽就停下弹窗，不许代选」）。
        #   auto_fill_first  = 三页通用（老开关）
        #   auto_fill_equip  = 只对「器材选择」页（2026-11-24 新增）
        #     器材是训练项页中场那 1~3 张支援卡，空一个槽 = 白扔一份加成（彩光 1.5 / 金光 1.0），
        #     也是评分卡在 8000+ 上不去 9000 的原因之一。要冲分就把这个开关打开。
        auto_ok = bool(cfg.get("auto_fill_first", False)) or (
            page == "器材选择" and bool(cfg.get("auto_fill_equip", False)))
        if auto_ok and self.slots_autofill < int(cfg.get("max_auto_fill") or 1):
            auto = self._find(items, ["自动选择", "自动填充"], exact=True)
            if auto:
                self.slots_autofill += 1
                self.log_info(f"{page}：还有空槽，先点「自动选择」补满（第 {self.slots_autofill} 次）")
                self._click_item(auto, "[自动选择-补空槽]")
                self.sleep(1.6)
                return "wait"  # 下一帧重新判断

        if cfg.get("on_empty", "暂停提示") == "暂停提示":
            self._shot(frame, f"pause_empty_{page}")
            hint = ("\n\n（想让脚本自己补满器材槽：strategy.yaml → pick_slots.auto_fill_equip: true）"
                    if page == "器材选择" else "")
            self._alert_popup(
                "篮球少女：编队有空位，已暂停",
                f"「{page}」还有 {len(slots)} 个空位（带黄色「+」的格子）没选。\n\n"
                f"脚本不会替你自动选择，已停在这里等你配好队伍。\n"
                f"配好后重新运行即可继续。\n\n"
                f"截图：screenshots/auto/pause_empty_{page}_*.png" + hint)
            self.log_error(f"{page} 还有 {len(slots)} 个空槽没选 —— 按你的要求不自动补，"
                           f"已暂停并在桌面弹出提示", notify=True)
            return "pause"
        self.log_warning(f"{page} 有空槽，但配置是继续，按原规则点下一步")
        return "ok"

    def _alert_popup(self, title, msg):
        """弹一个置顶的 Windows 提示框（后台线程，不阻塞脚本）。

        玩家要求「停止并且弹出提示」—— 光写日志看不见，脚本停了他也不知道为什么。
        用 daemon 线程弹 MessageBox：主线程照常走暂停逻辑，弹窗一直挂在那儿等人点。
        想关掉桌面弹窗，配置 alerts.popup: false。
        """
        cfg = self._tuning().get("alerts") or {}
        if cfg.get("popup", True) is False:
            return
        msg = str(msg)
        title = str(title)
        try:
            import ctypes
            import threading

            def _run():
                try:
                    MB_OK = 0x0
                    MB_ICONWARNING = 0x30
                    MB_SETFOREGROUND = 0x10000
                    MB_TOPMOST = 0x40000
                    ctypes.windll.user32.MessageBoxW(
                        0, msg, title,
                        MB_OK | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST)
                except Exception:
                    pass

            threading.Thread(target=_run, daemon=True).start()
            self.log_info(f"已弹出桌面提示窗：{title}")
        except Exception as e:
            self.log_warning(f"桌面弹窗失败（{e}），请看日志")

    # ---------- 页面识别 ----------

    def _pills(self, items, frame):
        """屏幕中间竖排的胶囊选项（集训事件送礼物这类）。

        实测（22:19）：希雅集训事件给三选一 --「DJKING音乐节票 / 清水家玻璃旋转餐厅 /
        就要小香肠」，三个白底圆角胶囊居中竖排，右上角「跳过」在这页点了没反应，
        必须选中一项才能继续。

        注意：这个判定很宽松、容易误伤，所以只在「剧情页连续点跳过 3 次没变化」时才用。
        """
        h, w = frame.shape[:2]
        if not any("跳过" in it["t"] for it in items):
            return []
        if any(it["t"] in ("训练", "休息", "商店", "比赛", "出行") for it in items):
            return []
        out = []
        for it in items:
            if not (0.10 < it["cy"] / h < 0.88):
                continue
            if abs(it["cx"] - w / 2) > w * 0.10:
                continue
            if not (2 <= len(it["t"]) <= 16):
                continue
            if re.match(r"^[\d/\.\+%★]+$", it["t"]):
                continue
            # 2026-11-23 08:18：背景装饰字（画面上的「See you」水印）也居中，
            # 混进来当第 4 个选项，宽度比 372/108=3.44 直接把整组选项判废，
            # 于是三选一页永远点「跳过」、连续 15 步不动 —— 只留含中文的。
            if re.match(r"^[\x00-\x7F]+$", it["t"].strip()):
                continue
            out.append(it)
        if not (2 <= len(out) <= 4):
            return []
        widths = [x["b"].width for x in out]
        # 2026-11-23：宽度差原来卡在 2.0，把「模拟软件而已…(764px) / 不许她们乱来(320px)」
        # 这种一长一短的剧情二选一全排除了 —— 连续 15 步点「跳过」无效，整轮退出。
        # 剧情选项本来就是长短句，胶囊选项（集训事件）才宽度接近，所以放宽到 3.0。
        if max(widths) > min(widths) * 3.0:
            return []
        out.sort(key=lambda x: x["cy"])
        return out

    def _right_card_count(self, frame):
        """右侧 2/3 区域里「带黄色等级牌的成员卡」数量 —— 纯图像，不看 OCR。

        选人页（选择队友 / 额外队友）特征是右侧竖排 4 张卡，每张卡顶部有
        一小块黄色「LV110」等级牌；训练项页右侧是训练项条目（不是卡 + 等级牌），
        训练结果页右侧基本空白。用这个把「选人页」和「属性面板/结果页」分开，
        补 OCR 只靠「额外队友」四字容易误判的短板（2026-11-23 14:20 实测）。

        返回 ≥2 说明像选人页。
        """
        if frame is None:
            return 0
        h, w = frame.shape[:2]
        x1, x2 = int(w * 0.70), int(w * 0.99)
        y1, y2 = int(h * 0.08), int(h * 0.70)
        sub = frame[y1:y2, x1:x2]
        if sub.size == 0:
            return 0
        try:
            import cv2
            hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, np.array([18, 110, 170]), np.array([40, 255, 255]))
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 7), np.uint8))
            cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            # 等级牌：小而扁的黄块（宽 30~180、高 14~60），按 y 聚成不同行
            ys = []
            for c in cnts:
                bx, by, bw, bh = cv2.boundingRect(c)
                if not (20 <= bw <= 200 and 10 <= bh <= 70):
                    continue
                if cv2.contourArea(c) < bw * bh * 0.45:
                    continue
                ys.append(by)
            # 不同 y（相隔 > 40px）算不同卡
            ys.sort()
            rows = 0
            last = -999
            for y in ys:
                if y - last > 40:
                    rows += 1
                    last = y
            return rows
        except Exception:
            return 0

    def _has_dialog_arrow(self, frame):
        """右下角白色倒三角▼ —— 剧情对话「点一下继续」的标记。

        实测 2026-11-23 15:23：对话页只有一句台词「喂！不要一言不发就跑掉啊！」，
        没有「跳过」按钮，OCR 读不到任何页面关键词 → 判未知停机；其实底部有
        白色小三角（1920x1080 下约 29x26px，在右下角 x>0.9w,y>0.88h），点一下就过。
        """
        if frame is None:
            return False
        h, w = frame.shape[:2]
        sub = frame[int(h * 0.88):h, int(w * 0.90):w]
        if sub.size == 0:
            return False
        try:
            gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
            m = cv2.inRange(gray, 232, 255)
            n, lab, stats, cen = cv2.connectedComponentsWithStats(m)
            for i in range(1, n):
                x, y, bw, bh, area = stats[i]
                # 白色小三角：宽高 15~45px、面积够、位置靠右下角
                if 15 <= bw <= 45 and 15 <= bh <= 45 and area >= 100:
                    return True
            return False
        except Exception:
            return False

    def _is_buy_popup(self, page, items):
        """局外体力不足 / 购买提示浮层（绝不点「购买」，直接取消）。

        2026-11-23 22:08 实跑踩坑：步行街扭蛋三选一里有张卡就叫「精力补充」，
        正文又全是「精力」，旧条件 `(购买|补充)+(体力|精力)` 把整个扭蛋选择页
        误判成弹窗 → 找不到「取消」就点左上角，连点 600+ 步空转。
        历史上这兜底只在「未知 / 每日页」的购买浮层上生效（12:08、15:29
        实跑，文字 6~9 段），所以：排除有独立处理分支的功能页，且必须真的
        出现「购买」二字，「补充」不再单独触发。
        """
        if page in ("专注恢复", "体力恢复", "扭蛋选择", "扭蛋机", "商店"):
            return False
        texts = [it["t"].strip() for it in items]
        joined = " ".join(texts)
        if "购买" not in joined or not ("体力" in joined or "精力" in joined):
            return False
        # 2026-11-24 修死循环：原来只看「购买 + 体力/精力」词共现。
        # 商店买精力药水后弹的「购买并使用 精力药水」浮层（8~9 段字、没有取消键）
        # 被判成体力不足弹窗 → 找不到「取消」就点左上角 → 连点 600+ 步空转。
        # 真弹窗一定带「取消/暂不/以后再说/关闭/放弃」之一；没有就不是弹窗，
        # 交给浮层表或通用推进器处理。
        if any(("购买并" in t) or ("并使用" in t) for t in texts):
            return False
        return any(t in ("取消", "暂不", "以后再说", "关闭", "放弃", "算了")
                   or ("取消" in t and len(t) <= 4) for t in texts)

    def _is_event_reply(self, items, frame):
        """8.2 事件二选一之后的「回应对话」页（2026-11-23 21:59 实跑）。

        点完黄卡片后画面只剩顶栏 + 对话框里一句台词，右下角是角色头像而不是
        白色▼，8.1 的箭头规则接不住 → 判未知页、按兜底 pause 直接收工。
        特征：第N周 + 中部一句 ≥10 字中文长句 + 无行动按钮/跳过/继续。
        要求长句，避免把「商店购买浮层」那种顶栏 + 几个短词的页面抢走。
        """
        if frame is None:
            return False
        joined = " ".join(it["t"] for it in items)
        if not re.search(r"第\s*\d+\s*周", joined):
            return False
        if self._action_btn(items, frame):
            return False
        if any(w in joined for w in ("跳过", "点击屏幕继续", "下一步", "确定")):
            return False
        h = frame.shape[0]
        body = [it for it in items if 0.20 < it["cy"] / h < 0.80]
        # 剧情对话页正文只有一两句台词；真每日页/训练项页中部还有一堆按钮、
        # 属性条、训练项标签，段数远多于此。离线扫 222 张验证过这个区分。
        if not (1 <= len(body) <= 2):
            return False
        return any(len(re.sub(r"[^\u4e00-\u9fff]", "", it["t"])) >= 10
                   for it in body)

    # ---------------- 页面判定：OCR 关键词 + 框架锚点 融合 ----------------

    def _log_quiet(self, msg, warn=False):
        """日志失败不影响流程（离线测试里 __new__ 出来的对象没有 logger）。"""
        try:
            (self.log_warning if warn else self.log_info)(msg)
        except Exception:
            pass

    def _anchor_meta(self):
        """读 assets/anchors.json（tools/build_anchors.py 生成）。

        结构：{"pages": {页面: {anchors: [锚点名], need: 命中几个算这页}},
               "anchors": {锚点名: {region: [rx,ry,rw,rh], proto: 原型文件}}}
        region 是匹配范围（相对坐标）—— 限制在小块区域里找，又快又不会满屏乱配。
        """
        if getattr(self, "_anchor_cache", None) is not None:
            return self._anchor_cache
        cfg = self._tuning().get("anchors") or {}
        self._anchor_enabled = bool(cfg.get("enabled", True))
        self._anchor_thr = float(cfg.get("threshold", 0.8))
        meta = {"pages": {}, "anchors": {}}
        if self._anchor_enabled:
            try:
                root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                path = cfg.get("file") or "assets/anchors.json"
                if not os.path.isabs(path):
                    path = os.path.join(root, path)
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f) or meta
                self._log_quiet(f"[锚点] 载入 {len(meta.get('pages', {}))} 页 / "
                                f"{len(meta.get('anchors', {}))} 个锚点")
            except Exception as e:
                self._log_quiet(f"[锚点] 载入失败，本次只用 OCR 判页：{e}", warn=True)
        self._anchor_cache = meta
        return meta

    def _anchor_page(self, frame, only=None):
        """用框架的模板匹配认页 —— 全程不读文字。

        这是「不全靠 OCR」的落点：锚点是像素级裁片（按钮/标题的原始像素），
        OCR 读花、读漏、读到繁体都不影响；命中即认定这一页。

        only="训练项页" 时只匹配这一页的锚点（读数热路径用，省掉 8 页 × N 锚点的遍历）。
        """
        if frame is None:          # 没画面就没什么可匹配的（离线测试也走这条）
            return None
        meta = self._anchor_meta()
        if not getattr(self, "_anchor_enabled", False):
            return None
        pages = meta.get("pages") or {}
        if only:
            pages = {k: v for k, v in pages.items() if k == only}
        anchors = meta.get("anchors") or {}
        thr = getattr(self, "_anchor_thr", 0.8)
        for page, d in pages.items():
            hit = 0
            for name in d.get("anchors", []):
                r = (anchors.get(name) or {}).get("region") or [0.0, 0.0, 1.0, 1.0]
                try:
                    boxes = self.find_feature(name, threshold=thr, x=r[0], y=r[1],
                                              width=r[2], height=r[3], frame=frame)
                except Exception as e:
                    # 别再静默吞异常：2026-11-24 实跑就是这里把
                    # 「assets/templates 里有不合规文件名 → 框架每次抛
                    # TemplateFormatError」藏了 3 分钟，锚点 0 命中还以为匹配不准。
                    # 同一类错误只报一次，避免刷屏。
                    boxes = []
                    if getattr(self, "_anchor_err_type", None) != type(e).__name__:
                        self._anchor_err_type = type(e).__name__
                        self._log_quiet(f"[锚点] {name} 匹配异常（本类错误只报一次）："
                                        f"{type(e).__name__}: {e}", warn=True)
                if boxes:
                    hit += 1
                    self._anchor_hits[name] = self._anchor_hits.get(name, 0) + 1
            if hit >= int(d.get("need") or 1):
                return page
        return None

    def _anchor_find(self, page, frame, names=None):
        """在指定页的锚点里找一个命中的框，返回 (锚点名, Box) —— 用来定位按钮。

        和 `_anchor_page` 的分工：
          · `_anchor_page` 只回答「这是不是这页」（认页）；
          · 这里要**具体位置**（点哪儿）。
        训练项页的「决定」按钮按训练项换版式（4 套锚点，位置差近 200px），
        OCR 读不到时用它比经验坐标可靠得多 —— 这是「能用框架就用框架」的落点。
        """
        if frame is None:
            return None, None
        meta = self._anchor_meta()
        if not getattr(self, "_anchor_enabled", False):
            return None, None
        d = (meta.get("pages") or {}).get(page) or {}
        anchors = meta.get("anchors") or {}
        thr = getattr(self, "_anchor_thr", 0.8)
        for name in (names or d.get("anchors") or []):
            r = (anchors.get(name) or {}).get("region") or [0.0, 0.0, 1.0, 1.0]
            try:
                boxes = self.find_feature(name, threshold=thr, x=r[0], y=r[1],
                                          width=r[2], height=r[3], frame=frame)
            except Exception:
                boxes = []
            if boxes:
                return name, boxes[0]
        return None, None

    def _vis_bump(self, key):
        """视觉层计数（摘要里用）：命中 / 补位（OCR 未知时接管）/ 冲突改判 / 硬否决。"""
        st = getattr(self, "_vis_stats", None)
        if st is None:
            self._vis_stats = st = {"hit": 0, "fill": 0, "conflict": 0, "veto": 0}
        st[key] = st.get(key, 0) + 1

    def _transition_veto(self, prev, page):
        """马尔可夫硬约束：上一步是 A 时判成 B，直接否决成「未知」。

        2026-11-24 玩家追问「不是说做了马尔可夫链吗」，查下来的真相是：
        只实现了 _check_transition 的**警告**，从不否决，等于没生效。
        这里才是真正的约束 —— 训练项页不可能一步跳到大厅/商店，
        局内也不可能冒出局外弹窗，判出来就直接否决，交给锚点/推进器重判。
        """
        if not prev or not page or page in ("未知",) or prev in ("未知",) or page == prev:
            return False
        return page in (PAGE_FORBIDDEN.get(prev) or ())

    def _classify(self, items, frame=None):
        """最终页面判定 = OCR 关键词 + 框架锚点，再套一层转移硬约束。

        三层（2026-11-24 重构，玩家：别再一个一个打补丁）：
          1) _classify_ocr —— 原来那套关键词规则，保留不动，负责「说出这是什么页」
          2) 框架锚点     —— FeatureSet 模板匹配（assets/anchors.json），像素级认页
          3) 转移硬约束   —— 上一步到这一步不可能出现的页，直接否决成未知
        """
        ocr_page = self._classify_ocr(items, frame)
        prev = getattr(self, "_last_page_hint", None)

        if self._transition_veto(prev, ocr_page):
            self.log_warning(f"[硬约束] {prev} → {ocr_page} 不可能出现，判定作废")
            self._vis_bump("veto")
            ocr_page = "未知"

        anchor_page = self._anchor_page(frame) if frame is not None else None
        if anchor_page and self._transition_veto(prev, anchor_page):
            self.log_info(f"[硬约束] 锚点认出 {anchor_page}，但从 {prev} 过来不可能，忽略")
            anchor_page = None

        if anchor_page:
            self._vis_bump("hit")
        why = ""
        if anchor_page and ocr_page == "未知":
            page = anchor_page
            self._vis_bump("fill")
            why = f"OCR 没认出来，锚点认出「{anchor_page}」"
        elif anchor_page and ocr_page and anchor_page != ocr_page:
            # 锚点是像素证据、关键词只是共现，冲突时信锚点
            page = anchor_page
            self._vis_bump("conflict")
            why = f"OCR 说「{ocr_page}」，锚点认出「{anchor_page}」→ 采用锚点"
        else:
            page = ocr_page or anchor_page or "未知"

        if why:
            key = (prev, page, ocr_page)
            if getattr(self, "_classify_why_key", None) != key:
                self._classify_why_key = key
                self.log_info(f"[判页] {why}")
        else:
            self._classify_why_key = None
        return page

    def _classify_ocr(self, items, frame=None):
        """判定当前页面。顺序 = 优先级，别随意调换。"""
        texts = [it["t"] for it in items]
        joined = " ".join(texts)
        has = lambda *ws: any(w in joined for w in ws)
        exact = lambda *ws: any(t in ws for t in texts)

        # 0. 局外体力不足弹窗「专注恢复」会盖在任何页面上，必须最先拦
        #    （2026-11-23 05:37：专注 9/150 点训练，弹出这个窗，脚本把它当成
        #     「器材选择」页找不到黄按钮，空转 15 步停下）
        #    注意：这里的「专注」是**局外体力**（上限 150），跟局内精力(130) 不是一回事。
        if has("专注恢复", "当前专注", "下次恢复"):
            return "专注恢复"
        # 1. 游戏内公告页（点错活动图标/进大厅自动弹）：必须先退出去，
        #    否则会一直被当成「未知页」，点中间推进 11 次后停机（08:12 实测）。
        if has("停服更新公告", "防沉迷系统公告", "公平运营公告", "内测返利说明") \
                or ("公告" in joined and "置顶" in joined):
            return "公告页"
        # 1.05 训练确认弹窗：上一次训练非正常退出，这里会出现「继续训练」，
        #      点它就能恢复中断，而不是重新开一轮。
        if "训练确认" in joined or "继续训练" in joined:
            return "训练确认"
        # 1.1 小游戏：没有主按钮，必须最先拦下来
        if has("拼图碎片", "收回碎片", "点击旋转拼图碎片"):
            return "小游戏"
        # 1.1 炒股小游戏（剧情支线，主线跑流程时就会撞上）
        #     它跟拼图不同 —— 没有「放弃」，必须玩到结束才放人，
        #     所以不能落到上面的「小游戏」分支（那个分支专门找放弃/ESC 退出）。
        #     关键词取「炒股」「买入+卖出+持仓」这种只有这一页才同时出现的组合。
        if "炒股" in joined or "请选择你要投资的股票" in joined \
                or "股票走势" in joined or has("涨跌幅", "最新价") \
                or (has("稳健型", "激进型", "平衡型") and has("确定", "放弃")):
            return "炒股小游戏"
        # 1.5 公园子页：底部也挂着六项训练项面板（投射/弹跳/…），
        #     不先拦就会在下面被「右侧竖排训练项 ≥4」认成训练项页 →
        #     于是对着公园页猛点「决定」坐标，卡死 15 步（2026-11-23 06:11 实测）。
        if "在公园干点什么好呢" in joined:
            return "公园子页"
        # 1.55 确认编队页（2026-11-23 10:37 实测）
        #      打开训练后先弹这一页：左侧「训练队伍」五人、右侧「训练笔记 / 器材」，
        #      底部「将消耗 89/30 开始训练，确定要继续吗？」+ 黄色大按钮「开始训练」。
        #      之前没有这条规则，每次都卡在这儿得人工点（玩家：我配好了，你直接点开始训练）。
        if "确认编队" in joined or ("开始训练" in joined and "将消耗" in joined)                 or ("训练队伍" in joined and "开始训练" in joined):
            return "确认编队"
        # 1.6 剧情提议「加练」对话（2026-11-23 10:28 实测）
        #     画面是训练馆 + 一段正文 + 两个黄按钮「稍等一下 / 那就开始吧」，
        #     底部还挂着队伍属性面板（投射/弹跳/… 六项），于是被下面
        #     「右侧训练项 ≥4」当成训练项页 → 对着不存在的训练项条目猛点，
        #     读数 0/5 帧、整轮作废。必须在训练项页之前拦下来。
        if ("那就开始吧" in joined and "稍等一下" in joined) or "提议再" in joined                 or ("加练" in joined and ("那就开始吧" in joined or "稍等一下" in joined)):
            return "训练提议"
        # 2. 比赛整条链
        if has("赛前准备", "跳过比赛", "前往比赛"):
            return "赛前准备"
        # 2.1 加时赛（第 7 周赢了之后追加的那三场）的赛前宣传页：只有
        #     「EXTRA MISSION · VS XX学园」+「1天战胜XX」这类标题，没有任何按钮文字，
        #     以前判成未知页，靠兜底连点中间 2~3 步才过去。
        if "EXTRA MISSION" in joined or ("VS" in joined and has("战胜", "全国", "学园", "公学")):
            return "赛前准备"
        if exact("MVP") or "MVP" in joined:
            return "比赛MVP"
        if has("目标达成", "日标达成"):
            return "目标达成"
        if exact("结算") or "结算" in joined and has("胜利", "获得"):
            return "结算"
        # 3.5 训练收尾 / 注册球员详情页（「注册球员」这四个字最独特，优先级要高于「行为卡」标签）
        # 注意：必须 exact —— 事件对话正文里常出现「训练结束后，…」这种句子，
        # 用子串匹配会把事件页认成训练结束页（23:10 卡过一次）。
        if has("注册球员", "记录笔记") or exact("训练结束"):
            return "训练结束"
        # 4. 得分行为卡（含选卡后的能力详情页）
        if has("得分行为卡", "行为卡", "请选择1个技能", "看破概率提升", "看破擅长", "无惧重围"):
            return "行为卡"
        # 4. 一段训练收尾
        if "训练评分" in joined:
            return "训练评分"
        if "恭喜获得" in joined:
            return "恭喜获得"
        # 5. 训练中的功能页
        #    注意：「契合度」同时出现在「笔记选择」(开训前选笔记) 和「笔记学习」(训练中) 两页，
        #    不能见「契合度」就判笔记学习 —— 那会把开训前的选笔记页判错，走去找「学习」按钮，
        #    结果卡在选人三连之外（2026-11-23 真实截图 OCR 测出）。先让选笔记页的独占词过关。
        if has("选择笔记", "选择队友", "选择器材"):
            if "选择队友" in joined:
                return "选择队友"
            if "选择器材" in joined:
                return "器材选择"
            return "笔记选择"
        if "契合度" in joined:
            return "笔记学习"
        if "请选择1个扭蛋" in joined:
            return "扭蛋选择"
        if has("超级扭蛋机", "转动扭蛋"):
            return "扭蛋机"
        if has("请选择一种加强训练", "加强训练成功"):
            return "经理增益"
        if "上新时间" in joined:
            return "商店"
        # 5.9 开训前的选人三连已统一提到第 5 条之前（契合度冲突）；此处只留确认编队。
        if "确认编队" in joined or "活动加成总计" in joined:
            return "确认编队"
        if "开始训练" in joined:
            return "开始训练确认"
        # 6. 训练结果（有独特词，先判；否则会被下面的「训练项页」按训练项名抢走）
        if has("属性提升了", "精力降低", "触发训练后事件", "器材+", "新境界"):
            return "训练结果"
        # 6.9 独占词快车道：这些页面底部也挂着六项训练项（投射/弹跳/…），
        #     若掉到下面 7.2「右侧训练项 ≥4」就会被误判成训练项页，对着不存在的
        #     训练项条目猛点空转。它们的标题词是唯一的，必须先拦。
        #     素材来自 logs/ok-script.log 真实「未知页面」记录（1104 次里的大头）。
        if has("本周总结", "球队评分"):
            return "本周总结"
        if "点击屏幕继续" in joined:
            return "点击继续"
        # 属性成长面板：is_attr_panel 命中就判训练结果，不再要求 ≥3 个属性词 ——
        # 实测有整页只剩「倍率加成 心·技·体 へ」这种（OCR 把属性行全吞了），
        # 严格计数会掉到未知页连点中间。它是点一下就过的非交互页，判训练结果最安全。
        # 【关键排除】开训前的「额外队友/选择队友」选人页顶部也写着「额外队友」，
        # 而「额外队友」正是 is_attr_panel 的词表之一 —— 不排除就会把它判成训练结果，
        # 结果页分支找不到「点击屏幕继续」只能点屏幕中央，15 步卡死（14:20 实测）。
        # 选人页有「筛选」开关或右侧 ≥2 张等级牌卡，属性面板两者都没有。
        pick_page = (("筛选" in joined and has("下一步", "编队", "玩家阵容", "额外队友"))
                     or (frame is not None and self._right_card_count(frame) >= 2
                         and has("下一步", "编队", "额外队友")))
        if is_attr_panel(joined) and not pick_page:
            return "训练结果"
        # 7. 训练项页：**右侧竖排**训练项条目 ≥4 个才算。
        #    不能依赖「决定」二字 —— 它是黄底白字，OCR 时有时无。
        #    也不能只看「屏幕上出现 ≥4 个训练项名」—— 公园/出行页底部挂着一整块
        #    队伍属性面板（投射 B 447/1784 …），会把它误判成训练项页，
        #    然后对着公园页猛点「决定」坐标空转到卡死（2026-11-23 06:11 实测）。
        w = frame.shape[1] if frame is not None else 1920
        right_skills = {it["t"].replace(" ", "") for it in items
                        if it["cx"] > w * 0.72 and it["t"].replace(" ", "") in self.SKILLS}
        if len(right_skills) >= 4 or (exact("决定") and has("失败率")):
            return "训练项页"
        # 7.35 开训动画（只有「训练开始」一行字）
        if "训练开始" in joined:
            return "训练结果"
        # 7.3 开训前的「额外队友 / 选择队友」选人页：右侧 4 张队友卡 + 底部六属性，
        #     顶部也写着「额外队友」，会被下面 7.4 的 is_attr_panel 当成训练后属性面板
        #     误判成「训练结果」→ 结果页分支只找「点击屏幕继续」，找不到就点屏幕中央
        #     空白，15 步后卡死（2026-11-23 14:20 实测一步没动就停）。
        #     独特区别：这页右上角有「筛选」开关，训练后的属性面板没有。
        pick_visual = frame is not None and self._right_card_count(frame) >= 2
        if (("筛选" in joined and has("下一步", "编队", "玩家阵容"))
                or (pick_visual and has("下一步", "编队", "额外队友"))):
            return "选择队友"
        # 7.4 训练后的属性成长面板（心·技·体 + 六个属性条 + 倍率加成）
        #     它不是可交互页，点一下/下一步就过；判成「未知」会走兜底连点中间，
        #     一轮下来能占十几步（实测未知页 23% 里一大半是它）。
        if is_attr_panel(joined) and sum(1 for w in ATTR_WORDS if w in joined) >= 3:
            return "训练结果"
        # 7.4b 训练类过场动画（2026-11-23 15:25 / 15:27 实跑）通用识别：
        #     开训动画（球场+底部属性条）、训练后成长动画（+13/+6 飘字、S 1011/+26）
        #     都读不到「心·技·体」，但都有**多个属性名 + 没有任何可点按钮**，
        #     而且游戏自己会播完。与其逐帧补关键词，用这个结构特征一次覆盖。
        #     排除所有主按钮词：每日页(训练/商店)、选人页(筛选/下一步)、
        #     经理增益(确定)、剧情(跳过/继续) —— 它们属性名再多也不会误判。
        if (sum(1 for w in ATTR_WORDS if w in joined) >= 3
                and not any(b in joined for b in (
                    "跳过", "决定", "下一步", "确定", "确认", "训练", "比赛",
                    "商店", "筛选", "自动选择", "选择", "继续", "休息", "出行",
                    "前往", "野餐", "精力+"))):
            return "训练结果"
        # 7.4c 训练结果计数动画的残缺帧（2026-11-24 08:49 实跑第2周）：底栏属性条
        #     只被 OCR 读到两项「投射 B 475」「运控 S 1,194」，凑不够 7.4b 的 ≥3 项，
        #     于是判未知页 → pause 收工（本轮到这就断了）。结构特征很硬：
        #     **属性名 + 单字母等级 + 3~4 位大数字（可带千分位）+ 屏幕上没有任何操作按钮**。
        #     训练项页底部也有同款属性条，但它一定有「决定」按钮 → 被下面的排除表挡住。
        if (sum(1 for w in ATTR_WORDS if w in joined) >= 2
                and re.search(r"(?<![A-Za-z])[SABCD][+]?(?![A-Za-z])", joined)
                and re.search(r"\d{1,3}(?:,\d{3})+|\d{3,4}", joined)
                and not any(b in joined for b in (
                    "跳过", "决定", "下一步", "确定", "确认", "训练", "比赛",
                    "商店", "筛选", "自动选择", "选择", "继续", "休息", "出行",
                    "前往", "野餐", "精力+"))):
            return "训练结果"
        # 7.5 出行/工资之类的结果屏：成功！+ 获得 + 下一步（顶栏也有第N周，会误判成每日页）
        if has("成功！", "成功!") and has("获得", "工资") and exact("下一步"):
            return "行程结果"
        # 7.6 出行结果剧情页（2026-11-23 15:21 实跑）：顶栏第N周 + 属性涨幅飘字
        #     「弹跳+20，防守+20」+ 剧情立绘，底部▼/点继续推进，没有行动按钮。
        #     周切换瞬间「星期-」还没刷新，OCR 读残就落到未知页停了。用「属性+数字」
        #     这种通用模式匹配，不写死具体数值/地点名。要求无行动按钮，避免抢每日页。
        # 7.6b 事件二选一（2026-11-23 16:06 实跑「记者突击采访：老实回答/黑红也是红」）：
        #     每个选项下方各挂一条收益说明「体能+10」「50%概率体能+40」，位于画面下部
        #     (cy>0.78h) 且横向分开。这两条也含「属性+数字」，会被 7.6 抢成行程结果
        #     → 找不到继续按钮、点中间空转 15 步。所以先于 7.6 判成选项页。
        #     出行结果页的涨幅在正文区(cy≈0.5h)，不会命中，区分可靠。
        # 21:34 补充：收益也可能是资源词（「算了，先放过她 精力+30」/
        #     「让她挑战一下极限 体能+30」），OCR 还会带个爱心前缀读成「❤体能+30」。
        #     用 GAIN_WORDS 覆盖属性 + 资源，正则用 search 天然容忍前缀符号。
        if frame is not None:
            gain_items = [it for it in items
                          if it["cy"] > 0.78 * frame.shape[0]
                          and re.search(r"(?:%s)\s*[+＋]\s*\d+|\d+%%\s*概率.*(?:%s)\s*[+＋]\s*\d+"
                                        % ("|".join(GAIN_WORDS), "|".join(GAIN_WORDS)), it["t"])]
            if len(gain_items) >= 2:
                return "选项"
        gain = re.search(r"(?:%s)\s*[+＋-]\s*\d+" % "|".join(ATTR_WORDS), joined)
        if (gain and re.search(r"第\s*\d+\s*周", joined)
                and not self._action_btn(items, frame)):
            return "行程结果"
        # 7.9 居中竖排胶囊选项（集训事件三选一这类）：先认出来再选，
        #     不必先白白点 3 次「跳过」等 same_count 攒够（每次约 5 秒）。
        if frame is not None and "跳过" in joined and len(self._pills(items, frame)) >= 2:
            return "选项"
        # 8. 剧情 / 事件（有「跳过」的大多是对话页；事件页另有黄卡片兜底）
        if exact("跳过") or "跳过" in joined:
            return "剧情事件"
        # 8.1 无「跳过」的纯对话页：右下角白色▼ 就是「点一下继续」的唯一标记。
        #     2026-11-23 15:23「喂！不要一言不发就跑掉啊！」；15:32 剧本事件对话
        #     「是！那就请您继续指导球队训练吧！」——后者还读出「剧本事件/角色名」
        #     等标签，文字多达 11 段，所以**不按文字段数限制**，只靠▼视觉+无行动按钮。
        if (frame is not None and self._has_dialog_arrow(frame)
                and not self._action_btn(items, frame)):
            return "剧情事件"
        # 8.2 事件选择后的「回应对话」页（2026-11-23 21:59 实跑）：
        #     点完二选一黄卡片，画面只剩顶栏 + 对话框里一句台词，右下角是**角色头像**
        #     而不是白色▼，8.1 的箭头规则接不住 → 判未知页、按兜底 pause 直接收工。
        #     特征：有第N周、中部一句 ≥10 字的中文长句、没有行动按钮/跳过/继续。
        #     判剧情事件后 _do_story 会点对话框推进。要长句是为了不误伤
        #     「商店购买浮层」那种顶栏 + 几个短词的页面。
        #     抽成方法，离线扫描时可 monkeypatch 关掉来对比新旧分类、验证零误伤。
        if self._is_event_reply(items, frame):
            return "剧情事件"
        # 8.5 训练事件 / 球星卡事件：正文常提到「公园」这类词，会被下面的出行规则抢走
        if "训练事件" in joined and not exact("跳过"):
            return "选项"
        # 9. 出行三连。
        #    不能用「正文里出现公园/餐厅」判定 —— 事件正文写「篮球社集体在公园打街球」，
        #    结果整页被当成出行页，找不到地点就一直警告空转（2026-11-23 06:17 卡死 14 步）。
        #    出行页一定至少有 2 个「地点名」文本框。
        trip_hits = [it for it in items if it["t"] in self.TRIP_NAMES]
        if "在公园干点什么好呢" in joined:
            return "公园子页"
        if exact("步行街") or len(trip_hits) >= 2:
            return "出行"
        if any(t.startswith("前往") for t in texts):
            return "前往"
        if "幸运大抽奖" in joined:
            return "抽奖"
        # 10. 训练剧本（这页也有 图鉴/成就 图标，会被大厅抢走）
        if has("训练剧本") or (has("启程") and has("托管")) or has("大师模式"):
            return "训练剧本"
        # 10.5 全屏遮罩弹窗（「新目标」「距离比赛还有N天」这类）
        #      正文下方固定带「点击屏幕继续」，但**被遮住的每日页按钮仍会被 OCR 读到**，
        #      于是判成每日页 → 在遮罩上猛点「训练」，15 步毫无变化后停机
        #      （2026-11-23 10:52 实测：第 5 周星期 2，精力 130/130，连点 15 次）。
        #      只要有这一行提示，就是「点一下继续」，不是功能页。
        if "点击屏幕继续" in joined:
            return "点击继续"
        # 11. 大厅：一定有一个「训练」按钮（exact），再配协会/好友/图鉴这些入口。
        #     必须排在「每日页」之前：大厅也可能带「第N周」，右下角也有「训练」按钮，
        #     否则回大厅会被当成每日页，收工判定就永远等不到（saw_training 白攒）。
        if exact("训练") and has("协会", "好友", "图鉴", "成就", "招募"):
            return "大厅"
        # 【2026-11-23 12:05 实跑兜底】OCR 偶尔读不到「训练」两个字，整个大厅就被判成
        # 未知页，连点 11 次屏幕中间后停机（那一轮 0 步有效）。大厅有一批**独占词**，
        # 凑够 3 个且没有「星期X」（每日页才有的）就认大厅。
        hall_hits = sum(1 for w in ("助教", "商城", "飞跃计划", "协会赛", "表演赛",
                                    "招募", "卡册", "图鉴", "成就", "协会")
                        if has(w))
        if hall_hits >= 3 and not re.search(r"星期[一二三四五六日天]", joined):
            return "大厅"
        # 12. 每日行动页（枢纽）：带「第N周」+ 右下角真的有行动按钮。
        #     只认「第N周」不行 —— 行程结果、野餐中、粉丝对话这些页顶栏也带第N周，
        #     被当成每日页后找不到按钮，就会一直警告空转（2026-11-23 06:13 实测）。
        if re.search(r"第\s*\d+\s*周", joined) and self._action_btn(items, frame):
            return "每日页"
        # 12.1 按钮文字被读花/被提示条挤掉的每日页（2026-11-23 13:0x 实跑：
        #      ['第1周','星期二','3 113/130','60','6','数早三','购买并使用防守秘籍']
        #      被当未知页，白点好几步）。「第N周 + 星期X + 精力 a/b」这三件套
        #      是这游戏里每日页独有的，凑齐就认；再用其他页的硬特征排掉。
        #      2026-11-23 16:18：商店购买后弹「购买并使用XX」浮层，OCR 只读到
        #      顶栏+「购买并」6 段字、没有行动按钮，以前会被这条当每日页 →
        #      _do_daily 找不到按钮就点屏幕中间 → 点回商品重复买。
        #      所以：带「购买并」且没有行动按钮时不认每日页，交给 overlay 等待消失。
        buy_toast = ("购买并" in joined or "购买并使用" in joined) \
            and not self._action_btn(items, frame)
        if (re.search(r"第\s*\d+\s*周", joined)
                and re.search(r"星期[一二三四五六日天]", joined)
                and re.search(r"\d{1,3}\s*/\s*\d{1,3}", joined)
                and not buy_toast
                and not has("MVP", "赛前准备", "目标达成", "点击屏幕继续", "心·", "·体")):
            return "每日页"
        # 12.15 「附加赛目标」弹窗盖住的每日页（2026-11-23 17:30 实跑）：
        #      OCR 只读 ['附加赛目标','商店','休息','训练']——顶栏第N周/星期X/精力
        #      被半透明弹窗遮住读不出，12/12.1 都不认；但底部训练按钮是黄色可点的。
        #      这类弹窗只是提示，不影响行动；有训练/休息/商店中≥2个按钮就按每日页点。
        if has("附加赛目标") and sum(1 for w in ("训练", "休息", "商店") if has(w)) >= 2:
            return "每日页"
        # 12.2 视觉兜底：OCR 一个字都没认全，但右侧有 ≥2 张带等级牌的成员卡
        #      → 这是选人页（选择队友/额外队友）。纯靠 OCR 时这类页常被漏读成未知，
        #      白点中间空转；用画面硬特征补上（2026-11-23 玩家要求 OCR 之外加视觉信号）。
        if frame is not None and self._right_card_count(frame) >= 2 \
                and not has("MVP", "结算", "赛前准备"):
            return "选择队友"
        # 兜底：只剩「第N周」不代表就是每日页（很多结果页也带顶栏），
        # 交给未知页兜底（点屏幕中间推进）比硬当成每日页安全。
        return "未知"

    def _read_week(self, items):
        joined = " ".join(it["t"] for it in items)
        m = re.search(r"第\s*(\d+)\s*周", joined)
        week = int(m.group(1)) if m else None
        day = None
        m2 = re.search(r"星期([一二三四五六日天])", joined)
        if m2:
            day = WEEKDAYS.get(m2.group(1))
        return week, day

    # ---------- 各页面处理 ----------

    ACTION_BTNS = ("训练", "休息", "出行", "比赛")

    def _action_btn(self, items, frame):
        """右下角那排行动按钮（训练/休息/出行/比赛）。

        这是「每日行动页」最硬的特征：文字可以读丢、第N周到处都有，
        但按钮一定在那个区域。先精确匹配，读花了再退回子串匹配。
        """
        if frame is None:
            return None
        it = self._btn_in_area(items, self.ACTION_BTNS, frame)
        if it:
            return it
        return self._btn_in_area(items, self.ACTION_BTNS, frame, exact=False)

    def _shop_button(self, items, frame):
        """每日页的「商店」入口。

        2026-11-23 12:10 实跑：整轮一次都没进店。原因是这里原来用 `_btn_in_area`，
        而它只在**右下角**（x>0.6w, y>0.45h）找按钮 —— 每日页的「商店」不在那个区域
        （掉线弹窗的 OCR 里能看到「商店」二字，说明这页确实有）。
        改成全屏找，右下角优先，并排除顶部状态栏里的同名小图标。
        """
        h, w = frame.shape[:2]
        cands = [it for it in items if it["t"].strip() == "商店"
                 and it["cy"] > h * 0.15 and it["cx"] > w * 0.05]
        if not cands:
            # OCR 会把「商店」拆成「商」「店」两个框（2026-11-23 12:21 实测：
            # 每日页文字是 ['...','商','店','休息',...]），精确匹配永远落空。
            # 把同一行、左右相邻的「商」「店」合起来当按钮。
            shang = [it for it in items if it["t"].strip() == "商"]
            dian = [it for it in items if it["t"].strip() == "店"]
            for a in shang:
                for b in dian:
                    if abs(a["cy"] - b["cy"]) < 45 and 0 < b["cx"] - a["cx"] < 130:
                        cands.append({"t": "商店",
                                      "cx": (a["cx"] + b["cx"]) / 2,
                                      "cy": (a["cy"] + b["cy"]) / 2,
                                      "b": a["b"]})
        if not cands:
            return None
        cands.sort(key=lambda x: (x["cx"] > w * 0.60 and x["cy"] > h * 0.45,
                                  x["cx"] + x["cy"]), reverse=True)
        it = cands[0]
        self.log_info(f"商店按钮 @({int(it['cx'])},{int(it['cy'])}) "
                      f"相对({it['cx']/w:.2f},{it['cy']/h:.2f})")
        return it

    def _btn_in_area(self, items, words, frame, exact=True):
        """只在「右下角按钮区」找按钮。

        踩坑（22:38）：每日页左上角有一处「休息」标签 @(398,169)，
        按文字找第一个就点到了它，于是「休息」点了 9 次都没反应。
        行动按钮都在屏幕右下：x > 0.60w、y > 0.45h。
        """
        h, w = frame.shape[:2]
        cands = []
        for it in items:
            if it["cx"] < w * 0.60 or it["cy"] < h * 0.45:
                continue
            for word in words:
                if (it["t"] == word) if exact else (word in it["t"]):
                    cands.append(it)
                    break
        if not cands:
            return None
        return max(cands, key=lambda x: x["cx"])

    def _do_daily(self, items, frame):
        """每日行动页：比赛 > 训练（精力够）> 出行（周末补精力）> 休息。

        资源口径（2026-11-23 玩家纠正）：
          这里比的是**局内精力**（⚡ a/130），不是顶栏那个局外专注(150)。
          精力只有两个来源：局内事件「精力+X」、周末出行「公园」。
          「休息」只推进一天、**不补精力**，所以精力不够且当天能出行时，
          应该先出行去公园，而不是直接休息。
        """
        # 已经回到每日页 → 探笔记流程结束，清掉「还剩几层要退」的状态
        self.trip_probe_back = 0

        # 二次确认弹窗（休息/训练前会弹「…是否要休息？」+ 取消/确认）
        ok = self._find(items, ["确认"], exact=True)
        cancel = self._find(items, ["取消"], exact=True)
        if ok and cancel:
            self._click_item(ok, "[确认弹窗]")
            return True

        match = self._btn_in_area(items, ["比赛"], frame)
        if match:
            self._click_btn(match, "[比赛日-进赛前准备]", frame)
            return True

        stamina_min = int(self.config.get("训练精力下限") or 12)
        goal = self._goal_cfg()
        train_days = [int(x) for x in (goal.get("train_days") or [1, 2, 3, 4, 5, 6])]
        rest_days = [int(x) for x in (goal.get("rest_days") or [7])]
        hard_min = int(goal.get("stamina_hard_min") or 0)
        week_no, wd = self._read_week(items)
        must_train = wd in train_days
        rest_only = wd in rest_days
        # 训练日：门槛降到 stamina_hard_min（默认 1）—— 玩家的目标是「周一到周六都练」，
        #   历史日志证明 12 点精力也能练完一次（旧门槛 40 曾把整个周六挡成出行）。
        # 非训练日：保持原门槛，只在精力够的时候才练（避免周日乱练）。
        need = min(stamina_min, hard_min) if must_train else stamina_min
        st = self.stamina if self.stamina is not None else 999
        enough = st >= need
        train = self._btn_in_area(items, ["训练"], frame)
        rest = self._btn_in_area(items, ["休息"], frame)
        go = self._btn_in_area(items, ["出行"], frame)
        shop = self._shop_button(items, frame)

        # 商店（玩家 2026-11-23：只买 0 元 + 精力 <50 买精力药）
        #   以前这里没接入口，整轮一次店都没进过 → 精力药从来没买过。
        #   放在训练前：先把能补的精力补上，再决定练什么。
        if shop and self._want_shop(items):
            self.shop_day = self._day_key(items)
            self.shop_week = (self.shop_day or "").split("-")[0] or None
            self._click_btn(shop, f"[进商店 精力{self.stamina} 看货架]", frame)
            return True
        if not shop:
            day = self._day_key(items) or ""
            if self.shop_week != day.split("-")[0]:
                self.log_info(f"每日页没找到「商店」按钮（本周期还没看过货架）"
                              f"｜文字 {[it['t'] for it in items][:12]}")

        # 探笔记：2026-11-23 22:14 玩家反馈「周日没有检查笔记，直接去公园了」。
        #   旧逻辑把探笔记塞在「有训练且精力够」分支里，周日没有训练（休息日），
        #   直接走下面的「精力不足 → 出行去公园」，整周最后一天从不检查笔记。
        #   现在统一提到所有行动分支之前：每天只要有出行入口，先探一次步行街扭蛋
        #   看有没有【笔记 / 继承】；没有就两级右上角退回，再按原规则训练/去公园。
        #   每天仍只探一次（trip_probe_done 记账），退回失败只放弃当天。
        day_key = self._day_key(items)
        # 重试计数按天清零：昨天试了 3 次失败，不该让今天也直接放弃。
        # getattr 兜底是为了让离线测试桩（绕过 __init__ 构造）也能跑。
        if getattr(self, "trip_probe_retry_day", None) != day_key:
            self.trip_probe_retry_day = day_key
            self.trip_probe_retry = 0
        if go and self._can_probe_trip(day_key):
            self.trip_probe = True
            # **不在这里写 trip_probe_done**：只有真的进了出行页才算探过
            #（见 _do_trip 开头）。否则点击被覆盖窗口挡住时会误判成「探完了、没笔记」。
            self.trip_probe_retry = getattr(self, "trip_probe_retry", 0) + 1
            self.trip_back_tries = 0
            self._click_btn(go, f"[探出行-看有没有笔记 试{self.trip_probe_retry}/3 "
                                f"精力{self.stamina}/{self.stamina_max}]", frame)
            return True

        if train and enough and not rest_only:
            probed = (self.trip_probe_done == day_key)
            if probed:
                self.log_info(f"[探笔记] 已回到每日页 → 当天照常训练（精力 {self.stamina}/{self.stamina_max}）")
            tag = "[探笔记后-训练" if probed else "[训练"
            self._click_btn(train, f"{tag} 精力{self.stamina}/{self.stamina_max} 星期{wd}"
                                   f"{'（目标：周一到周六全练）' if must_train else ''}]", frame)
            return True

        # 星期几属于 rest_days（默认周日）：玩家的口径是「周日的休息 = 去公园回精力」，
        #   所以有出行入口就直接去，绝不点「休息」——休息只推进一天、不回精力。
        if rest_only and go:
            self._click_btn(go, f"[星期{wd} 休息日-出行去公园补精力 精力{st}/{self.stamina_max}]", frame)
            return True

        # 精力不足：先看能不能去公园（唯一能明显回精力的地方）
        if go and not enough:
            if self.trip_probe_done == day_key:
                self.log_info("[探笔记] 已回到每日页 → 今天没有训练，按原规则去公园补精力")
            self._click_btn(go, f"[精力{self.stamina}<{need}-出行去公园补精力]", frame)
            return True

        if rest:
            self._click_btn(rest, f"[休息 精力{self.stamina}<{need}，休息不补精力只推一天]", frame)
            return True

        if train and not rest_only:  # 没别的路，先点训练（不足会弹专注恢复，那边有兜底）
            self._click_btn(train, "[训练-精力可能不足]", frame)
            return True

        # 周末（六/日）：每日页只有「出行」一个行动按钮，不走就走不动这一天
        if go:
            self._click_btn(go, "[周末-出行]", frame)
            return True

        # 兜底 0：这屏其实是个结果展示页（成功！/工资），点「下一步」就走
        nxt = self._find(items, ["下一步"], exact=True)
        if nxt:
            self._click_item(nxt, "[结果页-下一步]")
            return True
        # 兜底：这一屏可能其实是「事件/对话」盖在每日页上（顶部栏带第N周，容易误判）
        if self._event_cards(frame, items):
            self.log_info("每日页上没有行动按钮，但底部有成排黄卡片 → 按事件处理")
            return self._do_event(items, frame)
        if self._find(items, ["跳过"], exact=True):
            self._click_item(self._find(items, ["跳过"], exact=True), "[跳过对话]")
            return True

        self.log_warning(f"每日页没有 比赛/训练/休息 按钮，文字: {[i['t'] for i in items][:12]}")
        it = self._find(items, ["关闭"], exact=True)
        if it:
            self._click_item(it, "[关闭弹窗]")
            return True
        self.click_relative(0.5, 0.5)
        return True

    def _do_choice(self, items, frame):
        """居中竖排选项页：选一个。没有偏好就从上往下依次试（点过的下次换下一个）。"""
        pills = self._pills(items, frame)
        if not pills:
            return self._do_story(items, frame)
        idx = self.choice_idx % len(pills)
        it = pills[idx]
        self._click_item(it, f"[选项 {idx + 1}/{len(pills)}]")
        self.choice_idx = idx + 1
        return True

    def _do_wheel(self, items, frame):
        """幸运大抽奖：点「转一转」。抽完会弹结果，交给下一轮判页处理。

        实测（22:18）：事件选「热情答应（随机比赛技能）」之后进这一屏，
        底部中间一个黄色「转一转」，点屏幕中间没用。
        """
        it = self._find(items, ["转一转", "转一次"], exact=True) or self._find(items, ["转一转"])
        if it:
            self._click_item(it, "[抽奖-转一转]")
            return True
        self.log_info("没读到大转盘按钮，点底部中间的黄色按钮位置")
        self.click_relative(0.49, 0.875)
        return True

    def _do_pretrain(self, items, frame):
        """赛前准备：一律跳过比赛（玩家规则里从没打过比赛）。"""
        it = self._find(items, ["跳过比赛"])
        if it:
            self._click_btn(it, "[跳过比赛]", frame)
            return True
        it = self._find(items, ["跳过"])
        if it:
            self._click_btn(it, "[跳过]", frame)
            return True
        self.log_warning("赛前准备页没找到「跳过比赛」，点屏幕中间")
        self.click_relative(0.5, 0.5)
        return True

    def _do_next(self, page):
        """通用「点下一步推进」：优先主按钮，其次点屏幕中间。"""

        def handler(items, frame):
            if self._click_confirm(items, frame):
                return True
            self.log_info(f"{page}：没有可点的主按钮，点屏幕中间推进")
            self.click_relative(0.5, 0.5)
            return True

        return handler

    def _do_action_card(self, items, frame):
        """得分行为卡：**必须先选卡**，没选卡时「确定」是灰的，点了没反应。

        实测（22:13 卡住那一屏）：三张卡 耐力恢复/扎稳打/无惧重围，右下角「确定」灰色。
        之前不判颜色直接点「确定」，于是在这页空转 8 次停下。
        """
        h, w = frame.shape[:2]
        joined_all = " ".join(it["t"] for it in items)
        # 2026-11-23 15:40 实跑：行为卡页顶部「请选择1个技能」**选完卡也不消失**，
        # 右下「确定」按钮一直黄。所以既不能只看颜色点确定（没选卡点了没用），
        # 也不能只看标题一直选卡（选完卡还选 → 连 15 步不变）。
        # 用 card_pick_pending 在两个动作间交替：选一次卡 → 下一轮点一次确定。
        pick_pending = getattr(self, "card_pick_pending", False)
        ok = self._find(items, ["确定", "确认"], exact=True)
        if ok and pick_pending:
            self.card_pick_pending = False
            spot = self._yellow_btn_center(frame, ok)
            if spot:
                self.log_info(f"点击「确定」@({int(spot[0])},{int(spot[1])}) [选卡后确定]")
                self.click(spot[0], spot[1])
            else:
                self._click_item(ok, "[确定行为卡]")
            return True
        # 有些屏（注册球员详情）没有「确定」，只有「下一步」
        nxt = self._find(items, ["下一步"], exact=True)
        if nxt and not ok:
            self._click_item(nxt, "[行为卡-下一步]")
            return True

        skip = ("OFF", "详细", "通用", "得分手", "防守者", "中锋", "大前", "小前", "控卫", "分卫",
                "能力详情", "行为卡")
        # 2026-11-23 16:00 实跑：顶部 y≈0.25h 有「已装备行为卡」展示（如「无惧重围★★」），
        # 和候选卡同名。旧筛选 0.12~0.80 把它当候选，next() 取第一个 → 点卡面算到屏幕顶。
        # 真正的候选卡名标签在 y≈0.44h（实测 cy≈496/1119），收紧到 0.38~0.62 排除顶部展示。
        cards = [it for it in items
                 if 0.38 < it["cy"] / h < 0.62
                 and 2 <= len(it["t"]) <= 8
                 and it["t"] not in skip
                 and not re.match(r"^[\d/\.\+%★]+$", it["t"])]
        if not cards:
            self.log_warning(f"行为卡页没找到可选项，文字: {[i['t'] for i in items][:14]}")
            self.click_relative(0.5, 0.5)
            return True
        # 行为卡偏好读 strategy.yaml → behavior_card.prefer（P2 前写死在这里）
        prefer = [str(x) for x in (
            (self._tuning().get("behavior_card") or {}).get("prefer")
            or ["无惧重围", "看破擅长", "看破", "得分手"])]
        best = None
        for p in prefer:
            best = next((c for c in cards if p in c["t"]), None)
            if best:
                break
        best = best or cards[-1]
        # 注意：OCR 读到的是卡片上的「名称标签」（约在卡面图下方，y≈0.46h），标签本身点不动。
        # 2026-11-23 15:41 实跑修正：旧代码往下偏移(y+0.2h≈0.66h)点到了卡片下方空白，
        # 连点 15 次没反应；真正的卡面图在标签**上方**（y≈0.25h），往上偏移才选中。
        x = best["cx"]
        # 卡名标签 cy≈0.44h，卡面图中心约在其上方 0.16h（实测 496-180≈316）。
        # 旧值 h*0.22 偏移过大再被下限压到 0.12h，点到屏幕顶。
        y = max(best["cy"] - h * 0.16, h * 0.20)
        self.log_info(f"选行为卡「{best['t']}」→ 点卡面 @({int(x)},{int(y)})")
        self.click(x, y)
        self.card_pick_pending = True    # 下一轮直接点确定，别再重复选卡
        self.sleep(0.8)
        return True

    def _do_egg_select(self, items, frame):
        """扭蛋三选一：只买目标关键词（笔记继承），且价格不超上限。"""
        kw = str(self.config.get("扭蛋目标关键词") or "笔记,继承").split(",")
        kw = [k.strip() for k in kw if k.strip()]
        cap = int(self.config.get("扭蛋最高价") or 120)

        # ===== 探笔记（玩家 2026-11-23 21:01 确认）=====
        # 步行街扭蛋三选一里出现【笔记 / 继承】才算有笔记：
        #   有 → 选它（笔记 > 训练）；
        #   没有 → 两级右上角返回回每日页，当天照常训练（探笔记不消耗行动）。
        if self.trip_probe_back >= 2:
            joined = " ".join(i["t"] for i in items)
            hits = [k for k in ("笔记", "继承") if k in joined]
            week, day = self._read_week(items)
            self._audit_save(week, day, "NOTE_EGG", frame,
                             f"[探笔记] 步行街扭蛋三选一\n"
                             f"OCR：{[i['t'] for i in items]}\n"
                             f"命中【笔记/继承】：{hits or '无'}\n")
            if not hits or self.notes_done:
                opts = [i["t"] for i in items if 0.2 < i["cy"] / frame.shape[0] < 0.55][:6]
                self.log_info(f"[探笔记] 扭蛋三选一没有「笔记/继承」→ 两级右上角退回，回每日页走训练"
                              f"｜选项 {opts}")
                self.trip_probe_back = 1
                return self._leave_top_right(items, frame, "[退第1层 扭蛋页→出行页]")
            self.stat["note"] += 1
            self.log_info(f"[探笔记] 命中 {hits} → 选笔记扭蛋（本轮笔记次数 {self.stat['note']}）")
            self.trip_probe_back = 0   # 走正常购买流程，不用退回

        if self.notes_done:
            self.log_info("本轮已经拿过笔记继承，这次不买")
            return self._leave_top_right(items, frame, "[已拿过笔记]")

        cands = [it for it in items if any(k in it["t"] for k in kw)]
        cands = [c for c in cands if 0.2 < c["cy"] / frame.shape[0] < 0.55]
        if not cands:
            self.log_info(f"三个扭蛋里没有「{kw}」，跳过不买")
            # 2026-11-24 10:10 复盘：这里曾经被我改成「退不出去就强制买最便宜」——
            # 是错的。真实原因是**点击被上层窗口截走**（WorkBuddy 覆盖层压住游戏右上角），
            # 不是弹窗退不出去。玩家指出「右上角可以退出，昨天教过」并实测确认：
            # 置顶游戏后点 ↩ 画面变化 26%，弹窗正常关闭。所以这里只负责退，不负责买。
            if not self._leave_top_right(items, frame, "[无目标扭蛋]"):
                self.log_warning("[扭蛋] 这次没退出去（多半是点击被挡），下一步重试；不买")
            return True

        best = cands[0]
        price = None
        for it in items:
            if abs(it["cx"] - best["cx"]) < frame.shape[1] * 0.12 and it["cy"] > best["cy"]:
                n = _num(it["t"])
                if n and 0 <= n <= 999 and ("/" not in it["t"]):
                    price = n
                    break
        self.log_info(f"选中扭蛋「{best['t']}」，价格 {price}，上限 {cap}")
        if price is not None and price > cap:
            self.log_warning(f"价格 {price} 超过上限 {cap}，放弃")
            return self._leave_top_right(items, frame, "[超上限]")

        self._click_item(best, "[扭蛋目标]")
        self.sleep(self.config.get("等待秒数"))
        f2 = self._frame()
        if f2 is None:
            return True
        self._click_confirm(self._texts(f2), f2)
        self.notes_done = True
        return True

    def _do_egg_machine(self, items, frame):
        """超级扭蛋机：点可抓的机器（有次数的那台），否则退出。"""
        it = self._find(items, ["扭蛋"])
        if it:
            self._click_item(it, "[进入扭蛋选择]")
            return True
        return self._leave_top_right(items, frame, "[扭蛋机无可抓]")

    def _do_enhance(self, items, frame):
        """经理增益三选一：优先最高加成。"""
        if self._click_confirm(items, frame):
            return True
        cands = self._all(items, r"\+\s*\d{2,}")
        cands = [c for c in cands if 0.2 < c["cy"] / frame.shape[0] < 0.8]
        best = None
        strategy = (self._tuning().get("enhance") or {}).get("strategy")
        if not strategy:
            # yaml 没写就退回任务配置里的中文值（2026-11-26：这个键以前是摆设，
            # 改了没反应；工作台「经理增益选哪个」会把两边一起写）
            v = self.config.get("经理增益策略")
            strategy = "first" if v == "第一个" else "max_bonus"
        if cands:
            # max_bonus=取加成最大；first=取第一个（配置化，改 yaml 就能换）
            best = (max(cands, key=lambda c: _num(c["t"]) or 0)
                    if strategy == "max_bonus" else cands[0])
            self.log_info(f"经理增益（{strategy}）候选项: {[c['t'] for c in cands]}")
        if best is None:
            self.log_warning("没识别到加成数值，兜底点第一个选项")
            card = self._all(items, r"训练|强化")
            best = card[0] if card else None
        if best:
            self._click_item(best, "[最高加成]")
            self.sleep(self.config.get("等待秒数"))
            f2 = self._frame()
            if f2 is not None:
                self._click_confirm(self._texts(f2), f2)
            return True
        return False

    TRIP_NAMES = ["散步", "划船", "野餐", "公园", "游戏厅", "餐厅", "步行街", "休息", "发呆"]
    TRIP_FALLBACK = ["公园", "散步", "野餐", "划船", "餐厅", "步行街"]
    # 公园子页的兜底顺序。玩家 2026-11-23：「周日的休息指的就是去公园」——
    # 休息日的唯一目的就是把精力补回来，所以标签读不到数值时也要先挑野餐（大量恢复），
    # 而不是按地点顺序挑散步。原表是 公园>散步>野餐>划船，进子页后会把散步排到野餐前。
    PARK_FALLBACK = ["野餐", "划船", "散步"]
    GAIN_KEYS = ("精力", "属性", "零用钱", "金钱", "笔记")

    # OCR 吞掉数字时（只留「精力++」/「精力+」），按加号个数还原档位。
    # 单加＝小档，双加＝大档。数值是实测出来的常见档位，不是拍脑袋的权重。
    # 内置兜底值；正常从 strategy.yaml → event.keywords / symbol_gain 读。
    SYMBOL_GAIN = {"精力": (30, 70), "属性": (20, 40)}

    def _event_cfg(self):
        """event 段配置（代码只读这里，改 yaml 就能换优先级）。"""
        return (self._tuning().get("event") or {}) if hasattr(self, "_tuning") else {}

    def _keywords(self, tier_name):
        """某一档的关键词；没配就用内置兜底（保证老配置不会因为缺键而崩）。"""
        cfg = self._event_cfg()
        kw = (cfg.get("keywords") or {}).get(tier_name)
        if kw:
            return [str(k) for k in kw]
        fallback = {"笔记": ("笔记",), "技能": ("技能", "专属", "绝技"),
                    "精力": ("精力",),
                    "属性": ("属性", "体能", "运控", "意识", "弹跳", "防守", "投射"),
                    "零用钱": ("零用钱", "金钱", "金币")}
        return list(fallback.get(tier_name, ()))

    def _tier_index(self, name):
        """档位名在 event.priority 里的序号（越靠前越高）。没配的档排最后。"""
        priority = [str(x) for x in (self._event_cfg().get("priority") or
                                     ["笔记", "技能", "精力", "属性", "零用钱"])]
        return priority.index(name) if name in priority else len(priority)

    def _gain(self, text, key):
        """从「精力+70」「精力++」「属性+30」里取增益数值。

        玩家 2026-11-23 强调：局内精力只能靠事件和公园补，
        所以 +70 那档 > +30 那档，必须按数值选，不能只看有没有「精力」二字。

        2026-11-23 06:43 补：OCR 经常把数字吃掉，只剩「精力++」/「精力+」。
        原来这里一律返回 1 —— 于是 +70 档和 +30 档打平，打分靠坐标决胜负，
        玩家看到的就是「差 21 点却挑了 +70」。现在按加号个数还原成 30/70。
        """
        t = (text or "").replace("＋", "+").replace(" ", "")
        m = re.search(rf"{key}\s*\+\s*(\d+)", t)
        if m:
            return int(m.group(1))
        m2 = re.search(rf"{key}(\++)", t)
        if m2:
            pair = (self._event_cfg().get("symbol_gain") or {}).get(key)
            small, big = (int(pair[0]), int(pair[1])) if pair and len(pair) >= 2 \
                else self.SYMBOL_GAIN.get(key, (1, 2))
            return big if len(m2.group(1)) >= 2 else small
        return 0

    def _gap(self):
        """局内精力还差多少到顶。读不到时按 999 处理（当作缺口很大）。"""
        if self.stamina is None:
            return 999
        return max(0, (self.stamina_max or 130) - self.stamina)

    SKILL_WORDS = ("技能", "专属", "绝技")
    ATTR_WORDS = ("体能", "运控", "意识", "弹跳", "防守", "投射", "属性")

    def _reward_key(self, text):
        """统一的奖励优先级，顺序读 strategy.yaml → event.priority。

        返回 (tier, eff, at, money)：
          tier = N - priority.index(档位名)  （排在 priority 第一项的最高）
          eff  = 实际能吃进精力条的点数（**超出上限的部分不算**）
          at   = 属性点数

        「未溢出」是玩家 2026-11-23 06:43 专门点出来的：精力只差 21 时，
        +70 和 +30 都只能吃进 21，多出来的 49 白扔 —— 这时该选顺带加属性的 +30。
        以前直接拿标签原始数值当收益（+70 永远赢），所以才会挑错。

        P2 前这里写死 5 档、yaml 只写 3 档，两边对不上；现在代码只读配置。
        """
        t = (text or "").replace("＋", "+").replace(" ", "")
        cfg = self._event_cfg()
        priority = [str(x) for x in (cfg.get("priority") or
                                     ["笔记", "技能", "精力", "属性", "零用钱"])]
        # tier = 排在它后面的档数：默认 5 档时 笔记4/技能3/精力2/属性1/零钱0，
        # 与 P2 之前的编号完全一致（出行页有 key[0] < 3 这类判断依赖它）。
        highest = len(priority)

        if re.search(r"\d+\s*%", t) and bool(cfg.get("reject_probability", True)):
            return (0, 0, 0, 0)      # 概率型奖励一律不要

        # 按配置顺序逐档匹配：越靠前 tier 越高
        for name in priority:
            if any(k in t for k in self._keywords(name)):
                tier = highest - 1 - priority.index(name)
                if name == "精力":
                    gap = self._gap()
                    st = self._gain(t, "精力")
                    at = self._gain(t, "属性")
                    if not at and any(k in t for k in self._keywords("属性")):
                        at = 1
                    eff = min(st, gap) if gap > 0 else 0
                    if eff <= 0:     # 精力已满 → 这档没意义，继续看后面的档
                        continue
                    return (tier, eff, at, 0)
                if name == "属性":
                    at = self._gain(t, "属性")
                    return (tier, 0, max(1, at), 0)
                return (tier, 0, 0, 0)

        # 没匹配到任何档：至少把「零用钱」认出来（它可能不在 priority 里）
        if any(k in t for k in self._keywords("零用钱")):
            return (0, 0, 0, 1)
        return (0, 0, 0, 0)

    def _key_value(self, key):
        """把 _reward_key 的元组压成一个数值，方便沿用旧的「取最大值」写法。

        档间分差读 event.score_step（默认 10000），保证高 tier 永不被低 tier 的数值翻盘。
        """
        tier, eff, at, money = key
        step = int(self._event_cfg().get("score_step") or 10000)
        return tier * step + eff * 10 + at + money * 0.1

    def _near_text(self, items, anchor, frame, keys=GAIN_KEYS):
        """取某个按钮/卡片附近的收益文字（同一张卡里，上下 10% 屏高、左右 20% 屏宽）。"""
        h, w = frame.shape[:2]
        out = []
        for it in items:
            if it is anchor:
                continue
            if abs(it["cx"] - anchor["cx"]) > w * 0.20:
                continue
            if abs(it["cy"] - anchor["cy"]) > h * 0.10:
                continue
            if any(k in it["t"] for k in keys):
                out.append(it["t"])
        return " ".join(out)

    def _excluded_place(self, text):
        """规则里明确不去的出行地点（默认排除游戏厅 —— 进去就是拼图小游戏）。"""
        raw = str(self.config.get("排除地点") or "")
        words = [x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip()]
        return any(x in text for x in words)

    def _day_key(self, items):
        """今天是谁：'第N周-星期X'，读不到就 None（读不到就不探路，免得不记账反复进出）。"""
        w, d = self._read_week(items)
        return f"{w}-{d}" if (w and d) else None

    def _goal_cfg(self):
        """玩家三条终局目标的配置（strategy.yaml → tuning.goal）。"""
        return self._tuning().get("goal") or {}

    def _can_probe_trip(self, day_key):
        """今天还能不能为「看笔记」去点出行。"""
        if self.trip_probe_disabled or not day_key:
            return False
        trip = self._tuning().get("trip") or {}
        # 工作台上的开关：关掉就回到「训练优先」的老顺序（有训练先训练，出行只在精力不够时）
        if not bool(trip.get("probe_notes_first", True)):
            return False
        # 只有指定的星期才探（默认 6/7 = 周六周日）。
        #   目标 ①「每个周六周日都检查有没有笔记」；目标 ②「周一到周六都训练」——
        #   工作日去探笔记要多走 3~5 步，还有「退不回每日页就耽误一天」的风险，
        #   等于拿训练日去换一次探路，不划算。
        days = trip.get("probe_days")
        if days:
            try:
                allow = [int(x) for x in days]
            except Exception:
                allow = None
            if allow is not None and self.weekday is not None and self.weekday not in allow:
                if self.trip_probe_done != day_key:
                    self.trip_probe_done = day_key      # 记成今天不探，免得每帧重复判断
                    self.log_info(f"星期{self.weekday} 不探笔记（只在 {allow} 探）→ 直接训练")
                return False
        if self.trip_probe_done == day_key:
            return False
        # 点了「出行」但还停在每日页 = 点击没生效（最常见是被 FLUTTERVIEW 盖住）。
        # 不能当成探完，最多再试 3 次；3 次都不行才放弃今天（宁可少探，也别空转）。
        if getattr(self, "trip_probe_retry", 0) >= 3:
            if getattr(self, "_trip_giveup_logged", None) != day_key:
                self._trip_giveup_logged = day_key
                self.log_warning(f"[探笔记] 连续 {getattr(self, 'trip_probe_retry', 0)} 次"
                                 f"点「出行」都没进页面 → 今天放弃探笔记，按原规则训练")
            return False
        return True

    def _want_shop(self, items):
        """今天要不要进商店看一眼。

        商店现在不只买精力药了 —— 特训牛奶 / 金色秘籍 / 器材暴击 / 0 元馈赠都值得买
        （wiki 训练流口径）。但每天都绕一趟要白烧 2~3 步，所以只在两种时候进：
          1) 精力 < 50：缺精力，得看有没有 50 元以下的饮料；
          2) 换周了：商店「上新时间 7天后」，一周才刷一次货，例行进去把该买的买齐。
        按天记账，一天最多进一次，避免每日页 ↔ 商店来回打转。

        **周日一律不进**（玩家 2026-11-23）：周日去公园就能把精力回满，
        这时候再买精力药纯属溢出浪费钱。配置 shop.skip_weekdays: [7] 可改。
        """
        shop_cfg = self._tuning().get("shop") or {}
        if not self.config.get("商店自动购买", True):
            return False
        day = self._day_key(items)
        if self.shop_day is not None and self.shop_day == day:
            return False
        if self.shop_bought >= int(shop_cfg.get("max_buy_per_run", 4)):
            return False

        # 玩家 2026-11-23 12:47：「周日就不要去商店了，周日能逛公园回复，
        # 再去商店买精力药不就溢出了」→ 周日（7）默认不进店。
        week, wd = self._read_week(items)
        skip_days = shop_cfg.get("skip_weekdays", [7])
        if wd is not None and wd in [int(x) for x in skip_days]:
            self.shop_day = day            # 记成今天已看过，免得每帧重复打日志
            self.log_info(f"星期{wd} 不进商店：这天靠公园回精力，再买精力药会溢出")
            return False

        st = self.stamina if self.stamina is not None else 999
        below = int(((shop_cfg.get("rules") or {}).get("stamina") or {}).get("when_below", 50))
        if st < below:
            return True
        week = day.split("-")[0] if day else None
        if week and self.shop_week != week:
            self.log_info(f"新的一周（{week}）→ 例行进店看货（商店 7 天刷新一次）")
            return True
        return False

    def _place_tag(self, n, items, frame):
        """取某个出行地点名下方那行收益标签（精力+/属性+/笔记 …）。"""
        h, w = frame.shape[:2]
        for it in items:
            if abs(it["cx"] - n["cx"]) < w * 0.14 and 0 < it["cy"] - n["cy"] < h * 0.22:
                t = it["t"].replace("＋", "+")
                if any(k in t for k in self.GAIN_KEYS):
                    return t
        return ""

    def _do_trip(self, items, frame):
        """周末出行：笔记 > 训练项 > 未溢出的精力 > 属性 > 零用钱，游戏厅排除。

        精力来源只有事件和公园（玩家 2026-11-23 明确），所以：
          * 精力没满 → 公园里挑能真正吃进精力条的档位；
          * 精力已满 → 补精力不值钱，改挑「属性+」。

        2026-11-23 06:43 玩家实测纠错：精力只差 21 时，脚本挑了「精力+70」，
        而同时存在「精力+30 且属性+」的选项 —— 那 49 点溢出是白扔的。
        现在统一走 `_reward_key`：先把精力按缺口截断，打平再看属性。
        """
        # 真的进到出行页 → 本次探笔记才算数（这才是「探过」的定义，见 _do_daily）。
        if self.trip_probe:
            dk = self._day_key(items)
            if dk and self.trip_probe_done != dk:
                self.trip_probe_done = dk
                self.trip_probe_retry = 0
                self.log_info(f"[探笔记] 已进入出行页（{dk}）→ 本次探笔记计为有效")

        # 探笔记的退回流程：从扭蛋三选一退回出行页后，还要再点一次右上角才回每日页。
        if self.trip_probe_back > 0:
            self.trip_probe_back -= 1
            return self._leave_top_right(items, frame, "[退第2层 出行页→每日页]")

        go = self._find(items, ["前往"])
        if go and self._yellow_ratio(frame, go["b"]) > 0.35:
            self._click_item(go, "[出发确认]")
            return True

        if self.trip_picked and self._click_confirm(items, frame):
            self.trip_picked = False
            return True

        h, w = frame.shape[:2]
        gap = self._gap()

        names = [it for it in items if it["t"] in self.TRIP_NAMES]
        banned = [n for n in names if self._excluded_place(n["t"])]
        names = [n for n in names if not self._excluded_place(n["t"])]
        if banned:
            self.log_info(f"排除地点（规则里不去）：{[b['t'] for b in banned]}")
        if not names:
            self.log_warning(f"出行页没有可用地点（可能误判了页面）：{[i['t'] for i in items][:10]}")
            return self._do_unknown(items, frame)

        # ===== 探路：玩家 2026-11-23「出行看有没有笔记，大于训练」=====
        # 这次是每日页为「看笔记」点进来的：扫一遍地点标签
        #   有笔记 → 不退回，走下面的正常打分（笔记 tier 最高，必然排第一）；
        #   没笔记 → 立刻退出回每日页，改走训练。
        if self.trip_probe:
            self.trip_probe = False
            walk = next((n for n in names if n["t"] == "步行街"), None)
            if walk:
                # 玩家 2026-11-23 21:01 确认：笔记不在出行页的地点标签上，而在
                # 「步行街 → 扭蛋三选一」里出现【笔记 / 继承】。步行街标签原文是
                # 「随机增益+」，_place_tag 的 GAIN_KEYS 没收录 → 一直读成空、误判
                # 「没有笔记」，所以旧逻辑从没真的点进过步行街。
                week, day = self._read_week(items)
                self.stat["note_probe"] = self.stat.get("note_probe", 0) + 1
                self.log_info(f"[探笔记] 出行页 → 点进「步行街」看扭蛋三选一（第{week}周 星期{day}）")
                near = [i["t"] for i in items
                        if abs(i["cx"] - walk["cx"]) < 270 and 0 < i["cy"] - walk["cy"] < 240]
                self._audit_save(week, day, "NOTE_WALK", frame,
                                 f"[探笔记] 出行页 → 点进步行街\n"
                                 f"OCR：{[i['t'] for i in items]}\n"
                                 f"步行街附近文字：{near}\n")
                self.trip_probe_back = 2
                self._click_item(walk, "[探笔记-进步行街]")
                return True
            self.log_warning("[探笔记] 出行页没找到「步行街」→ 右上角退回每日页走训练")
            self.trip_probe_back = 1
            return self._leave_top_right(items, frame, "[无步行街-退回]")
        elif self.trip_back_tries > 0 and self._day_key(items) == self.trip_probe_done:
            # 退回失败只放弃今天，不再永久禁用：以前 trip_probe_disabled=True
            # 会让之后所有天（甚至断点恢复后的整局）都不再探笔记。
            self.trip_back_tries = 0
            self.trip_probe_back = 0
            self.log_warning("[探笔记] 退回没成功 → 今天放弃探笔记，按原规则出行")

        best, best_val, best_key = None, -1, None
        for n in names:
            tag = self._place_tag(n, items, frame)
            text = n["t"] + " " + tag
            key = self._reward_key(text)
            # 公园是唯一能大额回精力的入口，精力还有缺口时把它的位次抬到
            # 「任何具体精力档之上、技能之下」—— 进去子页再按同一套规则挑档。
            if n["t"] == "公园" and gap > 10 and key[0] < 3:
                key = (2, max(gap, 999), 0, 0)
            val = self._key_value(key)
            self.log_info(f"出行地点「{n['t']}」标签「{tag}」档位{key} → {val:.1f}"
                          f"（精力缺口 {gap}）")
            if val > best_val:
                best, best_val, best_key = n, val, key

        if best is None or best_key is None or best_key[0] == 0:
            # 公园子页（散步/划船/野餐）用单独的顺序：休息日只求把精力补回来
            in_park = all(n["t"] in ("散步", "划船", "野餐", "发呆") for n in names)
            tcfg = self._tuning().get("trip") or {}
            order = ([str(x) for x in (tcfg.get("park_fallback") or self.PARK_FALLBACK)]
                     if in_park else
                     [str(x) for x in (tcfg.get("fallback_order") or self.TRIP_FALLBACK)])
            cand = sorted(names, key=lambda x: order.index(x["t"]) if x["t"] in order else 99)
            best = cand[0]
            self.log_info(f"地点都没读到收益标签，按兜底顺序选「{best['t']}」")
        self._click_item(best, f"[出行-档位{best_key}]")
        self.trip_picked = True
        return True

    def _do_go(self, items, frame):
        """「前往XX」列表：每行旁边标着 精力+X / 属性+X，按同一套优先级挑。

        踩坑：以前这里的处理是「点第一个前往」，于是经常进 +20 精力那档，
        明明右边有 +70 的。公园是主要回精力途径，必须按数值选；
        但数值还要按缺口截断（见 `_reward_key`），否则精力快满时会白扔。
        """
        gos = [it for it in items if it["t"].startswith("前往")]
        if not gos:
            self.click_relative(0.5, 0.5)
            return True
        if len(gos) == 1:
            self._click_item(gos[0], "[前往-唯一]")
            return True

        gap = self._gap()
        best, best_val, best_key = None, -1, None
        for g in gos:
            text = g["t"] + " " + self._near_text(items, g, frame)
            key = self._reward_key(text)
            if "公园" in g["t"] and gap > 10 and key[0] < 3:
                key = (2, max(gap, 999), 0, 0)
            val = self._key_value(key)
            self.log_info(f"「{g['t']}」附近「{text[len(g['t']):].strip()}」"
                          f"档位{key} → {val:.1f}（精力缺口 {gap}）")
            if val > best_val:
                best, best_val, best_key = g, val, key
        self._click_item(best or gos[0], f"[前往-档位{best_key}]")
        return True

    # ---------- 商店 ----------
    # 商品分类：名称关键词 → 类别。类别口径来自 wiki（篮球少女：飞跃巅峰 · 训练流）
    #   值得买：精力饮料 / 特训牛奶（降训练精力消耗）/ 器材暴击（有负面，且只有器材上
    #          站人才买）/ 金色教科书 / 金色属性秘籍 / 表情提升护符 / 老板的馈赠（0 元）
    #   不买：紫色白色杂项、效率等级书（性价比极低）、超过 50 元的回精力道具
    SHOP_CATS = (
        ("free",    ("馈赠", "红包", "免费")),
        ("milk",    ("牛奶", "特训")),
        ("stamina", ("饮料", "精力", "能量饮")),
        ("crit",    ("暴击",)),
        ("book",    ("教科书", "教材", "等级书")),
        ("stat",    ("秘籍", "属性书", "指南")),
        ("charm",   ("护符", "表情")),
    )
    # 不是商品名的词：数量、上新、按钮、tab、价格单位
    SHOP_NON_ITEM = ("剩余", "上新", "天后", "购买", "关闭", "使用", "比赛",
                     "商店", "能力详情", "元", "零用钱")

    def _shop_kind(self, name):
        """商品名 → 类别。认不出来返回 other（=不买）。"""
        n = (name or "").replace("＋", "+").strip()
        if "精力上限" in n:          # 「精力上限+」是上限书，不是回精力，别当药买
            return "stat"
        for cat, words in self.SHOP_CATS:
            if any(w in n for w in words):
                return cat
        return "other"

    def _shop_price(self, items, item):
        """商品卡上的价格。

        【2026-11-23 12:12 实机 1920x1080 截图逐字量的坐标】
            道具名 y=298 → 「剩余：1」y=474 → 价格 y=517（下一行分别是 584/763/805）
        也就是说价格在「剩余」下面 **约 43px**，名字下面 219px（≈5.5 个字高）。
        之前按「名字下方 3.2h」找，所以真价格一律超范围 → 全部 None，
        价格上限形同虚设；偶尔还会把库存那个「1」当价格。
        现在改成以「剩余」这一行作锚点，价格必须在它下面 5~95px；
        没有锚点才退回按名字的高度比例找。
        """
        h = item["b"].height
        stocks = [it["cy"] for it in items
                  if ("剩余" in (it["t"] or "") or "剩余" in (it["t"] or "").replace("：", ":"))
                  and abs(it["cx"] - item["cx"]) < 90]
        # 必须取**名字下方**最近的那条「剩余」：第二行卡片的正上方 110px 也有第一行的
        # 「剩余」，按绝对距离取会取到上一行，于是把上一行的价格算到这一行头上
        # （实测「特调专属饮料+」100 被读成上面那件的 130）。
        below = [sy for sy in stocks if sy > item["cy"] - 20]
        anchor = min(below) if below else None
        best, best_d = None, 1e9
        for it in items:
            t = (it["t"] or "").replace(" ", "")
            m = re.search(r"(\d{1,4})\s*元", t)
            if m:
                val = int(m.group(1))
            else:
                # 价格前面常带一个硬币图标，OCR 会读成「?50」「¥50」这种前缀
                m2 = re.fullmatch(r"\D{0,2}(\d{1,4})", t)
                if not m2 or "剩余" in t:
                    continue
                val = int(m2.group(1))
            if abs(it["cx"] - item["cx"]) > 90:
                continue
            if anchor is not None:
                dy_stock = it["cy"] - anchor
                if not (5 < dy_stock < 95):
                    continue
            else:
                dy = it["cy"] - item["cy"]
                if dy < -0.8 * h or dy > 6.5 * h:
                    continue
            d = abs(it["cy"] - item["cy"]) + abs(it["cx"] - item["cx"]) * 0.5
            if d < best_d:
                best, best_d = val, d
        return best

    def _shop_offers(self, items, frame):
        """把商店货架 OCR 聚合成 [{name, kind, price, item}]。

        页面事实（wiki 截图 OCR 校对）：
          每行 3 张卡，卡内是「道具名（可带 + / ++ 强化后缀）→ 价格数字 → 剩余:N」，
          卡旁是效果说明（「零用钱+80」「两个推荐属性随机+25~35」），
          顶部「上新时间 7天后」，右上角 X，底部 tab「比赛 / 商店 / 能力详情」。
        """
        offers = []
        for it in items:
            t = (it["t"] or "").strip()
            if not t or len(t) < 2:
                continue
            if any(k in t for k in self.SHOP_NON_ITEM):
                continue
            if re.fullmatch(r"[\d\s\.\+\-%]+", t):   # 纯数字 = 价格，不是商品
                continue
            kind = self._shop_kind(t)
            if kind == "other":
                continue
            offers.append({"name": t, "kind": kind,
                           "price": self._shop_price(items, it), "item": it})
        # 同一个商品名 OCR 可能重复给出（卡片名 + 效果区），按 (名字, 价格) 去重
        seen, uniq = set(), []
        for o in offers:
            k = (o["name"], o["price"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(o)
        return uniq

    def _shop_plan(self, offers, cfg):
        """按白名单挑一件要买的，返回 (offer, 原因) 或 (None, 不买的原因)。

        优先级 order：0 元馈赠 > 特训牛奶 > 精力饮料 > 器材暴击 > 教科书 > 属性秘籍 > 护符
        每条规则都能单独关（on: false）、单独设价格上限（max_price）、
        精力饮料还要求「精力 < when_below」，器材暴击还要求「上一场器材上有人」。
        """
        rules = cfg.get("rules") or {}
        order = cfg.get("order") or ["free", "milk", "stamina", "crit", "book", "stat", "charm"]
        st = self.stamina
        eq = getattr(self, "last_equip_count", 0) or 0

        # 玩家 2026-11-23 16:25：优先留 120 块给笔记继承扭蛋（egg.max_price=120），
        # 否则笔记刷出来买不起。金币读得到时，任何有价格的商品买了会跌破底线就不买。
        # 0 元馈赠不花钱，永远放行；读不到金币时不敢乱花，跳过付费项。
        keep = int(cfg.get("keep_gold", self.GOLD_KEEP))
        gold = getattr(self, "gold", None)

        # 紧急档：精力见底时先买饮料保命（否则今天直接练不动，牛奶再划算也没用）。
        # 精力还宽裕时才按 order 走，牛奶（永久降低训练精力消耗）排在饮料前面。
        urgent_rule = rules.get("stamina") or {}
        urgent = int(urgent_rule.get("urgent_below", 35))
        if st is not None and st < urgent:
            pool = [o for o in offers if o["kind"] == "stamina"]
            cap = urgent_rule.get("max_price")
            pool = [o for o in pool if cap is None or o["price"] is None or o["price"] <= float(cap)]
            if pool:
                pool.sort(key=lambda o: (o["price"] is None, o["price"] or 0))
                self.log_info(f"精力 {st} < {urgent}（见底）→ 优先买精力饮料")
                return pool[0], "stamina-urgent"

        for cat in order:
            rule = rules.get(cat) or {}
            # ⚠️ YAML 1.1 把裸键 `on` 解析成布尔 True（`on: false` 变成 {True: False}），
            # `rule.get("on")` 永远读不到 → 之前的 on:false 全是摆设。改用 enabled，
            # 同时兼容历史文件里被解析成 True 键的写法。
            enabled = rule.get("enabled", rule.get(True, rule.get("on", True)))
            if enabled is False:
                continue
            pool = [o for o in offers if o["kind"] == cat]
            if not pool:
                continue
            # 便宜的优先；读不到价格的排最后（不敢乱花）
            pool.sort(key=lambda o: (o["price"] is None, o["price"] or 0))

            if cat == "stamina":
                below = int(rule.get("when_below", 50))
                if st is None or st >= below:
                    self.log_info(f"商店有精力饮料，但精力 {st} ≥ {below} → 先不买")
                    continue
            if cat == "crit" and rule.get("need_equip", True) and not eq:
                self.log_info("商店有器材暴击道具，但上一场训练没采到器材 "
                              "（wiki：只有器材上站人才买）→ 跳过")
                continue

            o = pool[0]
            cap = rule.get("max_price")
            if cap is not None and o["price"] is not None and o["price"] > float(cap):
                self.log_warning(f"「{o['name']}」{o['price']} 元 > 上限 {cap} 元 → 不买")
                continue
            # 留 120 给笔记：付费商品买完会跌破底线就不买；0 元白拿放行。
            price = o["price"]
            if price is not None and price > 0:
                if gold is None:
                    self.log_warning(f"「{o['name']}」要 {price} 元，但顶栏金币读不到 "
                                     f"→ 不敢花（要留 {keep} 给笔记）")
                    continue
                if gold - price < keep:
                    self.log_warning(f"「{o['name']}」{price} 元：金币 {gold}-{price}="
                                     f"{gold - price} < 留底 {keep}（笔记继承）→ 不买")
                    continue
            return o, cat
        return None, "没有符合白名单的商品"

    def _do_shop(self, items, frame):
        """商店：严格按 wiki 白名单买，其余一律不碰。

        玩家 2026-11-23：「商店具体的效果你去 wiki 看看，决定可以买哪些、长什么样」。
        查证（篮球少女：飞跃巅峰·训练流攻略，多篇一致）：
          ✅ 精力饮料（回精力，**只买 50 元以下**）
          ✅ 特训牛奶（减少训练精力消耗 → 等于多练一天，本作最划算的一件）
          ✅ 器材暴击道具（带负面效果，且**只有当天器材上有站人时才买**）
          ✅ 金色教科书 / 金色属性秘籍（属性；单项 >1200 后收益减半，不买）
          ✅ 表情提升 5% 护符
          ✅ 老板的馈赠 / 馈赠+（标价 0，零用钱 +80，白拿）
          ❌ 紫色/白色杂项、效率等级书（性价比极低）、高价回精力道具
        """
        cfg = self._tuning().get("shop") or {}
        offers = self._shop_offers(items, frame)
        # 一件都没读出来 = 这页没解析成功，不能据此断言「店里没有精力道具」
        # （否则会把「高风险改练意识」那条规则带偏），保持 None。
        if offers:
            self.shop_has_stamina = any(o["kind"] == "stamina" for o in offers)
        self.log_info(f"商店货架「{[o['name'] for o in offers]}」"
                      f"价格{[o['price'] for o in offers]}｜精力 {self.stamina}")
        if cfg.get("snapshot", True):
            self._shot(frame, "shop")

        if not self.config.get("商店自动购买", True):
            return self._leave_shop(items, frame)

        max_buy = int(cfg.get("max_buy_per_run", 4))
        if self.shop_bought >= max_buy:
            self.log_info(f"本期商店已买 {self.shop_bought} 件（上限 {max_buy}），不再买")
            return self._leave_shop(items, frame)

        o, why = self._shop_plan(offers, cfg)
        if o is None:
            self.log_info(f"商店：{why}")
            return self._leave_shop(items, frame)

        self.shop_bought += 1
        self.stat["buy"].append((why, o["name"], o["price"]))
        self._click_item(o["item"], f"[商店-{why} 「{o['name']}」{o['price']}元]")
        self.sleep(0.9)
        f2 = self._frame()
        if f2 is not None:
            # 点开商品后右侧滑出详情面板，底部是**黄色大按钮**，上面写的是价格数字
            # （不是「购买」两个字），OCR 拿不到关键词，只能按色块定位。
            c = self._yellow_rect_center(f2, (0.55, 0.55, 0.98, 0.95))
            if c:
                self.log_info(f"点击购买按钮 @({int(c[0])},{int(c[1])})")
                self.click(c[0], c[1])
                self.sleep(0.6)
            else:
                self.log_warning("没找到右侧的黄色购买按钮，改按 OCR 确认词找")
                self._click_confirm(self._texts(f2), f2)
        return True

    def _yellow_rect_center(self, frame, roi=(0.55, 0.55, 0.98, 0.95)):
        """在指定区域里找**面积最大的黄色实心矩形**并返回中心（商店的购买按钮）。"""
        h, w = frame.shape[:2]
        x1, y1 = int(w * roi[0]), int(h * roi[1])
        x2, y2 = int(w * roi[2]), int(h * roi[3])
        sub = frame[y1:y2, x1:x2]
        if sub.size == 0:
            return None
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, np.array([18, 110, 170]), np.array([38, 255, 255]))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            if bw < 100 or bh < 34:
                continue
            if not (2.0 < bw / max(bh, 1) < 8.0):
                continue
            if cv2.contourArea(c) < bw * bh * 0.55:
                continue
            if best is None or bw * bh > best[0]:
                best = (bw * bh, (x1 + bx + bw / 2, y1 + by + bh / 2))
        return best[1] if best else None


    def _leave_shop(self, items, frame):
        """退出商店。

        实机截图确认：右上角那颗 X 是**图标**，OCR 读不到「关闭」两个字，
        所以 `_leave` 会退化成按 ESC —— 而 ESC 在商店页没用，于是整轮卡在店里
        连点「已买 4 件」（2026-11-23 12:06 实测卡了 10 步）。
        这里改成：先按文字找，找不到就点右上角 X 的比例坐标 (0.843, 0.155)。
        """
        it = self._find(items, STOP_WORDS, exact=True)
        if it:
            self._click_item(it, "[关闭商店]")
            return True
        h, w = frame.shape[:2]
        self.log_info("商店没有可识别的关闭按钮 → 点右上角 X")
        self.click(int(w * 0.931), int(h * 0.161))
        return True


    def _price_near(self, items, item, frame):
        """道具卡片上/下的「N 元」价格，读不到返回 None。"""
        best, best_d = None, 1e9
        for it in items:
            m = re.search(r"(\d+)\s*元", it["t"].replace(" ", ""))
            if not m:
                continue
            d = abs(it["cy"] - item["cy"]) + abs(it["cx"] - item["cx"]) * 0.3
            if d < best_d:
                best, best_d = int(m.group(1)), d
        return best if best_d < 400 else None

    def _leave_top_right(self, items, frame, why=""):
        """点顶栏最右侧的「返回」箭头（图形按钮，OCR 读不到文字）。

        2026-11-23 21:04 实机验证：出行页、步行街「请选择1个扭蛋」三选一页，
        右上角都有同一个浅蓝底 ↩ 按钮，客户区比例 ≈ (0.95, 0.046)。
        连点两次：扭蛋三选一 → 出行页 → 每日页，且**不消耗当天行动**
        （第3周星期六精力始终 80/130，回来「训练」「出行」按钮都还在）。
        ESC / STOP_WORDS 在这两页都没用，所以不能走 _leave() 的 ESC 分支。
        """
        h, w = (frame.shape[:2] if frame is not None else (1080, 1920))
        base = frame.copy() if frame is not None else None
        self.log_info(f"[探笔记] 点右上角返回箭头 (0.95,0.046) {why}")
        self.click(int(w * 0.95), int(h * 0.046))
        self.sleep(0.8)
        f2 = self._frame()
        if base is not None and f2 is not None and self._frame_diff(base, f2) >= 1.2:
            return True
        # 备用坐标：HUD 最右那颗浅蓝 ↩ 实测在图内 (0.943, 0.065)，0.046 偏高了十几像素
        self.click(int(w * 0.943), int(h * 0.065))
        self.sleep(0.8)
        f3 = self._frame()
        if base is not None and f3 is not None and self._frame_diff(base, f3) >= 1.2:
            self.log_info("[探笔记] 备用坐标点中返回箭头，退出去了")
            return True
        # 2026-11-24 10:01 实跑：「请选择1个扭蛋」是强制弹窗 —— 无 X、无取消，
        # 右上角 HUD 返回（含备用坐标）点下去画面一点不变。这种页退不出去，得让调用方换策略。
        self.log_warning(f"[探笔记] 右上角返回无效（这一屏退不出去）{why}")
        return False

    def _leave(self, items, frame):
        """退出当前页面：先找关闭/取消，再点右上角 X。"""
        it = self._find(items, STOP_WORDS, exact=True)
        if it:
            self._click_item(it, "[关闭]")
            return True
        self.log_info("没有关闭按钮，按 ESC 退出")
        try:
            self.send_key("esc")
            return True
        except Exception as e:
            self.log_warning(f"ESC 失败: {e}")
        self.click_relative(0.955, 0.055)
        return True

    def _do_minigame(self, items, frame):
        """小游戏（拼图/游戏厅一类）：玩家规则里没有要玩，一律放弃退出。"""
        self.minigame_tries += 1
        if self.minigame_tries > 5:
            self.log_error("小游戏连续 5 次没能退出，暂停等人工处理", notify=True)
            return None

        it = self._find(items, ["放弃"], exact=True) or self._find(items, ["放弃"])
        if not it:
            for w in ("退出", "结束", "跳过", "关闭", "返回"):
                it = self._find(items, [w], exact=True)
                if it:
                    break
        if not it:
            self.log_warning(f"小游戏页没有放弃按钮，按 ESC。文字: {[i['t'] for i in items][:12]}")
            try:
                self.send_key("esc")
            except Exception as e:
                self.log_warning(f"ESC 失败: {e}")
            self.sleep(1.0)
            return True

        self.log_warning(f"进入小游戏，点「{it['t']}」放弃（规则里不去游戏厅）")
        self._shot(frame, "minigame")
        self._click_item(it, "[放弃小游戏]")
        self.sleep(1.2)

        f2 = self._frame()
        if f2 is not None:
            its2 = self._texts(f2)
            joined = " ".join(i["t"] for i in its2)
            ok = self._find(its2, ["确定", "确认", "是", "放弃"], exact=True)
            if ok:
                self.log_info(f"放弃确认弹窗，点「{ok['t']}」")
                self._click_item(ok, "[确认放弃]")
                self.sleep(1.0)
            if not any(k in joined for k in ("拼图碎片", "收回碎片", "拼图")):
                self.log_info("已退出小游戏")
                self.minigame_tries = 0
        self.stock_idx = 0
        self.stock_tries = 0
        self.stock_picked = False
        self.route = {}
        return True

    def _match_template(self, frame, name, threshold=0.85):
        """在 frame 里找 assets/templates/<name>，返回相对坐标 (rx, ry)。

        2026-11-24 去重：原来自己写 cv2.matchTemplate + 三档缩放循环，
        框架的 find_one(template=...) 已经做了多尺度/灰度/边缘/掩码，直接用它。
        这里保留一个薄壳，只是把「图片路径 → 相对坐标」这层业务约定包住。
        """
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            tpl = cv2.imread(os.path.join(root, "assets", "misc", name))
            if tpl is None or frame is None:
                return None
            box = self.find_one(template=tpl, threshold=threshold, frame=frame)
            if box is None:
                return None
            h, w = frame.shape[:2]
            self.log_info(f"模板 {name} 命中 score={box.confidence:.2f}")
            return (box.x + box.width / 2.0) / w, (box.y + box.height / 2.0) / h
        except Exception as e:
            self.log_warning(f"模板匹配失败 {name}: {e}")
            return None

    def _do_notice(self, items, frame):
        """游戏内公告页：点右上角 X 退出（模板优先，认不到才用固定右上角）。"""
        it = self._find(items, ["关闭", "返回", "退出"], exact=True)
        if it:
            self.log_info(f"公告页：点「{it['t']}」")
            self._click_item(it, "[公告 关闭]")
            self.sleep(1.0)
            return True
        pos = self._match_template(frame, "x_close.png")
        if pos:
            self.log_info(f"公告页：模板 × @({pos[0]:.3f},{pos[1]:.3f})")
            self.click_relative(pos[0], pos[1])
        else:
            self.log_warning("公告页：没匹配到 ×，点右上角固定位置")
            self.click_relative(0.939, 0.115)
        self.sleep(1.2)
        return True

    def _do_confirm_team(self, items, frame):
        """确认编队页：点「开始训练」真正开训。

        玩家 2026-11-23：「我把人员配好了，你直接点开始训练就行」。
        这页只做确认，不改人、不动笔记/器材（配人是玩家自己的事）。
        """
        it = self._find(items, ["开始训练"], exact=True) or self._find(items, ["开始训练"])
        if it:
            spot = self._yellow_btn_center(frame, it)
            if spot:
                self.log_info(f"点击「开始训练」@({int(spot[0])},{int(spot[1])}) [按黄色按钮定位]")
                self.click(spot[0], spot[1])
            else:
                self._click_item(it, "[确认编队-开始训练]")
            return True
        self.log_warning("确认编队页没读到「开始训练」，点右下角大黄按钮")
        self.click_relative(0.808, 0.848)
        return True

    def _do_train_offer(self, items, frame):
        """剧情里「要不要再练一组」的提议：选「那就开始吧」。

        玩家 2026-11-23 的取向是尽量练（推荐属性 +10 比跳过划算）；
        万一只读到「稍等一下」就点它，别停在这儿。
        """
        for want in ("那就开始吧", "开始吧", "就这样吧"):
            it = self._find(items, [want], exact=True)
            if it:
                self._click_item(it, f"[训练提议-{want}]")
                return True
        it = self._find(items, ["稍等一下", "稍后再说"], exact=True)
        if it:
            self.log_info("提议页没读到「那就开始吧」，选「稍等一下」跳过")
            self._click_item(it, "[训练提议-稍等]")
            return True
        self.log_warning("训练提议页没读到按钮，点下方中间推进")
        self.click_relative(0.5, 0.62)
        return False

    def _do_train_confirm(self, items, frame):
        """训练确认弹窗：优先「继续训练」恢复上一次中断的进度。

        「继续训练」= 接着上次没打完的那一轮（周次、局内精力都在）；
        旁边那个才是新开一轮。玩家 2026-11-23 明确：非正常退出后就点这里恢复。
        """
        it = self._find(items, ["继续训练"], exact=True) or self._find(items, ["继续训练"])
        if not it:
            it = self._find(items, ["开始训练", "确认"], exact=True)
        if not it:
            self.log_warning(f"训练确认页没有「继续训练」，按 ESC。文字：{[i['t'] for i in items][:10]}")
            self.send_key("esc")
            self.sleep(0.8)
            return True
        self.in_training = True
        self.log_info(f"训练确认：点「{it['t']}」（恢复中断）")
        self._click_item(it, "[继续训练]")
        self.sleep(1.5)
        return True

    def _do_stock(self, items, frame):
        """炒股小游戏（剧情支线）：三张股票卡 → 选一张 → 点「确定」。

        实测（2026-11-23 08:15）：这一页右上角有「跳过」但点了没反应，
        必须先选卡（选中后出现黄色角标、底部「确定」由灰变黄）才能推进，
        所以不能落到「小游戏」的放弃分支。

        卡片布局：横排三张，上部是类型标签（稳健型 / 平衡型 / 激进型），
        下面是公司名、说明、价格。点标签下方约 12% 屏高处即卡片中心。

        TODO 角色分支 —— 不同主训人员走不同支线，这张表的偏好以后按
        strategy.yaml -> routes.<角色>.stock 覆盖，字段留好：
            mode         随便选 / 按类型
            prefer_type  稳健型 / 平衡型 / 激进型
            max_steps    硬上限
        现在「随便选」= 轮换着点，不看盈亏。
        """
        cfg = self.route.get("stock") if isinstance(self.route.get("stock"), dict) else None
        cfg = cfg or (self._tuning().get("stock") or {})
        mode = str(cfg.get("mode") or "随便选")

        self.stock_tries += 1
        limit = int(cfg.get("max_steps") or 30)
        if self.stock_tries > limit:
            self.log_error(f"炒股小游戏连续 {limit} 步没走完，暂停等人工处理", notify=True)
            self._shot(frame, "stock_stuck")
            return None

        h, w = frame.shape[:2]
        texts = [i["t"] for i in items]
        joined = " ".join(texts)
        self._shot(frame, f"stock_{self.stock_tries:02d}")

        # 1) 走势 / 结算屏（标题「第N周股票走势」，有涨跌幅和最新价）：
        #    主按钮是「卖出」（黄色），点掉就进入下一段或退出小游戏。
        if "股票走势" in joined or any(k in joined for k in ("涨跌幅", "最新价")):
            self.stock_picked = False
            for w_ in ("卖出", "继续", "下一步", "确定", "确认"):
                it = self._find(items, [w_], exact=True)
                if it:
                    self.log_info(f"炒股：走势结算屏，点「{it['t']}」")
                    self._click_item(it, "[炒股 卖出]")
                    self.sleep(1.2)
                    return True
            self.log_warning(f"炒股走势屏没找到主按钮，点底部中间。文字：{texts[:12]}")
            self.click_relative(0.5, 0.86)
            self.sleep(1.2)
            return True

        # 1.5) 排名 / 总结屏（「炒股队收益排名」，只有底部一个「下一步」）：
        #      这页 OCR 只读出 11 段，选股锚点会误抓左边的名次数字「3」，
        #      连点 15 步不动（08:29 实测）。有「下一步」就直接走。
        nx = self._find(items, ["下一步"], exact=True)
        if nx:
            self.stock_picked = False
            self.log_info("炒股：收益排名/总结屏，点「下一步」")
            self._click_item(nx, "[炒股 下一步]")
            self.sleep(1.2)
            return True

        # 2) 已经选过卡：点「确定」交单
        if self.stock_picked:
            ok = self._find(items, ["确定", "确认"], exact=True)
            if ok:
                self.log_info("炒股：点「确定」交单")
                self._click_item(ok, "[炒股 确定]")
                self.stock_picked = False
                self.sleep(1.2)
                return True
            self.stock_picked = False

        # 2) 选卡：优先用类型标签当锚点，退而求其次用价格数字
        anchors = [i for i in items if i["t"].strip() in ("稳健型", "平衡型", "激进型", "保守型")]
        if not anchors:
            # 价格锚点必须成组（≥2 个不同 x 位置）才认 —— 排名页左边的名次
            # 「1/2/3/4」也是一个数字，单个数字会被误当成卡片连点 15 步（08:29）。
            prices = [i for i in items
                      if re.match(r"^\d{1,4}$", i["t"].strip())
                      and 0.55 < i["cy"] / h < 0.85]
            if len(prices) >= 2 and len({round(p["cx"] / w, 2) for p in prices}) >= 2:
                anchors = prices
        if anchors:
            anchors.sort(key=lambda i: i["cx"])
            want = str(cfg.get("prefer_type") or "").strip()
            pool = [a for a in anchors if a["t"].strip() == want] or anchors
            idx = self.stock_idx % len(pool)
            it = pool[idx]
            self.stock_idx += 1
            rx = it["cx"] / w
            ry = it["cy"] / h + (0.12 if it["t"].strip().endswith("型") else 0.04)
            self.log_info(f"炒股：选第 {idx + 1}/{len(pool)} 张「{it['t']}」（{mode}）")
            self.click_relative(rx, ry)
            self.stock_picked = True
            self.sleep(0.9)
            return True

        # 3) 认不出卡片：先点「确定」，再退到放弃，最后 ESC
        for w_ in ("确定", "放弃", "退出"):
            it = self._find(items, [w_], exact=True)
            if it:
                self.log_warning(f"炒股页没认出卡片，点「{it['t']}」脱身")
                self._click_item(it, "[炒股 脱身]")
                self.sleep(1.0)
                return True
        self.log_warning(f"炒股页完全认不出，按 ESC。文字：{texts[:12]}")
        self.send_key("esc")
        self.sleep(0.8)
        return True

    def _do_story(self, items, frame):
        """剧情/对话/事件：有黄卡片就按卡片打分，否则点「跳过」。"""
        cards = self._event_cards(frame, items)
        if cards:
            return self._do_event(items, frame)
        it = self._find(items, ["跳过"], exact=True) or self._find(items, ["点击屏幕继续"])
        if it:
            self._click_item(it, "[跳过剧情]")
            return True
        self.click_relative(0.5, 0.5)
        return True

    # 两种资源的上限指纹 —— **只作兜底**，不再当作分类的唯一依据。
    # 2026-11-23 玩家指出：局内精力上限会被道具提升（商店就有「精力上限+」），
    # 写死 130 一旦上限涨到 140+ 就认不出；局外专注也会溢出显示 202/150。
    # 现在的分类优先看**页面上下文**（hint），只有拿不到上下文时才退回数字指纹。
    CAPS = (150, 130)      # 局外体力 / 局内精力（兜底反推用）
    FOCUS_CAP = 150
    STAMINA_CAP = 130

    # 页面 → 资源类型。这两个页面各自只会出现一种资源，是比数字更硬的判定：
    #   每日页顶栏 ⚡ = 局内精力（上限可能被道具提升）；
    #   专注恢复弹窗 = 局外专注（账号级体力，上限 150、可溢出）。
    PAGE_RESOURCE = {
        "每日页": "stamina",
        "专注恢复": "focus",
        "体力恢复": "focus",
    }

    # 资源槽位（相对坐标 x1,y1,x2,y2）—— 玩家 2026-11-23 明确要求「位置固定就按位置读」。
    # 实测 1936x1119 截图：局内精力在每日页顶栏左 x389-597,y47-101；
    # 局外专注在顶栏右 x1369-1527,y62-97。用比例适配不同窗口尺寸。
    # 之前整屏扫描所有 a/b，底部属性条「1216/2533」被当精力读出 291/253，这是根因。
    RESOURCE_ROI = {
        "stamina": (0.18, 0.02, 0.33, 0.12),
        "focus":   (0.66, 0.02, 0.84, 0.12),
    }
    RESOURCE_B_MAX = 320   # 道具提升+溢出后的硬上限；属性条 b 都是 1600+，一律挡在外面

    # 顶栏金币（零用钱）：实测 1936x1119 截图 rel(0.567, 0.042)，纯数字 15/155/95/35。
    # 玩家 2026-11-23 16:25 要求：优先留 120 块，笔记继承扭蛋刷出来才买得起（egg.max_price=120）。
    GOLD_ROI = (0.52, 0.01, 0.63, 0.10)
    GOLD_KEEP = 120

    def _ab_kind(self, a, b, hint=None):
        """判断 a/b 是哪种资源。

        优先级：
          1. 页面上下文 hint（最硬）—— 调用方知道当前页只会显示哪一种资源；
          2. 数字指纹兜底 —— b 贴着已知上限（±8）才认，避免把 303/164 这种
             随机数字当体力写进 out_focus（2026-11-23 06:51 踩过）。
        上限会变（精力上限+道具 / 专注溢出），所以指纹不再是唯一依据。
        """
        if hint in ("focus", "stamina"):
            return hint
        # 无上下文时先挡 OCR 噪声：当前值远超上限（>1.5 倍）说明这两个数
        # 不是同一个 a/b（如 303/164、218/271 这类页面上别处的数字串一起）。
        # 正常溢出（202/150 = 1.35 倍）不受影响。
        if a > b * 1.5:
            return None
        if 143 <= b <= 158:
            return "focus"
        if 122 <= b <= 138:
            return "stamina"
        # 上限被提升过：b 比已知上限大一段也认（如精力上限涨到 160/170）。
        # 只认「刚好比 cap 大、且差得不离谱」，避免把任意大数当资源。
        if 158 < b <= self.FOCUS_CAP + 60:
            return "focus"
        if 138 < b <= self.STAMINA_CAP + 60:
            return "stamina"
        return None

    def _parse_ab(self, text):
        """把「38/130」解析成 (38, 130)；OCR 常把斜杠吞掉（37/130 → 「371130」）。

        踩坑（2026-11-23 06:12 OCR 探针实测）：顶栏那个数值读出来是「381130」，
        原来的正则要求必须有「/」，于是精力一直读成 None → 状态机拿 999 兜底
        → 精力 38 也照训不误。改为：先按 a/b 匹配，失败再按已知上限反推。

        ⚠️ 2026-11-23 06:49 玩家指出：**体力允许溢出**，顶栏会显示 202/150。
        原来这里（以及 `_read_stamina`）都有 `a <= b` 的校验，溢出值会被整条丢掉
        —— 日志里「局外专注」一直 None/None 就是这么来的。更糟的是 OCR 吞掉斜杠时
        「202150」会退化成 (20,150)，把 202 读成 20，于是又去补药，越补越溢。
        现在放宽到 `a <= cap * 2`（够容纳溢出，又不至于把乱码当数值）。
        """
        s = (text or "").replace(" ", "").replace("＋", "+")
        # 斜杠还在：用 search 不用 ^$，因为实测 OCR 会在后面粘噪音
        # 「221/150+ü三」（2026-11-23 06:50 探针原样输出），锚定结尾必然匹配失败
        # (?!\d) 防止把 284/1784 截断成 284/178 —— 属性条大数被当成资源上限
        m = re.search(r"(\d{1,3})/(\d{2,3})(?!\d)", s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 <= a <= b * 2:
                return a, b
        if not re.fullmatch(r"\d{3,6}", s):
            return None
        for cap in self.CAPS:
            cs = str(cap)
            if not s.endswith(cs):
                continue
            pre = s[: -len(cs)]
            for cand in (pre, pre[:-1] if len(pre) > 1 else ""):
                if cand.isdigit() and 1 <= len(cand) <= 3:
                    a = int(cand)
                    if 0 <= a <= cap * 2:
                        return a, cap
        return None

    def _read_stamina(self, items, frame, page=None):
        """分别读两种资源 —— 完全不同的东西，绝不能混用。

        资源对照（2026-11-23 玩家两次当面纠正 + MCP 探针实测）：
          * **局内「精力」上限约 130，会被道具提升**：每日页顶栏 ⚡ 38/130。
            训练消耗它，决定「今天训练还是休息」；**只有局内事件(精力+10)
            和周日公园(+30/+70) 能补，药水补不了它**。
          * **局外「专注」上限 150、可溢出**：账号级体力，不够开不了训练，
            弹「专注恢复」时用道具补（不用钻石）。不参与训练/休息决策。

        分类优先级（2026-11-23 玩家指出上限会变，弃用「只靠数字指纹」）：
          1. **页面上下文**：每日页一定是局内精力、专注弹窗一定是局外专注；
          2. 数字指纹（b 贴着上限）兜底；上限疑似被提升时按相邻区间放宽。
        上限优先用本次 OCR 读到的 b（道具提升后能跟上），只有明显是 OCR 抖动
        （比已知上限差 1~6，如 134/136）时才保留旧上限，避免卡住不更新。
        """
        h = frame.shape[0] if frame is not None else 1080
        w = frame.shape[1] if frame is not None else 1920
        hint = self.PAGE_RESOURCE.get(page) if page else None
        for it in items:
            ab = self._parse_ab(it["t"])
            if not ab:
                continue
            a, b = ab
            # 体力可以溢出（202/150 是实测见到的真实状态），所以不能要求 a <= b
            if not (0 <= a <= b * 2):
                continue
            # 硬上限：资源上限最高也就 150+道具，b>320 必是属性条/金币（如 1216/2533）
            if b > self.RESOURCE_B_MAX:
                continue
            # 位置决定类型（比 hint 更硬）：顶栏左槽=精力、右槽=专注。
            # 专注恢复弹窗没有顶栏、文字在画面中央，这种页退回数字指纹判断。
            kind = None
            if page not in ("专注恢复", "体力恢复"):
                cx, cy = it.get("cx", 0) / w, it.get("cy", 0) / h
                for k, (x1, y1, x2, y2) in self.RESOURCE_ROI.items():
                    if x1 <= cx <= x2 and y1 <= cy <= y2:
                        kind = k
                        break
                if kind is None:
                    continue                      # 不在任何资源槽位 → 别处的数字，丢掉
                # 类型与上限对不上（如左槽读到 b=150）说明 OCR 串行了，也丢掉
                lo, hi = ((110, 220) if kind == "stamina" else (140, 320))
                if not (lo <= b <= hi):
                    continue
            else:
                kind = self._ab_kind(a, b, hint=hint)
            if kind not in ("focus", "stamina"):
                continue
            if kind == "focus":
                self.out_focus = a
                self.out_focus_max = self._settle_cap(b, self.out_focus_max,
                                                      self.FOCUS_CAP)
            elif kind == "stamina":
                self.stamina = a
                self.stamina_max = self._settle_cap(b, self.stamina_max,
                                                    self.STAMINA_CAP)

    def _read_gold(self, items, frame):
        """读顶栏金币（零用钱）—— 固定位置，只认 GOLD_ROI 里的纯数字。

        玩家 2026-11-23 16:25：「优先级改一下，优先保证有 120 块钱，
        要不然笔记刷出来买不了」。笔记继承扭蛋上限 120（strategy.yaml egg.max_price），
        商店若把钱花到 120 以下，笔记刷出来就只能眼看着错过。

        实测金币在顶栏 rel(0.567, 0.042)，是纯数字（15/155/95/35），旁边
        rel 0.649 的「-130」是购买飘字，靠 ROI 右边界 0.63 排除。
        不做「按全局找最大纯数字」这种事——那是之前精力读成鬼值的老毛病。
        """
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.GOLD_ROI
        for it in items:
            cx, cy = it.get("cx", 0) / w, it.get("cy", 0) / h
            if not (x1 <= cx <= x2 and y1 <= cy <= y2):
                continue
            t = (it["t"] or "").strip().replace(",", "")
            # 金币是纯数字；带斜杠/中文的是精力或其他槽位，别认
            if re.fullmatch(r"\d{2,6}", t):
                gold = int(t)
                # 金币不会是 3 位数以下的精力上限误读（这里只要位置对就认）
                self.gold = gold
                return gold
        return None

    def _settle_cap(self, read, old, default):
        """决定用哪个上限：优先本次读到的 b，过滤掉 OCR 的小抖动。

        read=134、default=130（差 4）→ 判成抖动，沿用旧值/默认；
        read=160、default=130（差 30）→ 是真提升，采纳 160。
        """
        base = old or default
        if read and abs(read - base) <= 4:
            return base                     # 抖动（±4，覆盖实测的 134/136/127），保留原上限
        if read and read >= base + 5:
            return read                     # 上限真的提升了
        return base

    def _orange_spots(self, frame):
        """右侧训练项列上的黄色小圆标：找橙色像素团的中心。"""
        h, w = frame.shape[:2]
        x1, x2 = int(w * 0.86), int(w * 0.995)
        y1, y2 = int(h * 0.05), int(h * 0.95)
        roi = frame[y1:y2, x1:x2]
        b = roi[:, :, 0].astype(int)
        g = roi[:, :, 1].astype(int)
        r = roi[:, :, 2].astype(int)
        mask = (r > 195) & (g > 90) & (g < 205) & (b < 115)
        ys, xs = np.nonzero(mask)
        if len(ys) == 0:
            return []
        clusters = []
        for y, x in zip(ys.tolist(), xs.tolist()):
            for c in clusters:
                if abs(c["cy"] - y) < 26 and abs(c["cx"] - x) < 40:
                    c["ys"].append(y)
                    c["xs"].append(x)
                    c["cy"] = sum(c["ys"]) / len(c["ys"])
                    c["cx"] = sum(c["xs"]) / len(c["xs"])
                    break
            else:
                clusters.append({"ys": [y], "xs": [x], "cy": y, "cx": x})
        return [(x1 + c["cx"], y1 + c["cy"]) for c in clusters if len(c["ys"]) >= 40]

    def _read_pct(self, items):
        """读「失败率 X%」。

        2026-11-23 离线复检 sweep 截图发现：OCR 有时把「失败率0%」切成
        「失败率0」+「%」两个框，带 % 的正则会漏读 → 先按无 % 的写法兜一层。
        """
        for it in items:
            m = re.search(r"失败率\s*(\d+)", it["t"].replace(" ", ""))
            if m:
                return int(m.group(1))
        for it in items:
            m = re.search(r"^(\d+)\s*%$", it["t"].replace(" ", ""))
            if m:
                return int(m.group(1))
        return None

    SKILLS = ["投射", "弹跳", "防守", "体能", "运控", "意识"]
    # 【2026-11-26 玩家定版】进训练项页时游戏默认选中的永远是「投射」，详情已经展开。
    # 以前靠「深蓝高亮」猜，11-26 10:54 那轮猜成了「体能」，于是把投射当普通项去点开 ——
    # 点默认项 = 确认开训，直接撞剧情、这次训练作废。现在写死，不猜。
    DEFAULT_ITEM = "投射"
    # 被训练的「主力」，他自己不算队友（录制 122 张训练项页帧里 119 张都有她）
    # 只用于识别**粘连词**（「百希雅」=百目鬼+希雅，含主力名且超 2 字 → 判为拼接产物）。
    # 2026-11-23 起不再用它从队友里剔人：主训每局会换，靠 _resident_names 自动判。
    MAIN_CHAR = "希雅"
    # 场上角色的口径（玩家 2026-11-23 定稿）：
    #   「经理也是算作队友的」，而且训练项页根本没有「替补 / 解说」这类标签，
    #   所以**场上站着的都算队友**（含经理、含主训本人），不做任何名字级剔除。
    #   恒在场的角色对每个训练项都是 +1，不影响排名；剔掉反而会因为某帧 OCR
    #   漏读而让那个训练项少算一分。只保留通用护栏（训练项名 / 粘连词 / 加成浮层词）。
    # 出现在球场上但明显不是人名的词：加成浮层条目 + 出行地点名
    MATE_STOP_WORDS = ("加成", "队友", "表情", "倍率", "一致", "额外", "组合", "概率",
                       "上限", "游戏厅", "公园", "餐厅", "步行街", "散步", "野餐", "划船")

    def _badge_blobs(self, frame):
        """全图所有橙色小人角标的圆心 [(x, y), ...]。

        【2026-11-24 13:00 玩家质疑＋实测改造】玩家问「不可能会有一整天都没有角标
        的情况，是不是角标识别不准」——对，是识别不准。旧 `_badge_near` 把搜索框
        绑在 OCR 文本框上（`框右边缘 -40px ~ +190px`），有两个失效点：

          1. OCR 框一旦偏了（日志里见过「文字框偏了 99px」），搜索框跟着偏，
             角标就跑到框外；
          2. 角标本身离胶囊右边缘约 83~87px（1600x900 实测），只要框宽多读进去
             一截（比如把「Lv.1」并进框），搜索框起点右移，角标被切成两半，
             面积/宽高比双重不过 → 判「没有」。

        改成先全图扫一遍橙色小圆、再按纵向就近归属（见 `_badge_near`），
        完全不看 OCR 框的横向位置。575 张真实训练项页帧实测：
        **每帧 1~6 个角标，0 张是「全无」**（1个:3 帧 / 2个:55 / 3个:198 /
        4个:259 / 5个:58 / 6个:2，均值 3.5）。
        也就是说「六项全无角标」在真实对局里不存在 —— 出现即坏帧。
        """
        h, w = frame.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        bb = frame[:, :, 0].astype(int)
        gg = frame[:, :, 1].astype(int)
        rr = frame[:, :, 2].astype(int)
        m = ((rr > 195) & (gg > 105) & (gg < 195) & (bb < 95)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            # 尺寸按 1920x1080 折算（玩家改过窗口大小：1600x900 下角标只有 35px）
            if not (16 * sx <= bw <= 52 * sx and 16 * sy <= bh <= 52 * sy):
                continue
            if not (0.6 < bw / max(bh, 1) < 1.7):
                continue
            if bw * bh < 260 * sx * sy:
                continue
            out.append((bx + bw / 2.0, by + bh / 2.0))
        return out

    def _badge_near(self, frame, row, blobs=None):
        """训练项行对应的橙色小人角标圆心；没有就返回 None。

        单行便捷接口（证据脚本 / CoreProbe 用）。批量判定请用 `_badges_for_rows`，
        它做了一对一分配，避免两行抢同一个角标。
        """
        return self._badges_for_rows(frame, [row], blobs).get(0)

    def _badges_for_rows(self, frame, rows, blobs=None):
        """给每个训练项行分配角标，**一对一**（一个角标只能用一次）。返回 {行下标: 圆心}。

        匹配方式（2026-11-24 改）：只看纵向，不看 OCR 框的横向位置。
          · 横向只要落在训练项条目列（`x > 0.62w`）即可 —— 角标贴在胶囊右上角外侧；
          · 纵向按 `|角标y - 训练项行cy|` 从小到大贪心分配，每个角标、每一行各用一次。

        为什么要一对一：训练项行间距实测约 108px，半距 54px。若阈值宽于半距，相邻两行
        会同时「认领」同一个角标 → 本来没人的那项被判成有人，白采一轮还拉低排名。
        贪心分配把这类抢用直接消掉。

        阈值依据（1600x900 实测）：角标 y 比对应训练项行中心高 43~45px（=0.048h），
        取 0.055h（≈49.5px）刚好覆盖，又明显小于半距 54px。
        """
        if blobs is None:
            blobs = self._badge_blobs(frame)
        out = {}
        if not blobs or not rows:
            return out
        h, w = frame.shape[:2]
        dy_max = 0.055 * h
        pairs = []
        for ri, r in enumerate(rows):
            for bi, (bx, by) in enumerate(blobs):
                if bx < 0.62 * w:
                    continue
                dy = abs(by - r["cy"])
                if dy > dy_max:
                    continue
                pairs.append((dy, ri, bi))
        pairs.sort()
        used_r, used_b = set(), set()
        for dy, ri, bi in pairs:
            if ri in used_r or bi in used_b:
                continue
            used_r.add(ri)
            used_b.add(bi)
            out[ri] = blobs[bi]
        return out

    def _court_names(self, items, frame):
        """球场上站着的角色名（队友 + 主力），名字是 2~5 个字的小标签。

        2026-11-23 实测坐标（1920x1080，训练项页）：球队经理(364,443)、真崎(555,448)、
        百目鬼(684,441)、希雅(888,455)，都落在 y≈0.41 这一行。
        「球队经理」是场边常驻角色，不是队友，必须排除，否则每个训练项都白送 1 分。
        """
        h, w = frame.shape[:2]
        out = []
        for it in items:
            t = it["t"].replace(" ", "")
            if not (0.30 < it["cy"] / h < 0.52 and 0.03 < it["cx"] / w < 0.80):
                continue
            if not self._is_mate_name(t):
                continue
            if t not in out:
                out.append(t)
        return out

    def _is_mate_name(self, t):
        """是不是一个可信的队友名。

        粘连词必须丢掉：OCR 会把相邻两个名字标签合成一个框，实测见过
        「百希雅」（=百目鬼+希雅）、「晞雅鬼」。它们含主力名字希雅又超过 2 个字，
        一律判为拼接产物 —— 否则每帧都能凭空多算一个队友。

        加成浮层也要防：05:29 实测点开训练项时偶尔会弹出左侧那四张加成卡，
        OCR 把「属性加成」「表情一致」当成了站在场上的队友，白送 2 分，
        直接把错的选项顶到第一。所以这类词一律不认。
        """
        if not re.fullmatch(r"[\u4e00-\u9fa5]{2,5}", t):
            return False
        if t in self.SKILLS:
            return False
        if any(w in t for w in self.MATE_STOP_WORDS):
            return False
        # 粘连词：含**任意一个已知主角名**且超过 2 字 → 判为「两个名字粘一起」。
        # 不再只看写死的 MAIN_CHAR：主训每局会换（希雅 / 闪堂 …），
        # 名字池随每轮识别结果自动扩充，见 _remember_main_char。
        if len(t) > 2 and any(n in t for n in self.known_main_chars):
            return False
        return True

    def _decide(self, items, frame):
        """训练项页的「决定」：**不判颜色**，OCR 读不到就用锚点定位按钮。

        实测（22:33，第4周训练项全 MAX）：这时的「决定」是白底按钮
        BGR≈(152,212,211)，被「只有黄色才点」的保护挡掉，结果在训练项页空转 15 步。
        又实测（2026-11-23 05:19）：黄底白字的「决定」OCR 有时整块读不出来，
        exact 匹配失败 → 页面判定和点击一起失灵。

        2026-11-24 11:20 实跑又出现 3 次「没读到决定」，说明这个兜底不是偶发。
        原来兜底是死坐标 (0.926, 0.831)，但「决定」按钮**按训练项换版式、位置不固定**
        （见 anchors.json 里 训练项页_决定_1..4，四套区域的 y 差了近 200px），
        死坐标只能碰运气。现在改成：先问框架「决定按钮的像素在哪」（模板匹配），
        命中就点它自己的中心；锚点也没命中（画面被浮层盖住）才退回死坐标。
        """
        it = self._find(items, ["决定"], exact=True)
        if it:
            self._click_item(it, "[决定-开训]")
            return True
        if self._click_confirm(items, frame):
            return True
        name, box = self._anchor_find("训练项页", frame)
        if box is None:
            # 这一步用的 frame 通常是「点训练项条目之前」那一帧，按钮可能还在动画里
            # （2026-11-24 11:23 实测 3 次读不到「决定」，就是卡在加成飘字的过渡帧）。
            # 等半秒重新取一帧再匹配，比直接退回经验坐标可靠。
            self.sleep(0.5)
            f2 = self._frame()
            if f2 is not None:
                name, box = self._anchor_find("训练项页", f2)
        if box is not None:
            cx, cy = box.x + box.width / 2, box.y + box.height / 2
            self.log_info(f"没读到「决定」文字 → 锚点 {name} 定位 ({int(cx)},{int(cy)})")
            self.click(cx, cy)
            return True
        # 锚点也没命中（画面被浮层挡）：退回经验坐标（1080p：x≈0.926、y≈0.83）
        self.log_warning("没读到「决定」文字、锚点也没命中，按经验坐标点右下角")
        self.click_relative(0.926, 0.831)
        return True

    def _selected_skill(self, frame, rows):
        """从当前帧认出「深蓝高亮」的训练项条目 = 游戏默认选中的那一项。

        实测两帧（2026-11-23 20:37）：
          W1D4 点开弹跳后：选中态整条带 亮度≈124、B-R≈9（深藏青）；
                            其余 亮度≈170~189、B-R≈95~117（浅蓝底）。
          10:13 调试帧：    选中态 亮度≈102、B-R≈24；其余 亮度≈168~179、
                            B-R≈-76~-116（橙底）。
        所以判据用「明显更暗 + B 不远高于 R」，两种皮肤都成立，也不会把黄色
        角标 / 「决定」按钮误当选中态。认不出返回 None，调用方走保守路径。
        """
        try:
            h, w = frame.shape[:2]
            sx, sy = w / 1920.0, h / 1080.0
            best, best_dark = None, None
            for r in rows:
                cx, cy = r["cx"], r["cy"]
                x0, x1 = max(0, int((cx - 180) * sx)), min(w, int((cx + 180) * sx))
                y0, y1 = max(0, int((cy - 30) * sy)), min(h, int((cy + 30) * sy))
                if x1 <= x0 or y1 <= y0:
                    continue
                roi = frame[y0:y1, x0:x1]
                b = float(roi[:, :, 0].mean())
                g = float(roi[:, :, 1].mean())
                rr = float(roi[:, :, 2].mean())
                bright = (b + g + rr) / 3.0
                if bright < 155 and (b - rr) < 60:
                    if best_dark is None or bright < best_dark:
                        best, best_dark = r["t"], bright
            return best
        except Exception:
            return None

    def _do_training_core(self, items, frame):
        """训练项页：点开带角标（=有人站台）的训练项 → 读队友 / 器材 / 失败率 → 打分 → 决定。

        候选池判定（玩家 2026-11-24 12:55）：「角标代表有人，没角标的一定没有人」
        → 没角标的训练项队友恒为 0，不采；默认选中项若在候选里，则**先采且一次都不点**
        （点了会把游戏的默认态变成手动选中，触发二次确认直接开训）。

        每轮都从画面上重新认「主训人员」（每个训练项都在场的名字），不写死名字。
        """
        h, w = frame.shape[:2]
        rows = [it for it in items if it["t"] in self.SKILLS and it["cx"] / w > 0.70]
        rows.sort(key=lambda x: x["cy"])
        if not rows:
            # 【2026-11-26】以前这里直接 _decide（= 点「决定」），等于蒙一项开训。
            # 页面上连六项训练项都读不全，就不该替玩家选。
            if self._read_fail_stop_now(
                    "训练项页", "训练项页没找到任何训练项条目（认页失败/残帧）", frame=frame,
                    hint="页面上连六项训练项都没读全，分不清哪一项是默认的「投射」，不敢替玩家选。"
            ):
                return True
            self.log_warning("训练项页没找到训练项条目，直接决定")
            return self._decide(items, frame)

        if self.item_checked:
            return self._decide(items, frame)
        self.item_checked = True

        # ── 角标扫描 + 坏帧重取 ──
        # **扫不到可用的角标 = 坏帧，不是「今天没人」**。依据（2026-11-24 13:00）：
        # 575 张真实训练项页帧，每帧都有 1~6 个角标（1个:3 / 2个:55 / 3个:198 /
        # 4个:259 / 5个:58 / 6个:2，均值 3.5），**0 张是「全无」**。
        # 实跑第 3 轮那 3 次「六项都没角标」，同时伴随「没认出默认选中态」
        # 「只读到 5 个训练项（缺防守）」「所有候选都没读到训练项详情」—— 典型的刚切页、
        # 胶囊和角标还在淡入的残帧。拿这种帧比较必然白扔一次训练。
        # 所以：角标一个都扫不到，或者扫到了却一个都对不上训练项行（OCR 行纵向偏了），
        # 都先重取帧，最多 3 次；仍不行才降级到按默认项决定。
        def _scan(fr, its):
            bl = self._badge_blobs(fr)
            rws = [it for it in its if it["t"] in self.SKILLS
                   and it["cx"] / fr.shape[1] > 0.70]
            rws.sort(key=lambda x: x["cy"])
            asg = self._badges_for_rows(fr, rws, bl)
            return bl, rws, [(rws[i], spot) for i, spot in sorted(asg.items())]

        blobs, rows, targets = _scan(frame, items)
        for attempt in range(1, 4):
            if targets:
                break
            self.log_warning(f"角标不可用（全图 {len(blobs)} 个 / 训练项区 {len(rows)} 项 / 对上 {len(targets)} 项）"
                             f"→ 疑似坏帧，重取第 {attempt} 次")
            self.sleep(0.5)
            f2 = self._frame()
            if f2 is None:
                continue
            items = self._texts(f2)
            b2, r2, t2 = _scan(f2, items)
            frame, blobs, rows, targets = f2, b2, r2, t2
            if not rows:
                self.log_warning("重取后的帧读不到训练项条目 → 直接按默认项决定")
                return self._decide(items, frame)
        if not targets:
            self.log_warning("连续重取 3 次仍拿不到可用角标 → 按坏帧处理，直接按默认项决定")
            return self._decide(items, frame)
        if attempt:
            self.log_info(f"重取第 {attempt} 次后角标可用：全图 {len(blobs)} 个，对上 {len(targets)} 项")
        self.log_info(f"角标扫描：全图 {len(blobs)} 个橙色小人，训练项区 {len(rows)} 项，有角标 {len(targets)} 项")

        # 【2026-11-26 玩家定版 A】默认项 = 「投射」，写死，不再靠颜色猜。
        # 历史：11-23 用两帧截图凑出「深蓝高亮」判据；11-26 10:54 实跑把它猜成了「体能」，
        # 于是「投射」被当成普通项去点开 —— 点默认项 = 确认开训，撞剧情、整次训练作废。
        # 现在：读的是常量，颜色识别只留作对账日志（猜的和写死的不一致就记一笔，方便
        # 以后发现游戏改了默认项），**绝不拿它做决策**。
        selected = self.DEFAULT_ITEM
        guess = self._selected_skill(frame, rows)
        if guess and guess != selected:
            self.log_info(f"颜色识别猜「{guess}」，但按写死的默认项「{selected}」处理（不采信颜色）")
        self.log_info(f"默认选中「{selected}」（写死，不猜）→ 这一项全程不点击，直接采默认帧")

        cfg = self._tuning().get("core_score", {}) or {}
        audit_week, audit_day = self._read_week(items)   # 证据目录按「第N周星期X」分组
        # 候选池 = **带角标的训练项**。
        # 【2026-11-24 12:55 玩家纠错】「角标代表有人，没角标的一定没有人！」
        #   → 角标 = 这一项有支援单位站台（有人 → 有队友分）；
        #     没角标 = 一定没人 → 队友恒为 0，采它只是白烧几帧 OCR，还可能把误读的名字
        #     当成队友抬分、误导排名。
        # 注：11:xx 那版曾按"角标只是当天加成"把六项全采，是理解错了，已回滚。
        hit = {id(r): s for r, s in targets}
        for r in rows:
            spot = hit.get(id(r))
            if spot:
                self.log_info(f"「{r['t']}」右上角有角标 @({int(spot[0])},{int(spot[1])}) → 有人，采")
            else:
                self.log_info(f"「{r['t']}」没有角标 → 没人，跳过")

        # 器材基线：进页面这一帧先读一次。卡片是**淡入淡出**的（实测同一场训练里
        # 4 个训练项帧分别读到 2/1/0/0 张），而同一场带的器材是固定的 ——
        # 所以拿进页面这一帧当基线，某个训练项的读数帧没卡也不至于白丢器材分。
        base_equips = []
        try:
            base_equips = [card_glow(frame, c) for c in find_cards(frame)]
        except Exception as e:
            self.log_info(f"器材基线检测失败（忽略）：{e}")
        if base_equips:
            self.log_info(f"器材基线（进页面帧）：{len(base_equips)} 张 {base_equips}")

        scored = []
        # ① 默认选中项**最先采，而且一次都不点**。
        #    玩家 2026-11-24 11:55：「进入的时候默认就是投射的，不用点击直接就能读数，
        #    如果这时候要去点击它，会直接变成选中」。也就是说这页一进来就已经展开着默认项的
        #    编队，点击动作本身会把"游戏默认态"改成"手动选中"（会触发二次确认直接开训），
        #    所以这里连"没读到就补点"都不做，采不到只重新取帧（_measure_item 内部处理）。
        #    为什么必须排在最前面：按胶囊的 cy 从上往下逐个点开的话，一旦前面的技能先被点，
        #    画面就变成那个训练项的编队了，轮到这里再"采当前帧"采到的根本不是默认项
        #    —— W2D4「带经理加成 + 器材」的投射掉出候选池，正是这个顺序问题。
        #    前提：默认项必须在候选里（有角标 = 有人）。没角标说明它没人，採出来是 0 分，
        #    不白花这几帧。
        preset_row = None
        if selected:
            preset_row = next((r for r, _s in targets if r["t"] == selected), None)
        if preset_row is None:
            # 【2026-11-26 玩家定版 A】默认项的两种缺席，处理方式完全不同：
            #   ①「投射」这行还在画面上、只是没角标 → 今天没人站台，队友恒 0，跳过它；
            #      此时点其余项是「切换选择」，不是确认开训，安全。
            #   ② 画面上根本读不到「投射」这一行 → 页面残缺/切页残帧，此时**一个都不点**，
            #      因为分不清哪一项才是当前展开的那项，点下去可能正好点在默认项上。
            #      按玩家要求：判不出来就停下来问，不许蒙。
            in_page = any(r["t"] == selected for r in rows)
            if in_page:
                self.log_info(f"默认项「{selected}」今天没角标（没人站台）→ 不采它，只点其余带角标的项")
            else:
                if self._read_fail_stop_now(
                        selected, "训练项页读不到默认项这一行（坏帧/切页残帧）", frame=frame,
                        hint=f"页面上读不到默认项「{selected}」这一行，"
                             f"怕手一点正好点成「确认开训」。"
                ):
                    return True
                self.log_warning(f"读不到默认项「{selected}」→ 不敢点任何项，交回主循环")
                return True
        if preset_row is not None:
            self.log_info(f"默认选中「{selected}」→ 进页面就展开着它的编队，先采它，全程不点击")
            rec = self._measure_item(preset_row, selected, "default", base_equips,
                                     audit_week, audit_day, idx=0, shot=frame)
            if rec is None:
                return True
            scored.append(rec)

        # ② 其余训练项逐个点开（带角标的才值得花一次点击 + 若干帧 OCR）
        rest = [(r, s) for (r, s) in targets if r is not preset_row]
        if rest:
            self.log_info(f"接下来点开 {len(rest)} 个带角标的训练项：[{', '.join(r['t'] for r, _ in rest)}]")
        for idx, (r, _spot) in enumerate(rest, 1):
            rec = self._measure_item(r, r["t"], "click", base_equips,
                                     audit_week, audit_day, idx=idx)
            if rec is None:
                return True
            scored.append(rec)
        # 读数失败已经请求停机（_read_fail_stop_now 会把 read_fail_stop 置 True）：
        # 不要再排名、不要再点决定，直接把控制权交回主循环去 break。
        if self.read_fail_stop:
            return True

        if not scored:
            # 玩家 2026-11-26：一个都没读到就按「默认项」决定 = 蒙，改成停下。
            if self._read_fail_stop_now("本次全部训练项", "所有候选都没读到训练项详情", frame=frame):
                return True
            self.log_warning("所有候选都没读到训练项详情，按当前默认项决定")
            return self._decide(items, frame)

        # 记下这一场器材最多有几张 —— 商店那边「器材暴击道具只有器材上站人才买」
        # 要用它（wiki 原话）。商店页看不到器材，只能靠训练项页这一眼。
        seen_eq = max([len(d.get("equips") or []) for d in scored] or [0])
        if seen_eq:
            self.last_equip_count = seen_eq

        usable = [d for d in scored if d["valid"]]
        if usable:
            if len(usable) < len(scored):
                # 计数已经在 _measure_item 里按「每个训练项一次读数」加过了，
                # 这里只打日志，别再重复加（2026-11-25 修「读数失败恒为 0」时统一了口径）。
                self.log_warning(f"{len(scored) - len(usable)} 个训练项读数失败，已排除在排名外")
            scored = usable
        else:
            # 全部 0 帧：同样是「蒙」，停下等人看画面（玩家 2026-11-26）。
            if self._read_fail_stop_now("本次全部训练项", "每个训练项都是 0 帧有效", frame=frame):
                return True
            self.log_warning("全部训练项读数都失败，按当前默认项决定")
            return self._decide(items, frame)

        # 主训人员：出现在**每个**训练项里的那个名字（玩家：不能写死名字）。
        #   ・只用来「知道这轮主训是谁」并打进日志，**不从队友里剔除** ——
        #     玩家同时要求「经理也算队友」，而恒在场的名字对每个训练项都是 +1，排名不受影响。
        main_char = self._main_char_of_round(scored, cfg)
        if main_char:
            self.main_char_round = main_char
            self._remember_main_char(main_char)
            self.log_info(f"本轮主训人员：{main_char}（每个训练项都在场，按队友计入）")
        else:
            self.log_warning("没认出主训人员（只采到一个训练项 / OCR 漏读），按场上名字直接算队友")

        # ===== 失败率硬门槛（玩家 2026-11-23：手动打从来不失败）=====
        scored = self._apply_fail_limit(scored, cfg)

        mgr_names = set(cfg.get("manager_names") or [])
        mgr_w = float(cfg.get("manager_weight", cfg.get("teammate", 1.0)))
        for d in scored:
            real = set(d["names"])          # 经理 / 主训 / 队友全算（玩家 2026-11-23）
            d["mates"] = len(real)
            mgr_hit = sorted(real & mgr_names)
            mgr_txt = f" 经理{mgr_hit}×{mgr_w}" if mgr_hit else ""
            d["score"] = self._score_item(sorted(real), d["level"], d["pct"], cfg,
                                          d.get("equips"))
            eq = d.get("equips") or []
            self.log_info(f"「{d['row']['t']}」{d['level'] or '?'} 失败率={d['pct']}% "
                          f"队友{d['mates']}{sorted(real)}{mgr_txt} 器材{len(eq)}{eq} "
                          f"→ 得分 {d['score']:.2f}")

        # ===== 意识加权 = 精力泵（玩家 2026-11-23）=====
        # 意识训练**不消耗精力、反而小幅回精力**，而且是所有训练项里成功率最高的。
        # 所以精力见底时它不是「次优解」，而是唯一能让「周一到周六每天都训练」成立的选择。
        # 精力充足时不加权，避免抢掉正常成长。
        self._apply_consciousness_boost(scored, cfg)

        scored.sort(key=lambda d: (-d["score"],
                                   d["pct"] if d["pct"] is not None else 999))
        self.log_info("训练项排名：" + " | ".join(
            f"{d['row']['t']} {d['score']:.2f}分(队友{d['mates']} 器材{len(d.get('equips') or [])}"
            f"/{d['level'] or '?'}) 失败率{d['pct']}%"
            for d in scored))

        # 玩家规则（2026-11-23）：
        #   能练的训练项失败率**全部**高于阈值时，练下去就是白扔精力；
        #   如果商店又确认买不到精力药（精力补不回来），改练「意识」（成功率最高）。
        #   商店本期没进去看过 = 不知道有没有药，这条规则不生效，按原排名走。
        forced = self._force_skill_when_risky(scored, cfg)
        best = forced or scored[0]
        if forced and scored[0] is not forced:
            self.log_info(f"按规则覆盖排名：改练「{forced['row']['t']}」"
                          f"（原第一「{scored[0]['row']['t']}」）")

        # 全员 0 分 = 队友和器材都没读到，这时候的排名没有意义，别装作在"赌"
        if best.get("score", 0) <= 0:
            self.log_warning(f"所有候选得分都是 0（队友/器材没读到），按「{best['row']['t']}」"
                             f"——读数可能不全，这一手不算决策")
        # 选中项带失败率 → 明确打一条「赌」的日志，事后复盘输赢用
        elif best.get("pct"):
            alt = [d for d in scored if d is not best and not d.get("pct")]
            self.stat["gamble"].append((best["row"]["t"], best["pct"]))
            self.log_warning(f"赌一把：「{best['row']['t']}」失败率 {best['pct']}%,"
                             f"期望得分仍排第一（{best['score']:.2f}）"
                             + (f"；稳的备选 {[(d['row']['t'], round(d['score'], 2)) for d in alt]}"
                                if alt else "；今天没有 0% 的稳选项"))

        _sk = best["row"]["t"]
        self.stat["train"][_sk] = self.stat["train"].get(_sk, 0) + 1

        # 判定汇总证据（玩家 16:47）：这一天六项各自的原始读数 + 打分 + 排名 + 最终选择，
        # 单独落一份 DECIDE.txt，人工一眼能核对「为什么选了它」。
        rank_txt = " | ".join(
            f"{d['row']['t']} {d['score']:.2f}分(队友{d['mates']} 器材{len(d.get('equips') or [])}"
            f"/{d['level'] or '?'}) 失败率{d['pct']}%"
            for d in scored)
        detail_lines = []
        for d in scored:
            real = sorted(set(d["names"]))
            detail_lines.append(
                f"  {d['row']['t']}: 队友={real} 器材={d.get('equips')} "
                f"失败率={d['pct']}% 等级={d['level']} → 得分={d['score']:.2f}")
        decide_txt = (
            f"第{audit_week}周 星期{audit_day}  判定时刻 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"精力 {self.stamina}/{self.stamina_max}  金币 {getattr(self, 'gold', None)}\n\n"
            f"【每项原始读数与得分】\n" + "\n".join(detail_lines) + "\n\n"
            f"【训练项排名】\n{rank_txt}\n\n"
            f"【默认选中项】{selected or '未识别'}"
            f"（{'先进页面直接采默认帧、全程未点击' if any(d['row']['t'] == selected for d in scored) else '未采到，未参与排名'}）\n"
            f"【规则覆盖】{'改练 ' + forced['row']['t'] + '（原第一 ' + scored[0]['row']['t'] + '）'
                         if forced and scored[0] is not forced else '无'}\n"
            f"【最终选择】{_sk}  得分 {best['score']:.2f}  失败率 {best.get('pct')}%\n"
            f"【读数是否有效】{'有效' if best.get('score', 0) > 0 else '全员0分/读数可能不全'}\n"
        )
        self._audit_save(audit_week, audit_day, "DECIDE", None, decide_txt)

        # 运行记录：记下这次选课的最终得分与当时的体力，工作台按轮次列出来
        self._runlog_train(_sk, best["score"], best["pct"], best["mates"],
                           best.get("equips"))
        if _sk == selected:
            # 本来就是默认选中项，再点一次会被游戏当成确认开训，跳过直接点「决定」
            self.log_info(f"「{_sk}」本来就是默认选中项 → 不再点它，避免二次确认开训")
        else:
            self._click_item(best["row"], f"[最优训练项 {_sk} 得分{best['score']:.1f}]")
        self.sleep(1.0)
        f3 = self._frame()
        if f3 is None:
            return True
        return self._decide(self._texts(f3), f3)

    def _remember_main_char(self, name):
        """把认出来的主角名补进粘贴词过滤池（去重，最多留 8 个）。"""
        pool = getattr(self, "known_main_chars", None)
        if pool is None:
            self.known_main_chars = [name]
            return
        if name not in pool:
            pool.append(name)
            del pool[:-8]

    def _read_fail_stop_now(self, name, detail, frame=None, hint=None):
        """训练项读数失败 → **停在这里**，不许按默认项蒙着继续。

        玩家 2026-11-26：「为什么读数 0 帧这么多还继续？应该停下来，你找到原因，再看。
        而不是一股脑流过去。」——所以读数失败不再降级为「排除该项 / 按默认项决定」，
        而是截图 + 桌面弹窗 + 退出主循环，等人看一眼画面再决定。

        hint：弹窗里那句「到底什么地方出问题」，不同调用点说法不同
        （读数失败 vs 认页失败 vs 读不到默认项），不传就用读数失败那句。

        返回 True 表示已经请求停机（调用方应直接返回，不要再排名、不要再点）。
        """
        cfg = self._tuning()
        # 任务配置优先（configs/AutoTask.json），其次 strategy.yaml 的 tuning 段，
        # 都没有就按「停」——玩家要的就是别蒙着继续。
        stop = self.config.get("读数失败即暂停", cfg.get("读数失败即暂停", True))
        if not stop:
            self.log_warning(f"「{name}」读数失败：{detail}（配置要求继续，按默认项走）")
            return False
        if frame is not None:
            self._shot(frame, f"read_fail_{name}")
        self.read_fail_stop = True
        self.log_error(f"「{name}」读数失败：{detail} → 已停下，不按默认项猜", notify=True)
        self._alert_popup(
            "篮球少女：训练项读数失败，已暂停",
            (hint or f"点开「{name}」之后没读到训练项详情（队友 / 器材 / 失败率 / 等级）。") + "\n\n"
            f"原因：{detail}\n\n"
            f"脚本不会按默认项蒙着继续（那等于白扔一次训练）。\n"
            f"请看一眼游戏画面：如果卡在经理增益 / 剧情 / 结算页，点掉它再重跑；\n"
            f"如果是「训练项详情」根本没弹（点空 / 点了没选中），也重跑一次。\n\n"
            f"截图：screenshots/auto/read_fail_{name}_*.png")
        return True

    def _measure_item(self, row, name, how, base_equips, audit_week, audit_day,
                      idx=0, shot=None):
        """采一个训练项候选的编队并落一份证据，返回参与排名的记录。

        how:
          "default" —— 页面进来自带选中态（玩家 2026-11-24 11:55：游戏默认就选中「投射」，
                       编队已经摊开，「不用点击直接就能读数」；「如果这时候要去点击它，
                       会直接变成选中」）→ **一次都不点**，连"没读到就补点"也不做，
                       只重新取帧。点了会把默认态变成手动选中并触发二次确认直接开训。
          "click"   —— 其余训练项：点开胶囊再采；没读到就上移 26px 补点一次。

        返回 None 表示读数途中页面已经切走，调用方应放弃本次训练项比较。
        """
        if how == "click":
            self.log_info(f"—— 点开「{name}」（第 {idx} 项）看训练项详情")
            self._click_item(row, "[展开训练项详情]")
            # 选中态会一直保留，起手等 0.4s 就够（由 _sample_item 的 warmup 控制）
            names, pct, level, equips = self._sample_item(row)
        else:
            # 默认帧就在眼前：起手几乎不用等，直接连拍（_sample_item 内部多帧投票）
            names, pct, level, equips = self._sample_item(row, warmup=0.1)
        # 【2026-11-25】画面在读数途中已经切走（开训 / 剧情 / 结算遮罩）：
        # 这时候**绝对不能补点** —— 补的那一下会落在经理增益卡片或事件选项上，
        # 等于替玩家瞎选一次，还让整次训练项比较作废。直接交回主循环处理那一页。
        if getattr(self, "_item_interrupt", None):
            self.log_warning(f"「{name}」读数被「{self._item_interrupt}」打断 → 不补点、不排名，"
                             f"交给主循环（这一天按画面上的实际情况继续）")
            self._item_interrupt = None
            self.stat["sample_fail"] += 1
            return None

        if not names and pct is None and level is None:
            if how == "click":
                # 2026-11-23 实测：「意识」这一档常点了没反应（选中的还是上一个训练项），
                # 因为它贴在最底下、离属性条太近。往上挪 26px 重点一次再读。
                self.log_warning(f"「{name}」没读到训练项详情（可能点偏了），往上挪 26px 重点一次")
                self.click(row["cx"], row["cy"] - 26)
                names, pct, level, equips = self._sample_item(row)
            else:
                # 默认项不能补点（会开训），只能等画面渲完再重采一次
                self.log_warning(f"「{name}」默认帧没读到训练项详情 → 等一拍重采（仍然不点击）")
                names, pct, level, equips = self._sample_item(row, warmup=0.6)

        # 页面守卫：读数途中页面可能已经变了（实测第 6 周点开「体能」时，
        # 画面已经跳到「器材事件」，后面 3 步全在对着事件页点训练项 + 点决定）。
        # 一旦不是训练项页就立刻交回主循环，别把后续训练项也点一遍。
        f_chk = self._frame()
        if f_chk is not None:
            now_page = self._classify(self._texts(f_chk), f_chk)
            if now_page != "训练项页":
                self.log_warning(f"读数途中页面已变成「{now_page}」→ 放弃本次训练项比较，交给主循环")
                return None

        # 三项全空 = 这次读数整个失败了（取帧全 None / 画面还在渲染），
        # 不能当成「0 队友 0 分」参与排名，否则会把本来最好的选项排到最后。
        valid = not (level is None and pct is None and not names)
        if not valid:
            # 2026-11-25：以前这个计数只在「部分失败」那条分支里加，最惨的
            # 「整次 0 帧有效」反而漏计，摘要里永远写「读数失败 0 项」。
            # 现在按「每个训练项一次读数」在这里统一计。
            self.stat["sample_fail"] += 1
            # 2026-11-26（玩家）：0 帧不许蒙着继续 —— 默认直接停下等人看画面。
            self._read_fail_stop_now(
                name, f"6 帧里 0 帧读到训练项详情（方式：{how}）", frame=self._frame())
            return None
        # 默认选中的那一项，证据就用刚进页面时的原始帧（数据也是从这一帧读的）
        f2 = shot if shot is not None else self._frame()
        if f2 is not None and self.config.get("保存截图"):
            self._shot(f2, f"item_{idx}")
        rec = {"row": row, "pct": pct, "mates": 0, "level": level,
               "valid": valid, "names": set(names), "score": 0.0,
               "equips": equips or list(base_equips), "how": how}
        # 读数证据（玩家 16:47 要人工检查）：每个训练项一帧图 + OCR 原文 +
        # 这次读到的队友/器材/失败率/等级；打分在下面算完后会再补写一份汇总。
        f2_items = self._texts(f2) if f2 is not None else []
        audit_txt = (
            f"第{audit_week}周 星期{audit_day}  读数时刻 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"训练项：{name}  有效读数：{valid}  "
            f"读数方式：{'默认选中帧(未点击)' if how == 'default' else '点开胶囊后读数'}\n"
            f"队友({len(names)})：{names}\n"
            f"器材({len(equips or base_equips)})：{equips or base_equips}"
            f"{'' if equips else '（本帧没读到，用进页面基线）'}\n"
            f"失败率：{pct}%   等级：{level}\n"
            f"—— 本帧 OCR 原文 ——\n"
            + "\n".join(f"{it.get('cx'):.0f},{it.get('cy'):.0f}  {it['t']}"
                        for it in (f2_items or []))
        )
        self._audit_save(audit_week, audit_day, f"item{idx}_{name}", f2, audit_txt)
        return rec

    def _main_char_of_round(self, scored, cfg):
        """本轮主训人员是谁 —— 出现在**每个**训练项里的那个名字。

        玩家 2026-11-23 两条要求：
          1.「每轮训练你要知道主训人员是谁，不能写死名字」
             主训每局会换（上一局希雅、这一局闪堂），所以按「每个训练项都在场」自动判。
          2.「经理也是算作队友的」
             所以判出来**只记录、不剔除**；恒在场 = 对每个训练项都 +1，排名不受影响。

        判不出来（只采到一个训练项、或 OCR 漏读）就返回 None，不猜。
        多个候选（主训 + 常驻经理都满场）时取出现帧数最多的那个。
        """
        if len(scored) < 2:
            return None
        ratio = float(cfg.get("resident_ratio", 1.0) or 1.0)
        cnt = {}
        for d in scored:
            for n in d["names"]:
                cnt[n] = cnt.get(n, 0) + 1
        need = max(2, round(len(scored) * ratio))
        cands = {n: c for n, c in cnt.items() if c >= need}
        if not cands:
            return None
        return max(cands.items(), key=lambda kv: (kv[1], len(kv[0])))[0]

    def _force_skill_when_risky(self, scored, cfg):
        """失败率全高于阈值 + 商店没精力道具 → 改练指定训练项（默认「意识」）。"""
        force = cfg.get("force_when_risky") or {}
        if not force.get("enabled", True):
            return None
        thr = int(force.get("fail_over", 30))
        want = str(force.get("skill", "意识"))
        if any(d["pct"] is None for d in scored):
            # 有训练项没读到失败率 → 不能断定「全部高风险」，那条未读到的可能正是安全项
            self.log_info("有训练项没读到失败率，无法断定全部高风险 → 按原排名")
            return None
        known = [d["pct"] for d in scored if d["pct"] is not None]
        if not known or not all(p > thr for p in known):
            return None
        if self.shop_has_stamina is None:
            self.log_info(f"失败率全部 >{thr}%，但本期没进过商店，不知道有没有精力药 → 按原排名")
            return None
        if self.shop_has_stamina:
            self.log_info(f"失败率全部 >{thr}%，但商店有精力道具在卖 → 按原排名")
            return None
        cand = next((d for d in scored if d["row"]["t"] == want), None)
        if not cand:
            self.log_warning(f"失败率全部 >{thr}% 且商店无精力道具，但「{want}」今天没角标 → 按原排名")
            return None
        self.log_info(f"失败率全部 >{thr}% 且商店无精力道具 → 改练「{want}」（成功率最高）")
        return cand

    # 读数帧「已经不在训练项页」的硬指纹（2026-11-25 实跑归纳）。
    #   · 经理增益：点训练项后直接开训 → 三选一，画面背景还挂着「心·技·体」，
    #     会被 is_attr_panel 当成"加成动画"一直等，是 0 帧有效的最大来源；
    #   · 剧情/剧本事件：右上角一定有「跳过」，训练项页没有这个词；
    #   · 结算遮罩：整屏半透明 + 「点击屏幕继续」。
    # 只在「画面里一个训练项条目都没有（hits==0）」时才敢用，避免把
    # 训练项页上真有「跳过」的浮层（比如合宿）误判。
    ITEM_LEFT_HINTS = (
        ("经理增益", ("请选择一种加强训练",)),
        ("结算遮罩", ("点击屏幕继续", "点击继续")),
        ("剧情事件", ("剧本事件", "跳过剧情", "跳过")),
    )

    def _left_item_page(self, txt, hits):
        """读数帧是否已经离开训练项页 → 返回页面名，否则 None。见 ITEM_LEFT_HINTS。"""
        if hits:
            return None
        for page, keys in self.ITEM_LEFT_HINTS:
            if any(k in (txt or "") for k in keys):
                return page
        return None

    def _sample_item(self, row, frames=6, gap=0.18, warmup=0.35, max_tries=18,
                     min_frames=4):
        """点开训练项后等 0.4 秒（选中态出来），再连拍几帧投票成稳定值。

        2026-11-23 10:14 CoreSweepTask 逐帧扫查的结论（玩家提示「不用特定时间窗口」）：
            点完训练项后**选中态会一直保留**，不是一闪而过的浮层：
              · 帧00（+0.34s）偶有缺字，帧01（+0.5s）起「失败率 N%」稳定出现；
              · 球场上的队友名（佐久间 / 希雅）和主训（闪堂）、中间器材卡也一直在。
            所以只需 0.4s 起手 + 3~5 帧确认，不需要长窗口，也不需要死等 1.2s
            （那版会拍到别的东西，实测 6 帧全空）。

        为什么要多帧投票：
            过渡期 OCR 会把相邻两个名字标签粘成一个（「百目鬼」+「希雅」→「百希雅」），
            单帧读数会在 0~3 个队友之间乱跳。凑够 2 帧失败率 + 2 帧名字就提前收工。
        """
        self.sleep(warmup)
        pcts, name_sets, levels = [], [], []
        # 器材：训练项页中部会摆 1~3 张卡（玩家：「中间一个器材（鞋子），不是很明显吗」），
        # 卡面是这次训练带的支援单位，外框金/彩光 = 品质。选中态一直保留，所以每帧都能读。
        equips = []
        used = 0
        tries = 0
        bad_streak = 0
        # 2026-11-25：本次读数是否被「画面已经切走」打断（经理增益 / 剧情 / 结算遮罩）。
        # 记在这里，交给 _measure_item 判断「不要补点、不要排名」。
        self._item_interrupt = None
        while used < frames and tries < max_tries:
            tries += 1
            f = self._frame_retry()
            if f is None:
                self.sleep(gap)
                continue
            its = self._texts(f)
            hits = len({it["t"].replace(" ", "") for it in its
                        if it["t"].replace(" ", "") in self.SKILLS})
            # 浮层盖住右列时 hits 会掉到 0~1，所以「还像训练项页」不能只看训练项名：
            # 训练项名 ≥1 / 有「失败率」/ 有 Lv·MAX / 场上 ≥2 个队友名，任一命中就算。
            # 但也不能放太松，否则切场景的中途帧会把别的页面文字当队友
            # （05:29 出现过把出行页的「游戏厅」当队友）。
            names = self._court_names(its, f)
            has_pct = any("失败率" in it["t"] for it in its)
            has_lv = any(re.match(r"^(LV\.?\s*\d+|MAX)", it["t"].replace(" ", "").upper())
                         for it in its)
            # 反例加固（2026-11-23 10:32 实测）：读数中途点了按钮切到「赛前准备」，
            # 那一帧把阵容页的位置名（分卫/大前/控卫）当成队友、把球队大卡当成器材，
            # 结果唯一一项得分 8 分，直接定案选错训练项。只要出现比赛/结算类的字眼就丢帧。
            _txt = " ".join(it["t"] for it in its)
            off_core = is_attr_panel(_txt) or any(w in _txt
                           for w in ("赛前准备", "跳过比赛", "前往比赛", "MVP", "目标达成",
                                     # 2026-11-23 12:10 实跑发现：点训练项后有时先弹「心·技·体」
                                     # 属性/加成大浮层，把训练项详情整个盖住。这类帧以前会被
                                     # 当成训练项页的错误读数，现在明确丢掉（48% 的读数
                                     # 0 帧有效就是栽在这上面）。
                                     "表情升级"))
            looks_core = (not off_core) and (hits >= 1 or has_pct or has_lv or len(names) >= 2)
            if not looks_core and not off_core:
                # 框架锚点补位（2026-11-24）：点训练项后常弹「倍率加成 / 心·技·体」浮层把
                # 右列训练项名和失败率整片盖住，文字判据全灭 → 这一帧白丢。但训练项页
                # 的「决定」按钮在浮层之下仍在，用框架模板匹配（4 套版式，任一命中即算）
                # 做像素级确认，比"靠文字猜"稳。只在这一页的 4 个锚点上匹配，不做全页遍历。
                if self._anchor_page(f, only="训练项页") == "训练项页":
                    looks_core = True
                    self._vis_bump("anchor_rescue")
            if not looks_core:
                # 【2026-11-25 三轮实跑修正】122 次「读数 0 帧有效」的主因：
                # 点开训练项后游戏常常**根本没给你训练项详情**，而是先弹
                #   ①「请选择一种加强训练」三选一（= 这一下已经把训开掉了）
                #   ②「剧情事件 / 剧本事件」（右上角有「跳过」）
                #   ③「点击屏幕继续」结算遮罩
                # 旧逻辑把这些帧当"加成浮层"一直等到 tries 打满（≈5 秒），
                # 等完还按 26px 偏移补点一次 —— 那一击正好落在经理增益卡片 /
                # 事件选项上，等于替玩家瞎选。所以先认「已经离开训练项页」，
                # 命中就立刻停手，一帧不再点。
                left = self._left_item_page(_txt, hits)
                if left:
                    self._item_interrupt = left
                    self.log_warning(f"画面已切到「{left}」→ 立刻停止读数，不补点，交给主循环"
                                     f"（文字 {[it['t'] for it in its][:10]}）")
                    break
                # 【2026-11-23 13:1x 实跑修正】点开训练项后游戏先播一段「加成展示」动画
                # （画面上只剩「倍率加成」「属性加成」「C」这类字，1~3 秒才消失）。
                # 以前把这类帧也算进 bad_streak，4 帧（≈0.7s）就判「页面切走了」收工，
                # 于是 0 帧有效的占比 20%。现在分两种：
                #   · 加成/倍率/属性面板这类**已知过渡动画** → 耐心等，不算失败；
                #   · 真·切走（出现赛前准备/MVP 等）→ 才累加 bad_streak 早退。
                if is_attr_panel(_txt) or ("倍率加成" in _txt) or (len(its) <= 3 and used == 0):
                    self.sleep(0.35)          # 等动画播完（循环开头的 tries 已经计过数）
                    continue
                bad_streak += 1
                if bad_streak >= 4 and used == 0:
                    self.log_warning(f"连续 4 帧都不像训练项页 → 页面已经切走，停止读数"
                                     f"（画面文字 {[it['t'] for it in its][:8]}）")
                    break
                self.sleep(gap)
                continue
            bad_streak = 0
            used += 1
            p = self._read_pct(its)
            if p is not None and p <= 100:      # 210%、230% 这类是 OCR 把两个数字粘了
                pcts.append(p)
            name_sets.append(set(names))
            lv = self._skill_level(its, row)
            if lv:
                levels.append(lv)
            try:
                cs = find_cards(f)
                equips.append(tuple(card_glow(f, c) for c in cs))
                if used == 1:
                    self.log_info(f"读数帧 {f.shape[1]}x{f.shape[0]} 器材 {len(cs)} "
                                  f"{[(int(c['x']), int(c['y']), card_glow(f, c)) for c in cs]}")
            except Exception as e:      # 器材读失败不能拖累训练项读数
                self.log_info(f"器材检测失败（忽略）：{e}")
            # 【2026-11-23 12:05 实跑修正】原来 `>=2 帧就 break`，一局下来 78% 的训练
            # 只采到 2 帧。配合下面「名字要出现 >=2 帧才算」，等于**两帧都得读到**——
            # 漏读一帧就少一个队友，得分直接从 5.0 掉到 3.0，选出来的课自然比手打差。
            # 现在至少 min_frames 帧才允许收工，多拍 2 帧成本 <0.4s，换来名字读全。
            if used >= min_frames and len(pcts) >= 2 and len(name_sets) >= min_frames:
                break
            self.sleep(gap)

        mates = []
        if name_sets:
            cnt = {}
            for s in name_sets:
                for n in s:
                    cnt[n] = cnt.get(n, 0) + 1
            # 出现 2 帧以上就算：名字标签是淡入的，前 1~2 帧读不到很正常；
            # 粘连词/误读已经由 _is_mate_name 过滤，这里不用太严。
            # 【2026-11-23 10:14 扫查修正】这里**不再按 MAIN_CHAR 剔人**：
            # 主训人员每局都可能换（这一局是「闪堂」，上一局是「希雅」），
            # 写死一个名字必然剔错。谁该忽略交给 _do_training_core 用「每个训练项都有」
            # 的常驻名来判。
            # 门槛随帧数走：6 帧要 4 帧命中，4 帧要 2 帧命中。
            # （原来写死 >=2，帧少时等于"必须每帧都读到"，见上面 12:05 那条）
            need = max(2, int(len(name_sets) * 0.6 + 0.5))
            mates = [n for n, c in cnt.items() if c >= need]

        pct = None
        if pcts:
            cnt = {}
            for p in pcts:
                cnt[p] = cnt.get(p, 0) + 1
            pct = max(cnt.items(), key=lambda kv: (kv[1], -kv[0]))[0]

        level = None
        if levels:
            cnt = {}
            for v in levels:
                cnt[v] = cnt.get(v, 0) + 1
            level = max(cnt.items(), key=lambda kv: kv[1])[0]
        equip = ()
        if equips:
            # 【2026-11-23 12:10】器材是**一直画在场上**的，读到 0 张是检测漏了，
            # 不是"没有"。原来看票数取多数 → 3 帧 0 张 + 2 帧 1 张就判成"没器材"，
            # 一场少 0.5~1.5 分。现在只要有非 0 帧就以非 0 帧为准。
            nz = [e for e in equips if e]
            cnt = {}
            for e in (nz or equips):
                cnt[e] = cnt.get(e, 0) + 1
            equip = max(cnt.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        if used == 0:
            # 一帧都没通过「像训练项页」的判据 —— 把画面文字和截图留下来，
            # 不然只能猜（实跑里 48% 的读数是这种情况）。
            try:
                self.log_warning(f"读数 0 帧有效，画面文字：{[it['t'] for it in its][:14]}")
                self._shot(f, "core_fail")
            except Exception:
                pass
        if used < frames:
            self.log_info(f"训练项读数 {used}/{frames} 帧有效（试了 {tries} 次，废帧已丢弃）")
        return mates, pct, level, list(equip)

    def _skill_level(self, items, row):
        """训练项条目右边那个 Lv.1 / Lv.2 / MAX。"""
        best, best_d = None, 1e9
        for it in items:
            t = it["t"].replace(" ", "").upper()
            if not re.match(r"^(LV\.?\s*\d+|MAX)", t):
                continue
            d = abs(it["cy"] - row["cy"]) + max(0.0, it["cx"] - row["cx"]) * 0.15
            if d < best_d:
                best, best_d = t, d
        return best

    def _apply_fail_limit(self, scored, cfg):
        """失败率上限：>limit 的踢出排名，≤limit 的**允许赌**。

        口径演进（玩家 2026-11-23 三次）：
          ① 早先：只做乘法折扣 → 可能选到 40%/50% 的项；
          ② 后来：`fail_limit: 0` 硬性只选 0% → 太保守，把「收益明显更高、
             失败率 20%」的好课全排掉了；
          ③ 2026-11-23 中午：「失败可以赌一把，收益如果很高，小于 30 都能去」
             → `fail_limit: 30`。30 以内不排除，交给期望收益排序。
          ④ 2026-11-24 曾按「周一到周六都训练而且不失败」改成 `fail_limit: 0`；
          ⑤ **玩家当天撤回：「不失败只是理想状态，不是严格禁止」→ 还原 `fail_limit: 30`**
             （strict_fallback 回到 old）。0% 的课只是期望值更高，不是硬门槛；
             高收益的 20~30% 课照样赌，这才是人工手打的选法。
        全部训练项都 >limit 时退回原排名 —— 否则当天就彻底没得练了。
        读不到失败率（pct=None）仍视为不安全（不能假定它 0%）。
        """
        lim = cfg.get("fail_limit")
        if lim is None or not scored:
            return scored
        safe = [d for d in scored if d["pct"] is not None and float(d["pct"]) <= float(lim)]
        if not safe:
            # fail_limit=0 + strict_fallback=min_fail（2026-11-24 终局目标 ②「不失败」）：
            #   一个 0% 的都没有时，不再「退回原排名随便赌」，改成只留**失败率最低**的那档。
            #   老行为（strict_fallback: old）才退回原排名。
            if float(lim) <= 0 and str(cfg.get("strict_fallback") or "min_fail") == "min_fail":
                known = [d for d in scored if d["pct"] is not None]
                if known:
                    low = min(float(d["pct"]) for d in known)
                    keep = [d for d in known if float(d["pct"]) == low]
                    self.log_warning(
                        f"今天没有 0% 的稳课（最低失败率 {low:g}%）→ 只保留最低风险项 "
                        f"{[(d['row']['t'], d['pct']) for d in keep]}，不碰更高的")
                    return keep
            self.log_warning(f"所有训练项失败率都 >{lim}%，只能按原排名（今天没得选，硬着头皮练）")
            return scored
        if len(safe) < len(scored):
            drop = [(d["row"]["t"], d["pct"]) for d in scored if d not in safe]
            self.log_warning(f"失败率 >{lim}% 不赌，排除 → {drop}")
        willing = [(d["row"]["t"], d["pct"]) for d in safe if d["pct"]]
        if willing and cfg.get("log_gamble", True):
            self.log_info(f"阈值 {lim}% 以内允许赌 → 可下注的项 {willing}"
                          f"（按 收益×(1-失败率) 排，谁期望高选谁）")
        return safe

    def _apply_consciousness_boost(self, scored, cfg):
        """精力低 → 给「意识」加权。

        玩家 2026-11-23：**意识训练不消耗精力，反而会少量增加精力**，成功率也最高。
        所以它是「低精力也能继续训练」的钥匙 —— 周一到周六全训练靠的就是它。
        精力 ≥ stamina_below 时不加权，免得白白占掉成长位。
        """
        cons = cfg.get("consciousness") or {}
        if not cons.get("enabled", True) or not scored:
            return
        st_now = self.stamina if self.stamina is not None else 999
        low = float(cons.get("stamina_below", 30))
        if st_now >= low:
            return
        target = str(cons.get("skill") or "意识")
        hit = [d for d in scored if d["row"]["t"] == target]
        if not hit:
            self.log_info(f"精力 {st_now} < {low}，但「{target}」今天没有角标，按原排名")
            return
        boost = float(cons.get("boost", 2))
        for d in hit:
            d["score"] += boost
        self.log_info(f"精力 {st_now} < {low} → 「{target}」加权 +{boost:g}"
                      f"（不耗精力、小幅回精力、成功率最高）")

    def _score_item(self, mates, level, pct, cfg, equips=None):
        """训练项打分 = 出场队友数，再按失败率打折。

        2026-11-23 玩家口径（当次确定）：
          - **训练项等级不计分**（Lv.1/Lv.2/MAX 三档全部当 0），只是照旧读出来打在日志里，
            方便人工复核；配置里 level_bonus 三项已置 0。
          - 失败率 = 乘法折扣：分数 ×(1 - 失败率/100 × fail_weight)，fail_weight=1.0
            意味着 30% 失败率直接打七折、50% 打五折 —— 「失败 = 白扔一次精力」按比例扣。
          - 失败率**全部**高于 30% 且商店无精力道具时，不走这套打分，改由
            _force_skill_when_risky() 强制练「意识」。

        【2026-11-23 第二次纠正】「这页没有器材卡」是错的（玩家当场指出
        「中间一个器材（鞋子），不是很明显吗」）。实机 70 帧复核：中场确实摆着
        1~3 张支援卡，选之前就在画面上（选中态一直保留），外框金/彩光可判。
        检测用 src/core/cards.py 的 find_cards + card_glow。

        现在参与计分的信号有四个：
          1. 场上出场队友数（含经理、含主训本人）  ← 每个 +teammate 分
          2. 器材卡数量 × 光效档位（金 1.0 / 彩 1.5 / 普通 0.5）
          3. 失败率（失败 = 白扔体力）→ 整包乘法打折
          4. 训练项等级 Lv.1/Lv.2/MAX ← 只记录，**不计分**（玩家口径）
        """
        # 队友计分：普通队友 teammate（默认 1.0），球队经理单独 manager_weight（默认 1.5）。
        # 玩家 2026-11-23 22:14：「经理的权重调为1.5」。经理场边常驻、每个训练项都在场，
        # 名字命中 manager_names 就按这个权重算；老配置没配经理名时退化为「全员同权」，行为不变。
        teammate_w = float(cfg.get("teammate", 1.0))
        mgr_w = float(cfg.get("manager_weight", teammate_w))
        mgr_names = set(cfg.get("manager_names") or [])
        base = sum(mgr_w if m in mgr_names else teammate_w for m in mates)

        # 器材：训练项页中场摆着的 1~3 张支援卡，按外框光效计分。
        #   玩家原口径：「队友 +1、器材 0.5、金光 1、彩光 1.5」
        #   （2026-11-23 早先误判成「器材读不到」，实机截图证明选之前就画在场上，
        #     检测用 src/core/cards.py，离线 70 帧稳定：单张卡 x≈620、框色判定正常。）
        if equips:
            tbl = cfg.get("equipment_tier") or {}
            for g in equips:
                base += float(tbl.get(g, cfg.get("equipment", 0.5)))

        lv_table = cfg.get("level_bonus") or {}
        key = "MAX" if (level or "").upper().startswith("MAX") else ""
        if not key and level:
            m = re.search(r"(\d+)", level)
            key = f"Lv.{m.group(1)}" if m else ""
        base += float(lv_table.get(key, 0.0))

        if pct:
            keep = max(0.0, 1.0 - float(pct) / 100.0 * float(cfg.get("fail_weight", 1.0)))
            base *= keep
        return base

    def _tuning(self):
        """读 config/strategy.yaml 的 tuning 段（只读一次）。"""
        if getattr(self, "_tuning_cache", None) is not None:
            return self._tuning_cache
        try:
            import yaml
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            with open(os.path.join(root, "config", "strategy.yaml"), "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._tuning_cache = data.get("tuning", {}) or {}
        except Exception as e:
            self.log_warning(f"读 strategy.yaml 失败，用内置默认值：{e}")
            self._tuning_cache = {}
        return self._tuning_cache

    def _cfg(self, *names, default=None):
        """按顺序取第一个有值的配置项（用于配置项改名后的兼容）。"""
        for n in names:
            try:
                v = self.config.get(n)
            except Exception:
                v = None
            if v is not None and v != "":
                return v
        return default

    def _stamina_state(self):
        """重新取一帧，返回 (局外专注当前值, 上限, 弹窗是否还开着)。"""
        f = self._frame()
        if f is None:
            return None, None, None
        its = self._texts(f)
        focus = cap = None
        for it in its:
            ab = self._parse_ab(it["t"])
            # 这是专注恢复弹窗，上下文必然是局外专注 —— 直接给 hint，
            # 不再靠数字指纹（上限会变/溢出）。
            if ab and self._ab_kind(*ab, hint="focus") == "focus":
                focus, cap = ab[0], (ab[1] or self.FOCUS_CAP)
        opened = any(it["t"].strip() in ("使用", "使用道具", "专注恢复") for it in its)
        return focus, cap, opened

    def _do_focus(self, items, frame):
        """「专注恢复」弹窗 = 补**局外体力**（上限 150，允许溢出显示成 202/150）。

        资源模型（玩家 2026-11-23 当面纠正 + 06:49 补充的背景）：
          * 局外体力（上限 150）：账号级门票，不够就开不了训练；
            唯一补给方式是药水（小瓶 +30 / 大瓶 +60）。**绝不用钻石。**
          * **一次训练固定消耗 30** —— 所以完全不需要补满，够 30 就行。
          * 局内精力（⚡ a/130）：训练消耗它，恢复只靠局内事件「精力+X」
            和周末「公园」(+30/+70)。**药水补不了它**。

        页面事实（06:01 MCP 探针逐帧确认）：
          列表页三行 —— 钻石购买 专注+60（按钮文字是价格「50」，不是「使用」）
                       专注能量饮料 专注+30（拥有 22）
                       专注能量饮料·大瓶装 专注+60（拥有 28）
          点某行「使用」只是打开详情，详情页底部「使用道具」才真正生效。

        安全阀：读不到 150 上限的数字（说明不是体力弹窗）就直接关，不碰药水。

        ⚠️ 溢出问题的来龙去脉（玩家 2026-11-23 06:43 / 06:49 两次指出）：
          旧版不看剩余空间，永远点最下面那行（大瓶 +60），早期还按「点了几次」计数。
          2026-11-23 06:49 玩家看到顶栏体力是 **202/150**（已溢出），
          而日志里体力一直显示 None/None —— 真因是 `_parse_ab` / `_read_stamina`
          里的 `a <= b` 校验把溢出值整个丢掉了，OCR 吞掉斜杠时还会把 202 读成 20，
          于是以为"不够"又去补，越补越溢。

          现在两条铁律：
            1. 体力 ≥ 30（够一次训练）→ 一滴都不补，连弹窗都直接关；
            2. 要补只喝**小瓶 +30**（刚好一次），小瓶没库存才退大瓶。
        """
        mode = str(self._cfg("专注恢复", "体力恢复", default="用道具"))
        s_cfg = self._tuning().get("stamina") or {}
        allow_item = bool(s_cfg.get("use_item", True))     # 玩家要的「是否允许使用体力药水」
        limit = int(s_cfg.get("max_per_round")
                    or self._cfg("每轮最多补专注次数", "每轮最多补体力次数", default=1))
        close_xy = (0.927, 0.135)          # 右上角 X
        use_item = self._find(items, ["使用道具"], exact=True)

        def close(reason):
            self.log_warning(f"专注恢复：{reason}，关闭弹窗")
            self.click_relative(*close_xy)
            self.stamina_refills = 0        # 这次补药结束，下次弹窗重新计瓶数

        # ---- 玩家开关：不允许喝药就直接关弹窗（工作台里就是这颗开关）----
        if not allow_item:
            close("配置 stamina.use_item=false，不允许使用精力药水")
            return True

        # ---- 详情页：按下「使用道具」才算真喝掉 ----
        if use_item and mode != "关闭":
            before, _, _ = self._stamina_state()
            if before is None:
                close("读不到专注数值，疑似不是局外体力弹窗，不喝药")
                return True
            self._click_item(use_item, f"[确认使用道具 当前专注{before}]")
            self.sleep(1.1)
            after, cap2, still_open = self._stamina_state()
            gained = (after is not None and before is not None and after > before)
            # 「弹窗关了」也算成功，但必须真读到过一次数值，否则可能是 OCR 抽风
            if gained or (still_open is False and after is not None):
                self.stamina_refills += 1
                self.stamina_misses = 0
                self.log_info(f"局外专注 {before}→{after}（第 {self.stamina_refills}/{limit} 瓶）")
            else:
                self.stamina_misses += 1
                self.log_warning(f"喝药没生效（专注 {before}→{after}），"
                                 f"连续无效 {self.stamina_misses} 次")
                if self.stamina_misses >= 2:
                    close("连续两次点「使用道具」都没反应")
            return True

        # ---- 列表页 ----
        if mode == "关闭" or self.stamina_refills >= limit:
            close(f"已喝 {self.stamina_refills}/{limit} 瓶，不再补")
            return True

        uses = [it for it in items if it["t"].strip() == "使用"]
        if not uses:
            close("没有「使用」按钮（药水库存空了）")
            return True

        # 先确认这是 150 上限的局外体力弹窗，避免把别的「使用」误当成药水
        cur = cap = None
        for it in items:
            ab = self._parse_ab(it["t"])
            if ab and self._ab_kind(*ab, hint="focus") == "focus":
                cur, cap = ab[0], (ab[1] or self.FOCUS_CAP)
        if cur is None and not any("专注" in it["t"] for it in items):
            close("页面上没有专注(150)数值，判定不是局外体力弹窗，不喝药")
            return True
        if cur is not None:
            gap = max(0, (cap or 150) - cur)
            if gap <= 0:
                close(f"体力 {cur}/{cap} 已满（甚至溢出），不补")
                return True
            # 玩家给的关键背景（2026-11-23 06:49）：**一次训练只消耗 30**。
            # 所以根本不需要补满，够 30 就别动药水 —— 这是最省、也最不可能溢出的做法。
            if cur >= 30:
                close(f"体力 {cur}/{cap} 已够一次训练（消耗 30），不补")
                return True
        else:
            # 数值读不到（OCR 抽风但有「专注」字样）：保守按「不够一次」处理，
            # 只可能喝到最小瓶 +30，绝不会又灌一记 +60 溢出
            gap = 30

        # 把每行「专注+N」按 y 坐标配对到该行的「使用」按钮上
        rows = []
        for u in uses:
            gain, best_dy = 0, 1e9
            for it in items:
                m = re.search(r"专注\+(\d+)", it["t"].replace(" ", "").replace("＋", "+"))
                if not m:
                    continue
                dy = abs(it["cy"] - u["cy"])
                if dy < best_dy:
                    gain, best_dy = int(m.group(1)), dy
            rows.append((gain, u))

        fit = [(g, u) for g, u in rows if g]
        if not fit:
            close("没读到药水加成数值，不喝")
            return True
        # 一次训练 30 → 优先喝小瓶(+30)，刚好够一次，一滴不浪费；
        # 小瓶没库存时才退而喝大瓶(+60)。
        small = [(g, u) for g, u in fit if g <= 30]
        gain, pick = max(small, key=lambda x: x[0]) if small else min(fit, key=lambda x: x[0])

        self._click_item(pick, f"[补体力 {cur}/{cap} 缺口{gap} 选+{gain}]")
        self.sleep(0.9)
        return True

    def _do_confirm_team(self, items, frame):
        """确认编队：记录人数/器材数，然后开打。「活动加速」默认不开，避免多花资源。"""
        h, w = frame.shape[:2]
        mates = [it for it in items if 0.1 < it["cy"] / h < 0.72 and 0.08 < it["cx"] / w < 0.62
                 and re.search(r"^(SSR|SR|UR|R)\b", it["t"])]
        names = {re.sub(r"^(SSR|SR|UR|R)\s*", "", it["t"]) for it in mates}
        equips = [it for it in items if 0.3 < it["cy"] / h < 0.72 and it["cx"] / w > 0.68
                  and _num(it["t"]) and len(it["t"]) <= 4]
        score = len(names) * 1.0 + len(equips) * 0.5
        self.log_info(f"训练项评分：队友 {len(names)} 人（{sorted(names)}），器材 {len(equips)} 个，小计 {score}")
        for it in items:
            if "消耗" in it["t"] or "确定要继续" in it["t"]:
                self.log_warning(f"开打前提示：{it['t']}")
        return self._click_confirm(items, frame)

    def _yellow_options(self, frame, items):
        """底部一排黄色选项按钮（随机事件的三选一）。必须同水平线 ≥2 个才算。"""
        h = frame.shape[0]
        out = []
        for it in items:
            if it["cy"] / h < 0.70 or len(it["t"]) < 2 or len(it["t"]) > 18:
                continue
            if self._yellow_ratio(frame, it["b"]) > 0.45:
                out.append(it)
        if len(out) < 2:
            return []
        best_row = []
        for it in out:
            row = [o for o in out if abs(o["cy"] - it["cy"]) < h * 0.12]
            if len(row) > len(best_row):
                best_row = row
        return sorted(best_row, key=lambda x: x["cx"]) if len(best_row) >= 2 else []

    def _score_option(self, text):
        """事件选项打分：统一走 `_reward_key`（笔记 > 训练项 > 未溢出精力 > 属性）。

        2026-11-23 06:43 玩家指出优先级问题后，这里不再单独维护一套权重。
        以前是「精力一律 60+数值×0.5」，精力快满时 +70 仍压过「+30 且带属性」，
        和出行页是同一个毛病。现在精力先按缺口截断，打平再比属性。
        """
        return self._key_value(self._reward_key(text))

    def _event_cards(self, frame, items):
        """事件页底部的黄色大卡片：按列聚成组，每张卡取「标题行 + 效果行」。

        为什么不用「训练事件」当页面指纹：真实事件页根本没有这四个字，
        只有一张对话框 + 底部 3 张黄卡片，只能靠「成排黄卡片」这个图像特征。
        """
        h, w = frame.shape[:2]
        cands = []
        for it in items:
            if it["cy"] / h < 0.60 or len(it["t"]) < 2 or len(it["t"]) > 26:
                continue
            if self._yellow_ratio(frame, it["b"], pad=18) > 0.30:
                cands.append(it)
        if len(cands) < 2:
            return []
        cands.sort(key=lambda x: x["cx"])
        groups = []
        for it in cands:
            if groups and it["cx"] - groups[-1][-1]["cx"] < w * 0.18:
                groups[-1].append(it)
            else:
                groups.append([it])
        cards = []
        for g in groups:
            g.sort(key=lambda x: x["cy"])
            cards.append({
                "title": g[0]["t"],
                "text": " ".join(x["t"] for x in g),
                "cx": g[0]["cx"],
                "cy": g[0]["cy"],
            })
        return cards if len(cards) >= 2 else []

    def _do_event(self, items, frame, options=None):
        """随机事件：训练项 > 精力(未溢出) > 属性 > 金钱；判断不了选第一个。"""
        cards = self._event_cards(frame, items)
        if cards:
            scored = [(self._score_option(c["text"]), c) for c in cards]
            scored.sort(key=lambda x: -x[0])
            self.log_info(f"事件卡片打分: {[(c['title'], round(s, 1)) for s, c in scored]}")
            best = scored[0][1]
            self.log_info(f"选「{best['title']}」卡片：{best['text']}")
            self.click(best["cx"], best["cy"])
            return True

        opts = options if options is not None else self._yellow_options(frame, items)
        if not opts:
            h, w = frame.shape[:2]
            opts = [it for it in items
                    if 0.25 < it["cy"] / h < 0.95 and 0.08 < it["cx"] / w < 0.92
                    and 2 <= len(it["t"]) <= 16 and not re.match(r"^[\d/\.\+%]+$", it["t"])]
            opts = sorted(opts, key=lambda x: -x["cy"])[:3]
        if not opts:
            self.log_warning("事件页没识别到选项，点屏幕中间推进")
            self.click_relative(0.5, 0.5)
            return True
        scored = [(self._score_option(o["t"]), o) for o in opts]
        scored.sort(key=lambda x: -x[0])
        self.log_info(f"事件选项: {[(o['t'], round(s, 1)) for s, o in scored]}")
        best = scored[0][1]
        self._click_item(best, "[事件优选]")
        return True

    def _match_overlay(self, items):
        """未知页指纹匹配：命中返回 (名称, action)，否则 None。

        指纹表在 strategy.yaml → tuning.unknown.overlays。每项是
        `{name, keys: [...], action: next}`；keys 里任一关键词出现在画面文字里就算命中。
        可选 `exclude: [...]`：出现任一排除词则本条不命中（用来区分长得像的可交互页，
        例如经理增益页也有「心·技·体」，但有「确定」按钮，不该当属性浮层点掉）。
        纯展示页（比赛横幅、周总结、属性加成…）命中后走 `next`（点继续/中间推进），
        不再算「未知」——这样摘要里的未知次数才是真的「没见过」。
        """
        joined = " ".join(i["t"] for i in items)
        cfg = self._tuning().get("unknown") or {}
        for ov in cfg.get("overlays") or []:
            if not isinstance(ov, dict):
                continue
            excludes = ov.get("exclude") or []
            if excludes and any(str(k) in joined for k in excludes):
                continue
            keys = ov.get("keys") or []
            if any(str(k) in joined for k in keys):
                return (str(ov.get("name") or "已知浮层"), str(ov.get("action") or "next"))
        return None

    def _is_loading_frame(self, frame, items=None):
        """过场/加载帧 —— 画面还没渲染完或正在播过场，等一下就好，别判未知。

        实测两类（2026-11-23 15:15 / 15:19）：
        A. 白屏加载：顶部 HUD 在、主体整屏白。body std≈4，hud std≈42。
        B. 纯过场背景：紫色渐变+光斑，连 HUD 都没有，OCR 只从光斑里读出「9」。
           full std≈22、hud std≈16（远低于正常页的 90+），文字 ≤2 段。
        C. LOGO 转场（2026-11-23 17:12 实跑）：训练结果→大厅之间播蓝色剪影+篮球
           LOGO 动画，OCR 只读出「m」1 段就暂停。这帧有对比度(full std≈37)、
           不符合 A/B，但顶部无 HUD(hud std≈1.2)、大片蓝色剪影(占比≈27%)、
           背景米白(mean≈216)。离线扫全部截图只有这类帧命中，零误伤。
        D. 紫色半透明蒙版（2026-11-23 17:26 实跑）：每日页上方盖一层紫色转场
           蒙版（左紫三角+篮球水印+右斜杠），底下真实页面还在、OCR 能读出十几段
           文字（含训练/商店按钮），所以 B 的「文字≤2段」不满足。但这帧整屏偏紫
           (紫色像素占比≈68%)、整屏低对比(std≈18)。等蒙版消失即可，别当未知页。
        E. 训练结果动画（2026-11-23 21:12 实跑）：体育馆场景 + 左上「属性加成」
           横幅 + 四张未翻开的亮白卡片 + 光柱，OCR 只读 1 段，还常把「属性加成」
           读成「病性加成 / 肩性加成」。这帧 full std≈53、hud std≈54，A/B/C/D
           全不符合；但亮白(>225)占比≈8.7%、文字≤2段、顶栏周/天/精力全读不到。
           离线扫 204 张截图只命中 5 张同类动画帧，零误伤正常页。
        正常游戏页 full std 通常 >80、紫色占比极低，五种条件都碰不到，不会误伤。
        """
        if frame is None:
            return False
        try:
            h, w = frame.shape[:2]
            if h < 200 or w < 400:
                return False
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hud_std = float(gray[0:70, :].std())
            body_std = float(gray[80:h, :].std())
            # A. 白屏加载：主体空白 + 顶部 HUD 还在
            if body_std < 12.0 and hud_std > 20.0:
                return True
            # B. 纯过场背景：整屏低对比 + 几乎没读到文字（HUD 都还没出现）
            #    mean 下限排除全黑/纯黑帧（正常过场是亮紫色，mean≈200）
            if items is not None and gray.std() < 30.0 and hud_std < 25.0 \
                    and 60.0 < float(gray.mean()) < 250.0 and len(items) <= 2:
                return True
            # C. LOGO 转场：蓝色剪影占画面 1/4、顶部无 HUD、米白背景、几乎没文字
            if items is not None and len(items) <= 2 and hud_std < 5.0 \
                    and 180.0 < float(gray.mean()) < 250.0:
                bl, rr = frame[:, :, 0], frame[:, :, 2]
                blue_ratio = float(((bl > 140) & (rr < 130) & (bl - rr > 50)).mean())
                if blue_ratio > 0.15:
                    return True
            # D. 紫色半透明蒙版盖住真实页面：整屏偏紫 + 低对比（文字多，B 不满足）
            if items is not None and gray.std() < 35.0:
                bl, gc, rr = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
                purple_ratio = float(((bl > 140) & (rr > 110)
                                      & (bl - rr > 15) & (bl - gc > 10)).mean())
                if purple_ratio > 0.5:
                    return True
            # G. 训练结果「加成飘字」帧（2026-11-24 09:16 实跑）：教室立绘 + 两个「+15 意识」
            #    黄角标 + 底部一句「运控+30，意识+30」，OCR 5 段、无任何按钮，
            #    7.4b（≥3 属性词）/7.4c（要有等级字母）/E（≤4段）三条全接不住 → 判未知页 pause。
            #    特征：文字少（3~8 段）、一句行动按钮词都没有、且含「属性名+数字」增幅。
            #    正常功能页要么带「第N周/星期」，要么带训练/决定/下一步这类按钮词，碰不到这里。
            if items is not None and 3 <= len(items) <= 8:
                joined_g = " ".join(str(i.get("t", "")) for i in items)
                bad = ("第", "星期", "训练", "比赛", "商店", "确定", "下一步",
                       "跳过", "继续", "选择", "决定", "出行", "休息", "前往")
                if not any(k in joined_g for k in bad) and \
                        re.search(r"(?:%s)\s*[+＋]\s*\d+" % "|".join(ATTR_WORDS), joined_g):
                    return True
            # F. 蓝色剪影 LOADING 过场（2026-11-24 09:08 实跑，切剧本/切场景时出现）：
            #    整屏蓝白 + 大字 LOADING，OCR 只读到「-」1 段。实测 std 39.4、mean 203.7、
            #    白占比 0.355、hud_std 24.5、蓝色占比 0.417 —— C（hud_std<5）和
            #    _looks_transient（白占比<0.25）两条都接不住，于是判未知页 pause。
            #    特征很硬：**几乎没有文字 + 大片饱和蓝 + 亮底**，正常游戏页文字都远超 2 段。
            if items is not None and len(items) <= 2 and 150.0 < float(gray.mean()) < 245.0:
                bl, rr = frame[:, :, 0], frame[:, :, 2]
                blue_ratio = float(((bl > 140) & (rr < 130) & (bl - rr > 50)).mean())
                if blue_ratio > 0.30:
                    return True
            # E. 训练结果动画帧：亮白卡片+光柱，OCR 只剩几句被读花的碎片。
            #    实测同一帧读成「病性加成」(1段)、「-」(1段)、
            #    「肩性加成/つ/·体」(3段)，所以上限放宽到 4 段。
            #    离线扫 215 张截图：放宽后命中 7 张，全是这类动画帧，零误伤。
            if items is not None and len(items) <= 4:
                joined = " ".join(str(i.get("t", "")) for i in items)
                has_hud = any(k in joined for k in ("第", "星期", "/130", "/150"))
                over_words = ("加成", "属性", "提升", "成功", "获得", "等级", "结果")
                bright_ratio = float((gray > 225).mean())
                if (not has_hud) and bright_ratio > 0.04 \
                        and any(k in joined for k in over_words):
                    return True
            return False
        except Exception:
            return False

    def _looks_transient(self, items, frame):
        """画面还在播过场/动画的通用特征（不依赖具体画面规则）。

        文字 ≤2 段、顶栏 HUD（第N周/星期X/精力）一个都没读出来、也没有
        任何可点的行动/确认按钮，且画面不是纯黑纯白（那多半是采集失败）。
        2026-11-23 连续踩到白屏、紫渐变、LOGO、紫蒙版、「属性加成」动画，
        21:18 那次光柱把「属性加成」遮成「-」又漏了一条规则。与其一种一种补，
        不如这里做通用兜底：给几步宽限等动画自己过去，超时恢复原未知页处理，
        所以真实页面最多多等几秒，不会被永久跳过。
        """
        if frame is None or items is None or len(items) > 4:
            return False
        joined = " ".join(str(i.get("t", "")) for i in items)
        if any(k in joined for k in ("第", "星期", "/130", "/150")):
            return False
        buttons = ("训练", "休息", "出行", "比赛", "商店",
                   "确定", "下一步", "继续", "跳过", "关闭", "取消")
        if any(k in joined for k in buttons):
            return False
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if not (8.0 < float(gray.std()) and 25.0 < float(gray.mean()) < 245.0):
                return False
            # 选人页 / LOGO 转场也有「≤4段文字、无HUD」，但它们整屏亮白
            # (>225 占比 0.6~0.9)，训练结果动画帧只有 0.06~0.09。上限 0.25
            # 排除前者，避免把真实选人页当过场等过去。
            return float((gray > 225).mean()) < 0.25
        except Exception:
            return False

    # 通用推进器认识的「往前走」字形词。出现这些就点，不需要知道这是哪一页。
    ADVANCE_WORDS = ("点击屏幕继续", "点击继续", "确定", "确认", "继续", "下一步",
                     "好的", "知道了", "返回", "跳过", "开始", "进入",
                     "是", "否", "取消", "关闭", "以后再说")

    def _color_buttons(self, frame, items=None):
        """找「像按钮」的色块 —— 用框架的 find_color_rectangles，不读文字。

        2026-11-24 去重：原来在 vision.py 里自己写 HSV 掩膜 + 轮廓筛选，
        框架 ok.util.color.find_color_rectangles 就是干这个的（还带填充率阈值），
        直接换成它，少维护一份 300 行的重复实现。
        """
        out = []
        if frame is None:
            return out
        h, w = frame.shape[:2]
        for kind, rng, bonus in (("yellow", BTN_YELLOW, 0.2), ("blue", BTN_BLUE, 0.0)):
            try:
                boxes = find_color_rectangles(
                    frame, rng,
                    min_width=int(w * 0.06), min_height=int(h * 0.028),
                    max_width=int(w * 0.60), max_height=int(h * 0.10),
                    threshold=0.62)
            except Exception:
                boxes = []
            for b in boxes:
                cx, cy = b.x + b.width / 2.0, b.y + b.height / 2.0
                if cy < h * 0.10:                       # 顶部 HUD
                    continue
                if cx > w * 0.86 and cy < h * 0.24:     # 右上角充值/礼包
                    continue
                score = (0.6 * b.confidence
                         + 0.3 * min((cy / h) / 0.85, 1.0)
                         + 0.1 * (1.0 - min(abs(cx / w - 0.5) * 1.7, 1.0)))
                out.append({"cx": cx, "cy": cy, "w": b.width, "h": b.height,
                            "kind": kind, "score": round(score + bonus, 3), "text": ""})
        out.sort(key=lambda d: -d["score"])
        kept = []
        for b in out:
            if any(abs(b["cx"] - k["cx"]) < k["w"] * 0.6 and
                   abs(b["cy"] - k["cy"]) < k["h"] * 0.8 for k in kept):
                continue
            kept.append(b)
            if len(kept) >= 6:
                break
        for b in kept:
            if items:
                b["text"] = self._text_on_button(b, items)
        return kept

    @staticmethod
    def _text_on_button(btn, items):
        """这个按钮框里/附近有哪段 OCR 文字（只给日志和危险词判断用）。"""
        for it in items or []:
            if (abs(it["cx"] - btn["cx"]) <= btn["w"] * 0.8
                    and abs(it["cy"] - btn["cy"]) <= btn["h"] * 0.9):
                return it["t"]
        return ""

    def _danger_button(self, btn, items):
        """按钮上/附近的文字是不是「花钱」动作。返回 (bool, 命中文字)。"""
        for it in items or []:
            if (abs(it["cx"] - btn["cx"]) <= btn["w"] * 0.8
                    and abs(it["cy"] - btn["cy"]) <= btn["h"] * 0.9):
                t = it["t"]
                if any(w in t for w in DANGER_TEXT):
                    return True, t
                if "恢复" in t and any(ch.isdigit() for ch in t):
                    return True, t
        return False, ""

    @staticmethod
    def _frame_diff(a, b):
        """两帧差多少（0~100 的「变化率」）。框架没有帧差接口，这里保留 6 行自写。

        缩到 160x90 再比灰度均值差 —— 飘字/粒子这种小面积变化不会被误判成「页面变了」。
        """
        try:
            if a is None or b is None:
                return 0.0
            if a.shape != b.shape:
                b = cv2.resize(b, (a.shape[1], a.shape[0]))
            ga = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (160, 90),
                            interpolation=cv2.INTER_AREA)
            gb = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (160, 90),
                            interpolation=cv2.INTER_AREA)
            return float(np.mean(cv2.absdiff(ga, gb)) / 255.0 * 100.0)
        except Exception:
            return 0.0

    def _advance_generic(self, items, frame, page="未知"):
        """通用推进器：不认识这页，也要自己走出去。

        以前的兜底是 fallback=pause（停下等人工），每冒一个新事件就断一次。
        改成：OCR 认识的安全按钮 → 视觉色块按钮 → ESC → 右上角 X → 屏幕中间，
        每点一次都验证画面到底变没变，没变就换下一个候选。
        连续 give_up_after 次全都推不动才停（有上限，不会无限刷）。

        返回 True=继续跑；None=放弃（真的走不出去，需要人看）。
        """
        cfg = (self._tuning().get("unknown") or {}).get("advance") or {}
        max_clicks = int(cfg.get("max_clicks") or 4)
        wait = float(cfg.get("wait") or 0.8)
        diff_thr = float(cfg.get("diff_thr") or 1.2)
        give_up = int(cfg.get("give_up_after") or 6)
        base = frame.copy() if frame is not None else None

        cands = []
        for it in items:
            t = it["t"].strip()
            if len(t) <= 8 and any(w in t for w in self.ADVANCE_WORDS):
                cands.append({"cx": it["cx"], "cy": it["cy"], "w": 140, "h": 52,
                              "kind": "ocr", "text": t, "score": 9.0})
        # 色块找按钮：框架 ok.util.color.find_color_rectangles（黄色主按钮 / 蓝色次按钮）
        for b in self._color_buttons(frame, items):
            danger, why = self._danger_button(b, items)
            if danger:
                self.log_info(f"[推进] 跳过 {b['kind']}按钮@({int(b['cx'])},{int(b['cy'])})："
                              f"附近文字「{why}」像花钱，不点")
                continue
            cands.append(b)
        # 去重（同一个按钮被 OCR 和视觉各抓一次）
        uniq = []
        for c in cands:
            if any(abs(c["cx"] - u["cx"]) < 45 and abs(c["cy"] - u["cy"]) < 45 for u in uniq):
                continue
            uniq.append(c)
        uniq.sort(key=lambda c: -c.get("score", 0))

        tried = 0
        for c in uniq:
            if tried >= max_clicks:
                break
            tried += 1
            label = c.get("text") or f"{c['kind']}按钮"
            self.click(c["cx"], c["cy"])
            self.sleep(wait)
            f2 = self._frame()
            if f2 is None:
                continue
            d = self._frame_diff(base, f2)
            if d >= diff_thr:
                self.log_info(f"[推进] 点「{label}」→ 画面变化 {d:.1f}%，走出去了")
                self._advance_fails = 0
                return True
            self.log_info(f"[推进] 点「{label}」画面没变（{d:.1f}%），换下一个候选")

        # 逃生链：键盘 → 右上角 X → 屏幕中间。只等一次，别把每次点击都算进 max_clicks
        escapes = [
            ("ESC", lambda: self.send_key("esc")),
            ("右上角X", lambda: self._click_close_x(frame)),
            ("屏幕中间", lambda: self.click_relative(0.5, 0.5)),
        ]
        for name, act in escapes:
            try:
                act()
            except Exception as e:
                self.log_warning(f"[推进] {name} 失败：{e}")
                continue
            self.sleep(wait)
            f2 = self._frame()
            if f2 is None:
                continue
            d = self._frame_diff(base, f2)
            if d >= diff_thr:
                self.log_info(f"[推进] 逃生链「{name}」→ 画面变化 {d:.1f}%，走出去了")
                self._advance_fails = 0
                return True

        n = getattr(self, "_advance_fails", 0) + 1
        self._advance_fails = n
        if n >= give_up:
            p = self._shot(frame, f"stuck_{n}")
            self.log_error(f"通用推进器连续 {n} 次都没能离开未知页，暂停等人工。"
                           f"文字: {[i['t'] for i in items][:10]}｜截图: {p}", notify=True)
            return None
        self.log_warning(f"[推进] 这一屏所有候选都没推动（{n}/{give_up}），下一步重试")
        return True

    def _click_close_x(self, frame):
        """点右上角的关闭 X：先模板匹配，认不到就用实测比例坐标。"""
        pos = self._match_template(frame, "x_close.png")
        if pos:
            self.click_relative(pos[0], pos[1])
            return
        self.click_relative(0.929, 0.100)

    def _do_unknown(self, items, frame):
        joined = " ".join(i["t"] for i in items)
        texts = [i["t"] for i in items][:16]
        cfg = self._tuning().get("unknown") or {}

        # 0) 过场/加载帧：等它渲染完，绝不当未知页停下（也不点，避免误触）
        if self._is_loading_frame(frame, items):
            wait_s = float(cfg.get("wait_seconds") or 2.0)
            self.log_info(f"白屏加载帧（HUD 在、主体空）→ 等 {wait_s}s 让页面渲染")
            self.sleep(wait_s)
            return True

        # 0.5) 疑似过场/动画帧：给 4 步宽限，只等不点（也不记未知页）。
        #      超时就不再等，按下面的正常未知页流程处理，避免真实页面被跳过。
        if self._looks_transient(items, frame):
            n = getattr(self, "_transient_frames", 0) + 1
            self._transient_frames = n
            if n <= 4:
                wait_s = float(cfg.get("wait_seconds") or 2.0)
                self.log_info(f"[过场宽限] 疑似动画帧（文字 {len(items)} 段、顶栏 HUD 未出现）"
                              f"→ 等 {wait_s}s 让它自己播完（{n}/4）")
                self.sleep(wait_s)
                return True
            self.log_warning("[过场宽限] 连续 4 步仍是疑似动画帧 → 不再等待，按未知页处理")
        else:
            self._transient_frames = 0

        if any(k in joined for k in ("拼图", "碎片", "小游戏")):
            self.log_warning("未知页面但看着像小游戏，按放弃处理")
            return self._do_minigame(items, frame)
        if self._event_cards(frame, items):
            self.log_info("未知页面但是事件卡片样式，按事件处理")
            return self._do_event(items, frame)

        # 1) 先查已知浮层指纹表（命中就不算未知，走 next）
        hit = self._match_overlay(items)
        if hit:
            name, action = hit
            self.known_overlay_hits[name] = self.known_overlay_hits.get(name, 0) + 1
            if action == "pause":
                self.log_warning(f"已知浮层「{name}」按配置暂停，等你处理")
                return None
            # wait：自动消失的过场横幅（如「CD收集心愿开始」），等它自己过去，别点、
            #       也别判未知。实测 2026-11-23 15:11：过场未播完时 OCR 只读到标题，
            #       下一两秒游戏自己就进了每日页；此时点屏幕中间是多余的。
            if action == "wait":
                wait_s = float(cfg.get("wait_seconds") or 2.0)
                self.log_info(f"已知浮层「{name}」→ 等待 {wait_s}s 让过场自己过去")
                self.sleep(wait_s)
                return True
            # next：优先点页面上的「继续/下一步/确定」，实在没有再点中间
            if self._click_confirm(items, frame) or self._click_next_word(items, frame):
                self.log_info(f"已知浮层「{name}」→ 点继续推进")
                return True
            self.click_relative(0.5, 0.5)
            self.log_info(f"已知浮层「{name}」→ 点屏幕中间推进")
            return True

        # 2) 真·未知页：先试通用确认按钮
        if self._click_confirm(items, frame):
            return True

        # 老配置「未知页面兜底」= 暂停 仍优先（兼容）；否则用新配置 fallback
        old = str(self.config.get("未知页面兜底") or "")
        fallback = ("pause" if old == "暂停"
                    else str(cfg.get("fallback") or "auto"))
        self.unknown_count += 1
        self.unknown_texts.append(texts)

        if fallback == "pause":
            p = self._shot(frame, f"unknown_{self.unknown_count}")
            self.log_error(f"没见过的页面（第 {self.unknown_count} 次）按配置暂停。"
                           f"文字: {texts}｜截图: {p}", notify=True)
            return None

        # auto：通用推进器（视觉找按钮 → 点击 → 差分验证 → 逃生链）
        # 2026-11-24 起这是默认行为：新页面不再停机，自己想办法走出去。
        if fallback not in ("center",):
            self.log_warning(f"未知页面（第 {self.unknown_count} 次）→ 通用推进器接管。"
                             f"文字: {texts[:8]}")
            return self._advance_generic(items, frame, page="未知")

        limit = int(cfg.get("max_center_clicks") or 10)
        if self.unknown_count <= limit:
            self.log_warning(f"未知页面（第 {self.unknown_count} 次），点屏幕中间推进。"
                             f"文字: {texts[:8]}")
            self.click_relative(0.5, 0.5)
            return True
        p = self._shot(frame, f"unknown_{self.unknown_count}")
        self.log_error(f"未知页面连续 {self.unknown_count} 次没有可点主按钮，暂停。"
                       f"文字: {texts}｜截图: {p}", notify=True)
        return None

    def _click_next_word(self, items, frame):
        """页面上有「继续/点击屏幕继续/下一步」这类文字就点它，没有返回 False。"""
        it = self._find(items, ["点击屏幕继续", "继续", "下一步", "确定"], exact=False)
        if it:
            self._click_item(it, "[浮层-继续]")
            return True
        return False

    # ---------- MCP 基线契约 ----------
    # ok-script 的 MCP 在每次尝试前调用 reset_to_start() + verify_start_state()，
    # 这样"每一次验证都从同一状态开始"，不会再出现"接着上一轮残局跑"的问题。

    def reset_to_start(self):
        """清掉挡住屏幕的浮层。

        本项目的状态机是**按页面流转**的（不依赖第几周第几天计数），所以任何一屏
        都能接着往下跑，不需要回到大厅重开局。这里只做幂等的事：关掉活动/公告/礼包
        这类纯遮挡浮层，其余一律不动。
        """
        cleared = 0
        for _ in range(3):
            frame = self._frame()
            if frame is None:
                break
            items = self._texts(frame)
            joined = " ".join(i["t"] for i in items)
            if not any(k in joined for k in ("活动", "公告", "礼包", "签到", "限时")):
                break
            closer = self._find(items, ["关闭", "取消"], exact=True)
            if not closer:
                break
            self._click_item(closer, "[reset-关浮层]")
            cleared += 1
            self.sleep(0.7)
        self.log_info(f"reset_to_start 完成（关闭浮层 {cleared} 个）")
        return True

    def verify_start_state(self):
        """确认「能正常看到并识别游戏画面」。

        只在真正没法继续时返回 False（游戏窗口没了 / 截图全黑 / OCR 什么都没认出来）。
        认得出页面但不是"起点"，也放行 —— 因为状态机本来就是接着当前页面往下走。
        """
        frame = self._frame()
        if frame is None:
            self.log_error("verify_start_state: 取不到画面（游戏窗口不可见？）")
            return False
        items = self._texts(frame)
        page = self._classify(items, frame)
        self.log_info(f"verify_start_state: {frame.shape[1]}x{frame.shape[0]}，"
                      f"判定页面「{page}」，文字 {len(items)} 段")
        if len(items) < 3:
            self.log_error(f"verify_start_state: 只认出 {len(items)} 段文字，画面可能全黑")
            return False
        if page == "未知":
            self.log_warning("verify_start_state: 认不出页面，但画面正常，继续（未知页有兜底）")
        return True

    # ---------- 主循环 ----------

    def run(self):
        self.notes_done = False
        self.stamina = None
        self.stamina_max = None
        self.out_focus = None
        self.out_focus_max = None
        self.weekday = None
        self.item_checked = False
        self.read_fail_stop = False    # 训练项读数失败 → 主循环停机（玩家 2026-11-26）
        self._item_interrupt = None      # 读数被经理增益/剧情/遮罩打断时的页面名
        self.trip_picked = False
        # 「出行先探笔记，笔记 > 训练」（玩家 2026-11-23）：
        #   每日页同时有「训练」和「出行」时，先进出行页看地点标签里有没有笔记。
        #   有 → 直接出行；没有 → 退回每日页走训练。
        #   用「第N周-星期X」记今天已经探过，避免同一天反复进出。
        self.trip_probe = False          # 本次进出行页是「探笔记」，不是真要出行
        self.trip_probe_done = None      # **真的进过**出行页的那天，例如 "6-7"
        # 2026-11-24 第七周周六漏探笔记的根因：旧代码在**点击之前**就写 done，
        # 点「出行」被 FLUTTERVIEW 挡住时页面还在每日页，却被记成「今天探过了」，
        # 下一帧直接走训练分支。现在 done 只在真正进入出行页时才写，
        # 这里单独记「试了几次还没进去」，最多重试 3 次。
        self.trip_probe_retry = 0        # 当天点了出行但没进页面的次数
        self.trip_probe_retry_day = None # 上面那个计数属于哪一天
        self.trip_probe_disabled = False # 兼容旧状态；现在不再置 True（退回失败只放弃当天）
        self.trip_back_tries = 0         # 本次退出出行页试了几次
        # 探笔记的「两级右上角返回」还剩几层：2=扭蛋三选一页, 1=出行页, 0=无
        self.trip_probe_back = 0
        # 商店：以前 _do_shop 写好了却**从来没被调用过**（每日页没接入口），
        # 整轮跑下来一次店都没进、精力药也没买过（2026-11-23 查 run_1100/1105 日志确认）。
        # 现在每日页在「精力 < 阈值」时进店看一眼，按天记账，一天最多进一次。
        self.shop_day = None
        self.shop_week = None          # 已经例行看过货的那一周（商店 7 天刷新）
        self.unknown_count = 0
        self.minigame_tries = 0
        self.stock_idx = 0
        self.stock_tries = 0
        self.stock_picked = False
        self.slots_autofill = 0
        self.slots_empty = 0
        self.slots_checked_page = None   # 已经确认过「没有空槽」的那一页（只用来打一次日志）
        self.confirm_gray = 0            # 选人页「下一步」连续灰着几次了（配队没满就暂停）
        # 上一场训练在场上看到的器材数（商店买「器材暴击」要先确认器材上有人）
        self.last_equip_count = 0
        # 商店有没有精力道具卖：None=本期还没进过店 / True=有 / False=没有。
        # 「失败率全部>30 且无精力药 → 改练意识」这条规则要用它，没进店就不敢乱判。
        self.shop_has_stamina = None
        self.shop_bought = 0
        # 本轮主训人员（每局会换，由 _main_char_of_round 从画面上认，不写死名字）
        self.main_char_round = None
        # 已知主角名池：只用于剔除 OCR 粘连词（「百希雅」= 百目鬼 + 希雅），
        # 不作为「谁是主训」的判断依据。每轮认出新主训就补进来。
        self.known_main_chars = ["希雅", "闪堂"]
        self.route = {}
        self.in_training = False
        self.saw_training = False
        self.phase = PHASE_HALL            # 显式阶段；断点恢复时会被 _load_state 覆盖
        self.choice_idx = 0
        self.stamina_refills = 0
        self.stamina_misses = 0

        # ---- 收工摘要统计 ----
        #   2026-11-23 16:4x：原来 stat 全局累计、跨轮不清，摘要却叫「本轮运行摘要」，
        #   早期读数失败留下的「投射×5」被每次 stop 反复打印几十次（假数据）。
        #   现在拆两份：stat=本轮，stat_total=本次运行累计；每轮开始把上一轮并入累计
        #   再清零本轮，摘要的「本轮」名副其实，累计另起一行。
        self.stat = self._new_stat()
        self.stat_total = self._new_stat()

        max_steps = int(self.config.get("最大步数") or 1500)
        wait = float(self.config.get("等待秒数") or 1.3)
        # 循环轮数：`tuning.loop.max_rounds`（工作台上的那颗「循环轮数」）
        #   「跑完一轮就停」是老的单轮开关，仍然有最高优先级 —— 它一开就是 1 轮，
        #   这样以前的行为一字不变，不会因为改了轮数把老配置搞出意外。
        loop_cfg = self._tuning().get("loop") or {}
        max_rounds = max(1, int(loop_cfg.get("max_rounds") or 1))
        if bool(self.config.get("跑完一轮就停", True)) and max_rounds > 1:
            self.log_warning(f"配置里循环 {max_rounds} 轮，但「跑完一轮就停」= true → 只跑 1 轮"
                             f"（工作台保存时会把这两项对齐）")
            max_rounds = 1
        self.rounds_done = 0
        self.max_rounds = max_rounds

        self.log_info(f"开始自动训练：最多 {max_steps} 步，共 {max_rounds} 轮")
        self._load_state()
        # 运行记录：写一个 start 事件；断点续跑时会沿用同一个 run_id（见 _runlog_start）
        self._runlog_start()

        # 角色分支：不同主训人员走不同支线（剧情选项、小游戏、事件都不一样）。
        # 结构先接好，规则后填 —— strategy.yaml 的 routes 现在还是空的。
        #   用法：routes.<角色名>.<页面或模块>.<字段>，例如 routes.希雅.stock.mode
        _routes = self._tuning().get("routes") or {}
        _char = (self._tuning().get("core_score") or {}).get("main_char")
        self.route = _routes.get(_char) or {}
        if self.route:
            self.log_info(f"已载入角色分支规则：{_char}（{len(self.route)} 项）")
        else:
            self.log_info(f"角色 {_char} 暂无分支规则，全部走默认")
        self._start_watchdog()
        try:
            return self._run_loop(max_steps, wait, max_rounds)
        except Exception as e:
            # 异常停机也必须留痕 + 通知：以前只有日志里一行 traceback，
            # 人不在屏幕前根本不知道脚本什么时候、为什么停了。
            import traceback
            tb = traceback.format_exc()
            self._runlog_status = f"异常：{type(e).__name__}"
            self.log_error(f"脚本异常停机：{type(e).__name__}: {e}\n{tb[-800:]}", notify=True)
            raise
        finally:
            self._watchdog_stop = True
            # 正常收工和异常停机都打一份摘要（_run_loop 正常结束已打过，用标志去重）
            if not getattr(self, "_summary_printed", False):
                self._summary_printed = True
                self._print_summary()
            # 运行记录收尾：无论正常/异常都写，工作台的「运行记录」页据此显示本次结果
            self._runlog_end(getattr(self, "_runlog_status", "正常"))

    # ---------- 每轮最终得分留证（玩家 2026-11-24）----------
    # 「训练评分 / 恭喜获得」页会带一个四位数评分（录制样本里是 中锋7108），
    # 这是整轮训练唯一的总得分证据，页面一闪就过。只读不点：存图 + 存 OCR 原文，
    # 供事后人工核对（玩家明确要「只要结果」的每轮得分）。
    # 每次进页都记一条（上限 8 条/轮/页），因为数字可能在后一帧才读全。
    def _capture_score(self, items, frame, page):
        try:
            rnd = int(getattr(self, "rounds_done", 0)) + 1
            key = f"{rnd}_{page}"
            buf = getattr(self, "_score_captured", None)
            if buf is None:
                self._score_captured = buf = {}
            n = buf.get(key, 0)
            if n >= 8:
                return
            buf[key] = n + 1
            texts = [str(it.get("t") or "").strip() for it in items if it.get("t")]
            nums = []
            for t in texts:
                for m in re.findall(r"\d{4,5}", t):
                    try:
                        nums.append(int(m))
                    except Exception:
                        pass
            path = ""
            tag = "rating" if page == "训练评分" else "gain"
            try:
                os.makedirs("logs/scores", exist_ok=True)
                # 文件名带时刻，避免「重启后轮次又从 1 开始」把上一张同名图覆盖掉
                path = (f"logs/scores/round{rnd}_{tag}_"
                        f"{time.strftime('%H%M%S')}_{n + 1}.png")
                if frame is not None:
                    ok = cv2.imwrite(path, frame)
                    if not ok or not os.path.exists(path) or os.path.getsize(path) < 1024:
                        # cv2.imwrite 对非 ASCII 文件名不可靠，PIL 兜底（帧是 BGR）
                        from PIL import Image
                        Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(path)
            except Exception:
                path = ""
            self.log_info(f"★ 第 {rnd} 轮「{page}」页 OCR {len(texts)} 段"
                          f"→ 四位数 {nums or '无'}（存图 {path or '失败'}）")
            # 终局目标 ③：每轮最终评分要和 9000 比一比（日志里直接给差距，事后不用自己算）
            if page == "训练评分" and nums:
                try:
                    target = int(self._goal_cfg().get("target_score") or 0)
                except Exception:
                    target = 0
                got = max(nums)
                self.final_score = got
                if target:
                    gap = got - target
                    msg = (f"本轮最终评分 {got}｜目标 {target} → "
                           + (f"达标 +{gap}" if gap >= 0 else f"差 {abs(gap)} 分"))
                    if gap >= 0:
                        self.log_info("🎯 " + msg)
                    else:
                        self.log_warning("⚠ " + msg)
                    self.stat.setdefault("scores", []).append(got)
            if nums:
                try:
                    import json
                    with open("logs/scores.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"round": rnd, "page": page, "nums": nums,
                                            "texts": texts, "png": path,
                                            "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                           ensure_ascii=False) + "\n")
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- 运行记录（给工作台的「运行记录」页看）----------
    # 设计：append-only 的 jsonl，一次运行一个 run_id，事件分 start / train / end。
    #   为什么不在内存里攒一份给工作台：AutoTask 是工作台 spawn 的**独立进程**，
    #   内存不共享；只能通过文件交接。jsonl 追加写的好处是崩了也不丢已写的记录，
    #   工作台读的时候正好看到「有 start 没 end」= 还在跑 / 被强停。
    #   任何一步写失败都吞掉 —— 记录是锦上添花，绝不能因为磁盘满就把训练搞崩。

    def _runlog_path(self):
        cfg = self._tuning().get("runlog") or {}
        if cfg.get("enabled") is False:
            return None
        return str(cfg.get("path") or "logs/runs.jsonl")

    def _runlog_event(self, ev):
        p = self._runlog_path()
        if not p:
            return
        try:
            import json
            if not os.path.isabs(p):
                root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                p = os.path.join(root, p)
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            ev["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _runlog_start(self):
        # 断点续跑：run_id 已从 .state 恢复，就接着记（写一条 resume 便于对账），
        # 不新开一次运行，否则「本次运行几轮」会被拆成两次。
        if getattr(self, "run_id", None):
            self._runlog_event({"event": "resume", "run": self.run_id})
            return
        self.run_id = time.strftime("%Y%m%d_%H%M%S")
        self._runlog_first_stamina = None
        self._runlog_first_focus = None
        self._runlog_status = "正常"
        loop = self._tuning().get("loop") or {}
        self._runlog_event({"event": "start", "run": self.run_id,
                            "rounds": int(loop.get("max_rounds") or 1)})

    def _runlog_train(self, skill, score, pct, mates, equips):
        """一次选课 = 一条训练记录。得分是 _score_item 算出来的编队期望分。"""
        if not getattr(self, "run_id", None):
            return
        st, fo = self.stamina, self.out_focus
        if getattr(self, "_runlog_first_stamina", None) is None and st is not None:
            self._runlog_first_stamina = [st, self.stamina_max]
        if getattr(self, "_runlog_first_focus", None) is None and fo is not None:
            self._runlog_first_focus = [fo, self.out_focus_max]
        self._runlog_event({
            "event": "train", "run": self.run_id,
            "round": int(getattr(self, "rounds_done", 0)) + 1,
            "skill": skill, "score": round(float(score or 0), 2),
            "fail": pct, "mates": mates, "equips": len(equips or []),
            "stamina": st, "stamina_max": self.stamina_max,
            "focus": fo, "focus_max": self.out_focus_max,
        })

    def _runlog_end(self, status="正常"):
        if not getattr(self, "run_id", None):
            return
        st = getattr(self, "stat", {}) or {}
        first_st = getattr(self, "_runlog_first_stamina", None)
        first_fo = getattr(self, "_runlog_first_focus", None)
        self._runlog_event({
            "event": "end", "run": self.run_id, "status": status,
            "rounds_done": int(getattr(self, "rounds_done", 0)),
            "rounds": int(getattr(self, "max_rounds", 0)),
            "trains": int(sum((st.get("train") or {}).values())),
            "stamina_start": first_st[0] if first_st else None,
            "stamina_end": self.stamina,
            "focus_start": first_fo[0] if first_fo else None,
            "focus_end": self.out_focus,
            "drinks": int(getattr(self, "stamina_refills", 0)),
        })
        # 写完就本次运行结束，下次启动要新开 run_id
        self.run_id = None

    # ---------- 断点续跑 ----------
    # 状态机本来就是按页面流转的（任何一屏都能接着跑），所以断点只需记住
    # 「今天探没探笔记 / 进没进过店 / 这是第几轮」这类**记账**，不用记完整画面。
    RESUME_FIELDS = (
        "saw_training", "in_training", "phase", "weekday", "item_checked",
        "trip_picked", "trip_probe", "trip_probe_done",
        "trip_probe_retry", "trip_probe_retry_day",
        "trip_back_tries", "shop_day", "shop_week", "shop_bought",
        "shop_has_stamina", "notes_done", "minigame_tries", "stock_idx",
        "stock_tries", "stock_picked", "stamina_refills", "stamina_misses",
        "last_equip_count", "rounds_done", "stat",
        "run_id", "_runlog_first_stamina", "_runlog_first_focus",
    )

    def _state_path(self):
        cfg = self._tuning().get("resume") or {}
        if cfg.get("enabled") is False:
            return None
        return str(cfg.get("path") or ".state/last_run.json")

    def _save_state(self, step, page):
        """每步写盘（原子替换）。失败不影响主流程。"""
        p = self._state_path()
        if not p:
            return
        try:
            import json
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            data = {k: getattr(self, k, None) for k in self.RESUME_FIELDS}
            data.update({"step": step, "page": page, "at": time.time()})
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp, p)
            self._resume_saved_at = time.time()
        except Exception:
            pass

    def _load_state(self):
        """启动时读盘恢复；没有/损坏/太旧就当新跑。"""
        p = self._state_path()
        if not p or not os.path.exists(p):
            return
        try:
            import json
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            age = time.time() - float(data.get("at") or 0)
            if age > 6 * 3600:      # 超过 6 小时的旧档不算（游戏早关了）
                self.log_info(f"断点文件太旧（{age / 3600:.1f}h），按新跑")
                return
            for k in self.RESUME_FIELDS:
                if k in data and data[k] is not None:
                    setattr(self, k, data[k])
            # 兼容旧断点（没有 phase 字段）：从 saw_training 推导，
            # 免得老档恢复后阶段留在 HALL、回大厅白丢一轮。
            if "phase" not in data or not data.get("phase"):
                if getattr(self, "saw_training", False):
                    self.phase = PHASE_TRAINING
                else:
                    self.phase = PHASE_HALL
            self.log_info(f"★ 断点恢复：从「{data.get('page')}」第 {data.get('step')} 步接上"
                          f"（阶段 {self.phase}，{age / 60:.0f} 分钟前存的）", notify=True)
        except Exception as e:
            self.log_warning(f"断点文件读取失败（{e}），按新跑")

    def _clear_state(self):
        p = self._state_path()
        if p:
            try:
                os.remove(p)
            except OSError:
                pass

    def _print_summary(self):
        """收工摘要：一轮下来到底干了啥，一张表看清（配置 summary.enabled）。"""
        cfg = self._tuning().get("summary") or {}
        if cfg.get("enabled") is False:
            return
        self._summary_printed = True
        st = getattr(self, "stat", {}) or {}
        train = st.get("train") or {}
        gamble = st.get("gamble") or []
        buys = st.get("buy") or []
        total = getattr(self, "stat_total", None) or self._new_stat()
        tt = total.get("train") or {}
        lines = [
            "━━━━━━━━ 本轮运行摘要 ━━━━━━━━",
            f"完成轮数：{self.rounds_done}/{self.max_rounds}",
            f"本轮训练 {sum(train.values())} 次：" + ("、".join(f"{k}×{v}" for k, v in train.items()) or "无"),
            f"累计训练 {sum(tt.values())} 次：" + ("、".join(f"{k}×{v}" for k, v in tt.items()) or "无"),
            f"探步行街笔记 {st.get('note_probe', 0)} 次（拿到笔记 {st.get('note', 0)} 次）"
            f"　训练项读数失败 {st.get('sample_fail', 0)} 项",
            f"商店买 {len(buys)} 件：" + ("、".join(f"{n}({p})" for _, n, p in buys[:8]) or "无"),
            f"赌一把 {len(gamble)} 次：" + ("、".join(f"{k}@{p}%" for k, p in gamble[:8]) or "无"),
        ]
        # 终局目标 ③：最终评分 vs 9000
        scores = st.get("scores") or []
        try:
            target = int(self._goal_cfg().get("target_score") or 0)
        except Exception:
            target = 0
        if scores:
            hit = sum(1 for x in scores if x >= target) if target else 0
            lines.append(f"最终评分 {scores}" + (f"｜目标 {target} → 达标 {hit}/{len(scores)} 轮" if target else ""))
        else:
            lines.append("最终评分：本轮没读到「训练评分」页（可能中途停了）")
        if self.known_overlay_hits:
            lines.append("已知浮层（累计）：" + "、".join(
                f"{k}×{v}" for k, v in self.known_overlay_hits.items()))
        lines.append(f"未知页 {len(self.unknown_texts)} 次")
        st = getattr(self, "_vis_stats", None) or {}
        if st:
            lines.append(f"锚点层：命中 {st.get('hit', 0)} 次、补位 {st.get('fill', 0)} 次、"
                         f"冲突改判 {st.get('conflict', 0)} 次、硬约束否决 {st.get('veto', 0)} 次"
                         f"、读数救回 {st.get('anchor_rescue', 0)} 帧")
        trans = getattr(self, "transition_warns", []) or []
        if trans:
            pairs = {}
            for a, b, _ in trans:
                pairs[f"{a}→{b}"] = pairs.get(f"{a}→{b}", 0) + 1
            lines.append("页面跳转存疑（软约束，未拦截）：" + "、".join(
                f"{k}×{v}" for k, v in pairs.items()))
        for ln in lines:
            self.log_info(ln)
        # 顺手写一份到 logs/，方便事后翻（不刷屏控制台）
        try:
            os.makedirs("logs", exist_ok=True)
            p = f"logs/summary_{time.strftime('%m%d_%H%M%S')}.txt"
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.log_info(f"摘要已存：{p}")
        except Exception:
            pass

    def _check_transition(self, prev, page, frame=None, step=0):
        """页面级转移软约束：不可能的「上页→当前页」记警告+截图，但照常处理。

        只警告不拦，是为了先收集真实误报样本——玩家要的马尔可夫链式限制，
        得先证明转移表在真实运行里不误伤，才能收紧成硬约束。
        过场帧被判成「未知」很常见，出现「未知→任何页」「任何页→未知」都跳过。
        """
        if not prev or page in ("未知",) or prev == "未知" or prev == page:
            return False
        bad = PAGE_FORBIDDEN.get(prev) or ()
        if page not in bad:
            return False
        if not hasattr(self, "transition_warns"):
            self.transition_warns = []
        self.transition_warns.append((prev, page, step))
        shot = self._shot(frame, f"transition_{step}") if frame is not None else ""
        self.log_warning(f"页面跳转存疑：{prev} → {page}（第{step}步，只记录不拦截"
                         + (f"，截图 {shot}" if shot else "") + "）")
        return True

    def _set_phase(self, to, page="", reason=""):
        """显式阶段转换：HALL → TRAINING → SETTLING → (回大厅计一轮) → HALL。

        为什么要有这个方法（2026-11-23 玩家要求）：
          旧版靠 `page not in ("大厅","未知")` 就 `saw_training=True` —— 公告页、
          小游戏、误判页都会把「训练已开跑」点亮，回大厅就白计一轮；而且
          `saw_training`/`in_training` 两个布尔语义含糊，断点恢复后也说不清在哪。
          现在阶段有明确定义和转换日志，非法跳跃只警告不强改，便于从日志追。

        同时兼容旧字段：TRAINING/SETTLING 阶段 `saw_training=True`，
        `in_training` 只在真正点进训练时为 True（保持原有的下游判断不变）。
        """
        old = getattr(self, "phase", PHASE_HALL)
        if old == to:
            return False
        # 允许的转换；回退到 HALL 是新一轮开始或收工，都合法
        allowed = {
            PHASE_HALL: (PHASE_TRAINING,),
            PHASE_TRAINING: (PHASE_SETTLING, PHASE_HALL),
            PHASE_SETTLING: (PHASE_HALL,),
        }
        if to not in allowed.get(old, ()):
            self.log_warning(f"阶段跳跃 {old}→{to}（页面 {page}），按 {to} 处理：{reason}")
        self.phase = to
        self.saw_training = to in (PHASE_TRAINING, PHASE_SETTLING)
        self.log_info(f"阶段 {old}→{to}" + (f"（{page}）" if page else "")
                      + (f" {reason}" if reason else ""))
        return True

    @staticmethod
    def _new_stat():
        return {
            "train": {},        # 训练项 -> 次数
            "gamble": [],       # 每次下注 (训练项, 失败率)
            "buy": [],          # 每次购买 (类别, 名称, 价格)
            "note": 0,          # 出行拿到笔记的次数
            "note_probe": 0,    # 真点进步行街探笔记的次数（含没命中的）
            "sample_fail": 0,   # 训练项读数失败次数（0 帧有效）
            "stamina_drink": 0, # 喝精力药次数
            "scores": [],       # 每轮最终评分（训练评分页那个 8000/9000 的数）
        }

    @staticmethod
    def _merge_stat(dst, src):
        """把 src 累加进 dst（原地改 dst），用于「本轮 → 累计」。"""
        for k, v in (src.get("train") or {}).items():
            dst["train"][k] = dst["train"].get(k, 0) + v
        for k in ("gamble", "buy"):
            dst[k].extend(src.get(k) or [])
        for k in ("note", "note_probe", "sample_fail", "stamina_drink"):
            dst[k] = dst.get(k, 0) + (src.get(k) or 0)
        if src.get("scores"):
            dst.setdefault("scores", []).extend(src["scores"])

    def _rollover_stat(self):
        """一轮结束：本轮 stat 并入累计，再把本轮清零。

        摘要只在此后打印，所以「本轮运行摘要」只含这一轮真实发生的事；
        累计值单列一行，想看全局还有得看。
        """
        self._merge_stat(self.stat_total, self.stat)
        self.stat = self._new_stat()

    def _begin_round(self):
        """开始新的一轮之前，把「按天/按局」的状态清掉。

        不清会怎样：第 2 轮的第一天，`trip_probe_done` 还停在第 1 轮最后那天，
        商店 `shop_day` 也可能撞上，于是「探出行」「看货架」这些一天一次的动作
        整轮都被跳过。全局累计（买了几件、喝了几瓶）**故意不清** —— 那些是
        「每轮最多几次」的口径，跨轮累计才拦得住。
        """
        self.saw_training = False
        self.in_training = False
        self.phase = PHASE_HALL
        self.weekday = None
        self.item_checked = False
        self.read_fail_stop = False    # 训练项读数失败 → 主循环停机（玩家 2026-11-26）
        self._item_interrupt = None      # 读数被经理增益/剧情/遮罩打断时的页面名
        self.trip_picked = False
        self.trip_probe = False
        self.trip_probe_done = None
        self.trip_probe_retry = 0
        self.trip_probe_retry_day = None
        self.trip_back_tries = 0
        self.unknown_count = 0
        self.minigame_tries = 0
        self.stock_idx = 0
        self.stock_tries = 0
        self.stock_picked = False
        self.slots_autofill = 0
        self.slots_empty = 0
        self.slots_checked_page = None
        self.confirm_gray = 0
        self.main_char_round = None
        self.unknown_texts = []          # 未知页文字按轮清；已知浮层命中数全局累计到收工摘要
        self.transition_warns = []       # 页面转移存疑按轮清，收工摘要里汇总
        self.log_info(f"—— 开始第 {self.rounds_done + 1}/{self.max_rounds} 轮 ——")

    def _run_loop(self, max_steps, wait, max_rounds):
        last_key, repeat = None, 0
        last_page = None
        same_count = 0
        blind = 0
        blank = 0

        for step in range(1, max_steps + 1):
            self._last_step_at = time.time()
            self._ensure_foreground()
            # 断点：每 3 秒写一次（不是每步，省 IO）；异常停机时也只丢 3 秒内的进度
            if time.time() - getattr(self, "_resume_saved_at", 0) >= 3.0:
                self._save_state(step, last_page or "起点")
            frame = self._frame()
            if frame is None:
                blind += 1
                self.log_warning(f"取不到画面（{blind}/20），2 秒后重试（窗口最小化/显示器断电？）")
                if blind >= 20:
                    self.log_error("连续 20 次取不到画面，停止。请检查游戏窗口是否可见", notify=True)
                    break
                time.sleep(2)
                continue
            blind = 0

            items = self._texts(frame)
            # OCR 一段字都没读出来 = 画面还在切（白闪 / 渲染中）。
            # 这时候按「未知页」点屏幕中间，很可能点到还没画出来的按钮，重取一帧更安全。
            if not items:
                blank += 1
                self.log_info(f"画面还没渲染出文字（{blank}/8），稍后重取")
                if blank >= 8:
                    self.log_warning("连续 8 帧读不到任何文字，按未知页继续")
                    blank = 0
                else:
                    self.sleep(0.5)
                    continue
            else:
                blank = 0
            # 先判页面，再读资源：_read_stamina 要用页面上下文 hint 区分
            # 局内精力/局外专注（上限会变，不能再只靠数字指纹）。
            # _last_page_hint 给马尔可夫硬约束用：上一步是哪页，决定这一步哪些页不可能。
            self._last_page_hint = last_page
            page = self._classify(items, frame)
            self._read_stamina(items, frame, page=page)
            self._read_gold(items, frame)          # 顶栏金币，用于买东西时留 120 给笔记
            self.weekday = self._read_week(items)[1]
            # 页面级转移软约束：明显不可能的跳转记警告+截图，照常处理不拦截
            self._check_transition(last_page, page, frame=frame, step=step)
            # 事件页没有标题字，顶栏又带着「第N周」，很容易被认成每日页。
            # 成排黄卡片是它最硬的图像特征，所以在这里纠正一次。
            # 2026-11-23 21:34：二选一事件页（精力+30/体能+30）被 7.6 判成
            # 「行程结果」也会卡死，而结果类页面原本不在白名单 → 把「行程结果」
            # 加进来。真·行程结果页（涨幅飘字 + 剧情立绘）底部没有成排黄卡片，
            # _event_cards 返回空，不会误纠正。这里是视觉兜底，与 7.6b 的文字
            # 判定互为双保险：任一帧 OCR 读残或黄色比例抖动都能被另一条接住。
            if page in self.EVENT_CORRECT_PAGES and self._event_cards(frame, items):
                self.log_info(f"判定 {page} 但底部有成排黄卡片 → 按事件页处理")
                page = "剧情事件"
            if page != "未知":
                self.unknown_count = 0
            if page != "训练项页":
                self.item_checked = False
            # 离开炒股页就把计数和「已选卡」清掉，否则下一轮进选股页会残留 True，
            # 直点「确定」白费一步（08:26 实测）。
            if page != "炒股小游戏":
                self.stock_tries = 0
                self.stock_picked = False
            if page != "选项":
                self.choice_idx = 0
            # 离开选人三连就把「自动选择补空槽」的计数清掉，下一个页面还能再补一次
            if page not in ("选择队友", "笔记选择", "器材选择"):
                self.slots_autofill = 0
                self.slots_empty = 0
                self.slots_checked_page = None
                self.confirm_gray = 0

            key = (page, len(items))
            repeat = repeat + 1 if key == last_key else 0
            last_key = key
            same_count = same_count + 1 if page == last_page else 0
            if same_count == 0:
                self._stuck_tried = False   # 换页了就重置「卡死救过没」
            week, day = self._read_week(items)
            where = f"第{week}周 星期{day}" if week else ""
            self.log_info(f"--- 第 {step} 步 | 页面：{page} {where} | 精力 {self.stamina}/{self.stamina_max} "
                          f"| 局外专注 {self.out_focus}/{self.out_focus_max} | 文字 {len(items)} 段")

            if "笔记继承" in " ".join(i["t"] for i in items) and not self.notes_done:
                self.log_info("检测到「笔记继承」，标记本次已完成")
                self.notes_done = True

            # 局外体力不足弹窗：绝不点「购买」，直接取消
            joined = " ".join(i["t"] for i in items)
            if self._is_buy_popup(page, items):
                it = self._find(items, ["取消", "关闭", "稍后"])
                if it:
                    self.log_warning(f"体力不足弹窗，点「{it['t']}」不买")
                    self._click_item(it, "[拒绝购买]")
                else:
                    self.log_warning("体力不足弹窗，找不到取消按钮，点左上角关闭")
                    self.click_relative(0.02, 0.5)
                continue

            # 显式阶段推进：按页面查表，而不是「见过任何非大厅页就算开跑」。
            # 公告页/小游戏/纯浮层不在 PAGE_PHASE 里 → 不改阶段，
            # 这样在大厅弹个公告再关掉，不会被误当成训练已开跑、回大厅白计一轮。
            want_phase = PAGE_PHASE.get(page)
            if want_phase:
                self._set_phase(want_phase, page=page)

            result = True
            if page in ("专注恢复", "体力恢复"):
                self._do_focus(items, frame)
            elif page == "小游戏":
                if str(self.config.get("小游戏处理") or "放弃") == "暂停":
                    self.log_warning("进入小游戏，按配置暂停等你手动处理", notify=True)
                    self._shot(frame, f"minigame_{step}")
                    break
                result = self._do_minigame(items, frame)
            elif page == "公告页":
                result = self._do_notice(items, frame)
            elif page == "训练确认":
                result = self._do_train_confirm(items, frame)
            elif page == "炒股小游戏":
                result = self._do_stock(items, frame)
            elif page == "每日页":
                self._do_daily(items, frame)
            elif page == "大厅":
                # 只有「阶段确实进过局」才计一轮；停在大厅看公告/弹窗不算。
                if getattr(self, "phase", PHASE_HALL) in (PHASE_TRAINING, PHASE_SETTLING):
                    self.rounds_done += 1
                    self._set_phase(PHASE_HALL, page=page, reason="回大厅，完成一轮")
                    if self.rounds_done >= max_rounds:
                        self.log_info(f"★ 第 {self.rounds_done} 轮训练已跑完（共 {max_rounds} 轮），"
                                      f"回到大厅 —— 收工", notify=True)
                        self._shot(frame, "done_hall")
                        break
                    self.log_info(f"第 {self.rounds_done}/{max_rounds} 轮完成，"
                                  f"游戏不关，接着开下一轮", notify=True)
                    self._shot(frame, f"round{self.rounds_done}_done")
                    self._rollover_stat()   # 本轮并入累计、清零，下一轮从干净 stat 开始
                    self._begin_round()
                # 大厅右下角按钮实际写的是「训练中」（不是「训练」），
                # 且上次异常退出时点它会先弹「训练确认 → 继续训练」。
                it = self._find(items, ["训练中", "训练"], exact=True) or self._find(items, ["训练中"])
                if it:
                    self.in_training = True
                    self._set_phase(PHASE_TRAINING, page=page, reason="点进训练入口")
                    self._click_item(it, "[进入训练]")
                else:
                    # OCR 漏读「训练」两个字也不能就这么收工：大厅入口固定在右下角
                    # （1920x1080 实测 0.764, 0.883），按比例点一下。
                    self.log_info("大厅没读到「训练」文字，按固定坐标点右下角入口")
                    self.in_training = True
                    self._set_phase(PHASE_TRAINING, page=page, reason="按固定坐标点进训练")
                    hh, ww = frame.shape[:2]
                    self.click(int(ww * 0.764), int(hh * 0.883))
                    self.sleep(1.5)
            elif page == "赛前准备":
                self._do_pretrain(items, frame)
            elif page == "行程结果":
                self._do_next("行程结果")(items, frame)
            elif page == "抽奖":
                self._do_wheel(items, frame)
            elif page == "选项":
                self._do_choice(items, frame)
            elif page == "比赛MVP":
                self._do_next("比赛MVP")(items, frame)
            elif page == "结算":
                self._do_next("结算")(items, frame)
            elif page == "目标达成":
                self._do_next("目标达成")(items, frame)
            elif page == "行为卡":
                self._do_action_card(items, frame)
            elif page == "训练评分":
                self._capture_score(items, frame, page)
                self._do_next("训练评分")(items, frame)
            elif page == "恭喜获得":
                self._capture_score(items, frame, page)
                self._do_next("恭喜获得")(items, frame)
            elif page == "训练结束":
                # 这一屏有「前往主界面 / 编队 / 记录笔记 / 下一步」四个按钮。
                # 「训练结束」四个字是标题，不是按钮（22:44 连点 15 次标题，原地空转）。
                it = (self._find(items, ["前往主界面"]) or self._find(items, ["前往"])
                      or self._find(items, ["下一步"], exact=True)
                      or self._find(items, ["记录笔记"]))
                if it:
                    self._click_item(it, "[训练结束-回主界面]")
                elif any("注册球员" in str(i.get("t") or "") for i in items):
                    # 2026-11-24 09:01 实跑：这里叠了一张「注册球员」球员卡（闪堂电 分卫
                    # 评分6181），把「前往主界面/下一步/记录笔记」全挡住，点屏幕中间没用。
                    # 实测唯一出口是面板右上角 X。09:07 手动校准：X 在图内 (1513,95)，
                    # 窗口左边框 8px / 标题栏 31px → 客户区 (1505,64) → rel (0.941,0.071)。
                    # （第一次写成 0.929,0.100 差了 26px，点了 16 步没反应，别再调低。）
                    self.log_info("[训练结束] 注册球员卡片挡住按钮 → 点右上角 X 关闭")
                    self.click_relative(0.941, 0.071)
                else:
                    self.log_warning(f"训练结束页没有可点按钮，文字: {[i['t'] for i in items][:10]}")
                    self.click_relative(0.5, 0.5)
            elif page == "笔记学习":
                it = self._find(items, ["学习"], exact=True)
                if it:
                    self._click_item(it, "[学习笔记]")
                else:
                    self._click_confirm(items, frame)
            elif page == "扭蛋选择":
                self._do_egg_select(items, frame)
            elif page == "扭蛋机":
                self._do_egg_machine(items, frame)
            elif page == "经理增益":
                self._do_enhance(items, frame)
            elif page == "出行":
                self._do_trip(items, frame)
            elif page == "公园子页":
                self._do_trip(items, frame)
            elif page == "训练提议":
                self._do_train_offer(items, frame)
            elif page == "确认编队":
                self._do_confirm_team(items, frame)
            elif page == "前往":
                self._do_go(items, frame)
            elif page == "商店":
                self._do_shop(items, frame)
            elif page == "训练项页":
                self._do_training_core(items, frame)
                if self.read_fail_stop:
                    # 读数失败 → 玩家的要求是「停下来，你找到原因，再看」，
                    # 而不是拿默认项蒙着把这一天走完。
                    self.read_fail_stop = False
                    break
            elif page in ("点击继续", "本周总结"):
                # 本周总结是比赛周结算的滚动动画页（球队评分数字一直在变），
                # 没有明确按钮，和「点击屏幕继续」遮罩一样点一下推进即可。
                # 以前判未知页靠点屏幕中间，评分能滚完但白占十几步且不进日志。
                it = self._find(items, ["点击屏幕继续"]) or self._find(items, ["点击继续"])
                if it:
                    self._click_item(it, "[点击屏幕继续]")
                else:
                    self.click_relative(0.5, 0.86)
            elif page == "训练结果":
                it = self._find(items, ["点击屏幕继续"])
                if it:
                    self._click_item(it, "[训练结果-继续]")
                elif not self._click_confirm(items, frame):
                    # 兜底：开训前的「额外队友 / 选择队友」页顶部也写着「额外队友」，
                    # 会被 is_attr_panel 当成属性面板的 OCR 变体词而误判成训练结果。
                    # 这页真正的出口是右下角一个黄色「下一步」，不是屏幕正中央。
                    # 先找黄色主按钮点掉它；确实没有才退回点中间，避免连点空白 15 步卡死。
                    self.log_warning("结果页没找到「点击屏幕继续」也没有黄色主按钮，点屏幕中央兜底")
                    self.click_relative(0.5, 0.5)
            elif page in ("选择队友", "笔记选择", "器材选择"):
                # 先过「选满了没」这道闸门：有空白加号槽就补/停，绝不自己开训
                gate = self._slots_ready(page, items, frame)
                if gate == "pause":
                    break
                if gate == "wait":
                    result = True          # 已经点了自动选择，等下一帧再看
                    self.sleep(wait)
                    continue
                if not self._click_confirm(items, frame):
                    # 主按钮是灰的（BGR≈195,198,200）= 队伍 / 笔记 / 器材没配满。
                    # 玩家 2026-11-23 12:47：「如果有空的，你不要自己点自动选择，
                    # 而且停止并且弹出提示」→ 默认不再代为选择，直接停下弹窗。
                    # 只有配置 pick_slots.auto_fill_first: true 才保留旧行为。
                    slot_cfg = self._tuning().get("pick_slots") or {}
                    if slot_cfg.get("auto_fill_first", False):
                        auto = self._find(items, ["自动选择", "自动填充"], exact=True)
                        if auto:
                            self.log_info("主按钮是灰的（队伍没配满），先点「自动选择」再确认")
                            self._click_item(auto, "[自动选择]")
                            continue
                    self.confirm_gray += 1
                    if self.confirm_gray >= 3:
                        self.confirm_gray = 0
                        self._shot(frame, f"confirm_gray_{page}_{step}")
                        self._alert_popup(
                            "篮球少女：配队没满，已暂停",
                            f"「{page}」的「下一步」一直是灰色的，说明队伍 / 笔记 / 器材\n"
                            f"还没配满。\n\n"
                            f"脚本不会替你自动选择，已停下等你手动配好再运行。")
                        self.log_error(f"{page} 主按钮连续 3 次不是黄色（配队没满）→ "
                                       f"暂停等你手动配置", notify=True)
                        break
                    self.log_warning(f"{page} 主按钮不是黄色（配队没满），等一会儿再看 "
                                     f"（{self.confirm_gray}/3，我不会替你选）")
            elif page == "训练剧本":
                if not self._click_confirm(items, frame):
                    it = self._find(items, ["启程", "托管"])
                    if it:
                        self._click_item(it, "[强行点启程]")
                    else:
                        self.log_warning("训练剧本页找不到启程按钮")
                        self._shot(frame, f"script_{step}")
                        break
            elif page == "开始训练确认":
                self._do_next("开始训练确认")(items, frame)
            elif page == "确认编队":
                self._do_confirm_team(items, frame)
            elif page == "剧情事件":
                # 正常情况点「跳过」；连续 3 次没变化说明这页跳不过去（集训事件三选一之类），
                # 改成从中间的胶囊选项里挑一个。
                if same_count >= 3:
                    self.log_warning(f"「跳过」无效（连续 {same_count} 次同页），改选中间选项")
                    self._do_choice(items, frame)
                else:
                    self._do_story(items, frame)
            else:
                opts = self._yellow_options(frame, items)
                if opts:
                    self._do_event(items, frame, opts)
                    self.sleep(wait)
                    continue
                result = self._do_unknown(items, frame)

            if result is None:
                self._shot(frame, f"unknown_{step}")
                break

            # 注意：不能用 repeat —— 它比的是 (页面, 文字段数)，而文字段数每帧都在抖，
            # 永远累加不到阈值。改用「页面名连续没变」来判卡死。
            # 2026-11-23 15:34 实跑：每日页连点「训练」15 步页面不变，手动点一下就进——
            # 原因是窗口焦点被抢、pynput 点击被丢弃。现在 click() 每次都守前台，
            # 这里保留 4/8/12 步的强制置前作双保险（万一 _force_visible 那几帧没抢成功）。
            if same_count in (4, 8, 12):
                self.log_warning(f"页面「{page}」连续 {same_count} 步没变化，"
                                 f"强制把游戏窗口拉到前台再试（点击可能被丢弃）")
                self._force_visible()
            if same_count >= 15:
                # 先别急着停：让通用推进器（OCR 按钮 → 色块按钮 → ESC/右上X/中间，
                # 每次都验证画面变化）试一轮。已知页也可能因为弹窗叠层而推不动，
                # 这一层是「别一遇到没见过的叠层就断掉整轮」的兜底。
                if not getattr(self, "_stuck_tried", False):
                    self._stuck_tried = True
                    self.log_warning(f"页面「{page}」连续 {same_count} 步没变化 → "
                                     f"先交给通用推进器试一次")
                    if self._advance_generic(items, frame, page) is not None:
                        same_count = 0
                        self.sleep(wait)
                        continue
                self.log_error(f"页面「{page}」连续 {same_count} 步没变化，停下看看", notify=True)
                self._shot(frame, f"stuck_{step}")
                break
            last_page = page
            self.sleep(wait)

        self._print_summary()
        self._clear_state()          # 正常跑完才清断点；异常停机会留着下次接上
        self.log_info("本轮结束")
        return True
