"""自动训练：读屏 → 认页面 → 按规则选最优动作 → 点。

设计原则（来自实际踩坑）：
1. 每步都重新读屏判断，不写死点击序列 —— 训练过程有随机性，序列一定会错位。
2. 认不出页面就停（或按配置兜底），绝不盲点。
3. 每个决策都打日志，事后能复盘为什么选了它。

页面判定顺序很重要：越特殊、字数越独特的规则越靠前。
历史上翻车两次都是「通用兜底」抢在「具体页面」前面。
"""
import os
import re
import time

import numpy as np

from ok import Icon
import cv2

from src.core.cards import card_glow, find_cards
from src.tasks.MyBaseTask import MyBaseTask

# 主按钮候选，按优先级排列（越靠前越优先）
CONFIRM = ["开始训练", "确认编队", "确定", "决定", "确认", "启程", "下一步", "继续", "开始", "进入"]
SKIP = ["跳过", "点击屏幕继续"]
STOP_WORDS = ["关闭", "取消", "返回"]

WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "自动训练 / Auto"
        self.description = "自动跑一轮完整训练：训练 → 事件 → 加强训练 → 比赛 → 训练结束 → 回大厅"
        self.icon = Icon.SYNC
        self.default_config.update({
            "最大步数": 1500,
            "等待秒数": 1.3,
            "扭蛋目标关键词": "笔记",
            "扭蛋最高价": 120,
            "加强训练策略": "最高加成",
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
        # 局内精力（每日页 a/b，上限约 130）：训练/休息决策的唯一依据
        self.stamina = None
        self.stamina_max = None
        # 局外「专注」（顶栏 a/b，上限 150）：只决定「够不够开一次训练」，
        # 不参与训练/休息决策。2026-11-23 玩家明确：这是局外账号资源。
        self.out_focus = None
        self.out_focus_max = None
        self.weekday = None
        self.core_checked = False
        self.trip_picked = False
        # 「出行先探笔记，笔记 > 训练」（玩家 2026-11-23）：
        #   每日页同时有「训练」和「出行」时，先进出行页看地点标签里有没有笔记。
        #   有 → 直接出行；没有 → 退回每日页走训练。
        #   用「第N周-星期X」记今天已经探过，避免同一天反复进出。
        self.trip_probe = False          # 本次进出行页是「探笔记」，不是真要出行
        self.trip_probe_done = None      # 已经探过的那天，例如 "6-7"
        self.trip_probe_disabled = False # 出行页退不出去 → 以后不再探，直接按原顺序
        self.trip_back_tries = 0         # 本次退出出行页试了几次
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
        这里最多每 3 秒抢一次前台，避免每步都暴力 SetForegroundWindow。
        """
        now = time.time()
        if now - getattr(self, "_last_fg_try", 0.0) < 3.0:
            return
        try:
            import win32gui
            h = win32gui.FindWindow(None, "篮球少女：飞跃巅峰")
            if not h or win32gui.GetForegroundWindow() == h:
                return
        except Exception:
            return
        self._last_fg_try = now
        self.log_info("游戏窗口不在前台 → 点击会被丢弃，正在抢回前台")
        self._force_visible()

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

    def _shot(self, frame, tag):
        try:
            import cv2
            os.makedirs("screenshots/auto", exist_ok=True)
            p = f"screenshots/auto/{time.strftime('%H%M%S')}_{tag}.png"
            cv2.imwrite(p, frame)
            return p
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
        boxes = self.ocr(frame=frame) or []
        return [{"b": b, "t": (b.name or "").strip(),
                 "cx": (b.x + b.width / 2), "cy": (b.y + b.height / 2)} for b in boxes]

    def _find(self, items, words, exact=False):
        for it in items:
            for w in words:
                if (it["t"] == w) if exact else (w in it["t"]):
                    return it
        return None

    def _all(self, items, pat):
        rx = re.compile(pat)
        return [it for it in items if rx.search(it["t"])]

    def _yellow_btn_center(self, frame, it):
        """在文字框周围找**真实的黄色按钮矩形**，返回其中心（找不到返回 None）。

        2026-11-23 实测（行为卡页「请选择1个技能」）：OCR 给「确定」的框中心是 (1567,978)，
        而按钮实体在 y 980~1052、中心 (1583,1016) —— 照着 OCR 坐标点会落在按钮上方空白处。
        这一页连点 15 次都没反应，最后靠「连续 15 步无变化」停机。
        所以凡是"黄底大字"的主按钮（确定/决定/开始训练…），一律以黄色矩形为准。
        """
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1 = max(int(it["cx"] - 0.30 * w), 0)
        x2 = min(int(it["cx"] + 0.30 * w), w)
        y1 = max(int(it["cy"] - 0.06 * h), 0)
        y2 = min(int(it["cy"] + 0.14 * h), h)
        patch = frame[y1:y2, x1:x2]
        if patch.size == 0:
            return None
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, np.array((16, 110, 160)), np.array((42, 255, 255)))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            if bw < 0.08 * w or bh < 0.025 * h:
                continue
            if not (2.5 <= bw / float(bh) <= 14):
                continue
            if best is None or bw * bh > best[0]:
                best = (bw * bh, bx, by, bw, bh)
        if not best:
            return None
        _, bx, by, bw, bh = best
        return (bx + x1 + bw / 2.0, by + y1 + bh / 2.0)

    def _click_item(self, it, why=""):
        self.log_info(f"点击「{it['t']}」@({int(it['cx'])},{int(it['cy'])}) {why}")
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

    def _find_empty_slots(self, frame, cfg=None):
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
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            if not (min_h <= bh <= max_h):
                continue
            if not (0.72 < bw / bh < 1.35):
                continue
            if cv2.contourArea(c) / float(bw * bh) < 0.35:
                continue
            # 内部中间 46%：必须是近白
            ix, iy = x + int(bw * 0.27), y + int(bh * 0.27)
            ip = hsv[iy:iy + int(bh * 0.46), ix:ix + int(bw * 0.46)]
            if ip.size == 0:
                continue
            white = float(((ip[:, :, 1] < 45) & (ip[:, :, 2] > 215)).mean())
            if white < min_white:
                continue
            # 内部还得留着黄色十字
            cx, cy = x + int(bw * 0.35), y + int(bh * 0.35)
            cp = hsv[cy:cy + int(bh * 0.30), cx:cx + int(bw * 0.30)]
            if cp.size == 0:
                continue
            cross = float((cv2.inRange(cp, (15, 80, 120), (40, 255, 255)) > 0).mean())
            if cross < 0.08:
                continue
            out.append(dict(cx=(x + x0 + bw / 2.0) / w, cy=(y + y0 + bh / 2.0) / h,
                            w=bw / float(w), h=bh / float(h),
                            white=round(white, 2), cross=round(cross, 2)))
        out.sort(key=lambda d: d["cx"])
        return out

    def _slots_ready(self, page, items, frame):
        """开训前选人页的「选满了没」闸门。

        有空槽（金色框 + 白底 + 黄加号）= 还有东西没选：
          1) 先点一次「自动选择」（多数情况游戏会替你配满）
          2) 再查一遍，还是空 → 截图 + 通知 + 暂停，**绝不点下一步**
             （玩家明确要求：没选满就不要自己开始训练）
        返回 "ok" / "wait"（刚点完自动选择，下一帧再看）/ "pause"（要停下等人工）。
        """
        cfg = self._tuning().get("pick_slots") or {}
        if cfg.get("check") is False:
            return "ok"
        pages = cfg.get("pages") or ["选择队友", "笔记选择", "器材选择"]
        if page not in pages:
            return "ok"

        slots = self._find_empty_slots(frame, cfg)
        self.slots_empty = len(slots)
        if not slots:
            return "ok"

        pos = ", ".join(f"({s['cx']:.2f},{s['cy']:.2f})" for s in slots)
        self.log_warning(f"{page}：发现 {len(slots)} 个空槽（还没选）@ {pos}")
        self._shot(frame, f"empty_slots_{page}")

        if cfg.get("auto_fill_first", True) and self.slots_autofill < int(cfg.get("max_auto_fill") or 1):
            auto = self._find(items, ["自动选择", "自动填充"], exact=True)
            if auto:
                self.slots_autofill += 1
                self.log_info(f"{page}：还有空槽，先点「自动选择」补满（第 {self.slots_autofill} 次）")
                self._click_item(auto, "[自动选择-补空槽]")
                self.sleep(1.6)
                return "wait"  # 下一帧重新判断

        # 自动选择也补不上（比如「剩余0次」）→ 停在这里等人工
        if cfg.get("on_empty", "暂停提示") == "暂停提示":
            self._shot(frame, f"pause_empty_{page}")
            self.log_error(f"{page} 还有 {len(slots)} 个空槽没选（自动选择也补不上），"
                           f"已暂停，请手动选完或改配置后再继续", notify=True)
            return "pause"
        self.log_warning(f"{page} 有空槽，但配置是继续，按原规则点下一步")
        return "ok"

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

    def _classify(self, items, frame=None):
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
        # 1.5 公园子页：底部也挂着六项技能面板（投射/弹跳/…），
        #     不先拦就会在下面被「右侧竖排技能 ≥4」认成训练核心 →
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
        #     「右侧技能 ≥4」当成训练核心 → 对着不存在的技能胶囊猛点，
        #     采样 0/5 帧、整轮作废。必须在训练核心之前拦下来。
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
        if "契合度" in joined:
            return "笔记学习"
        if "请选择1个扭蛋" in joined:
            return "扭蛋选择"
        if has("超级扭蛋机", "转动扭蛋"):
            return "扭蛋机"
        if has("请选择一种加强训练", "加强训练成功"):
            return "加强训练"
        if "上新时间" in joined:
            return "商店"
        # 5.9 开训前的选人三连。文案独特，必须排在大厅和「训练核心」之前
        if "选择队友" in joined:
            return "选择队友"
        if "选择器材" in joined:
            return "器材选择"
        if "选择笔记" in joined:
            return "笔记选择"
        if "确认编队" in joined or "活动加成总计" in joined:
            return "确认编队"
        if "开始训练" in joined:
            return "开始训练确认"
        # 6. 训练结果（有独特词，先判；否则会被下面的「核心页」按技能名抢走）
        if has("属性提升了", "精力降低", "触发训练后事件", "器材+", "新境界"):
            return "训练结果"
        # 7. 训练核心：**右侧竖排**技能胶囊 ≥4 个才算。
        #    不能依赖「决定」二字 —— 它是黄底白字，OCR 时有时无。
        #    也不能只看「屏幕上出现 ≥4 个技能名」—— 公园/出行页底部挂着一整块
        #    队伍属性面板（投射 B 447/1784 …），会把它误判成训练核心，
        #    然后对着公园页猛点「决定」坐标空转到卡死（2026-11-23 06:11 实测）。
        w = frame.shape[1] if frame is not None else 1920
        right_skills = {it["t"].replace(" ", "") for it in items
                        if it["cx"] > w * 0.72 and it["t"].replace(" ", "") in self.SKILLS}
        if len(right_skills) >= 4 or (exact("决定") and has("失败率")):
            return "训练核心"
        # 7.35 开训动画（只有「训练开始」一行字）
        if "训练开始" in joined:
            return "训练结果"
        # 7.4 训练后的属性成长面板（心·技·体 + 六个属性条 + 倍率加成）
        #     它不是可交互页，点一下/下一步就过；判成「未知」会走兜底连点中间，
        #     一轮下来能占十几步（实测未知页 23% 里一大半是它）。
        if "心·技·体" in joined and has("投射", "弹跳", "运控", "体能", "防守", "意识"):
            return "训练结果"
        # 7.5 出行/工资之类的结果屏：成功！+ 获得 + 下一步（顶栏也有第N周，会误判成每日页）
        if has("成功！", "成功!") and has("获得", "工资") and exact("下一步"):
            return "行程结果"
        # 7.9 居中竖排胶囊选项（集训事件三选一这类）：先认出来再选，
        #     不必先白白点 3 次「跳过」等 same_count 攒够（每次约 5 秒）。
        if frame is not None and "跳过" in joined and len(self._pills(items, frame)) >= 2:
            return "选项"
        # 8. 剧情 / 事件（有「跳过」的大多是对话页；事件页另有黄卡片兜底）
        if exact("跳过") or "跳过" in joined:
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
        st = self.stamina if self.stamina is not None else 999
        enough = st >= stamina_min
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

        if train and enough:
            # 玩家 2026-11-23：「出行看有没有笔记，大于训练」。
            # 今天还有训练可打，但先把出行页点开瞄一眼地点标签 ——
            # 有笔记就直接出行（笔记 tier 最高），没笔记 _do_trip 会退回这里走训练。
            # 每天只探一次（trip_probe_done 记账），退不出去会自动永久关掉这条路。
            day_key = self._day_key(items)
            if go and self._can_probe_trip(day_key):
                self.trip_probe = True
                self.trip_probe_done = day_key
                self.trip_back_tries = 0
                self._click_btn(go, f"[探出行-看有没有笔记 精力{self.stamina}/{self.stamina_max}]", frame)
                return True
            self._click_btn(train, f"[训练 精力{self.stamina}/{self.stamina_max}]", frame)
            return True

        # 精力不足：先看能不能去公园（唯一能明显回精力的地方）
        if go and not enough:
            self._click_btn(go, f"[精力{self.stamina}<{stamina_min}-出行去公园补精力]", frame)
            return True

        if rest:
            self._click_btn(rest, f"[休息 精力{self.stamina}<{stamina_min}，休息不补精力只推一天]", frame)
            return True

        if train:  # 没别的路，先点训练（不足会弹专注恢复，那边有兜底）
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
        ok = self._find(items, ["确定", "确认"], exact=True)
        if ok and self._is_yellow_btn(frame, ok):
            spot = self._yellow_btn_center(frame, ok)
            if spot:
                self.log_info(f"点击「确定」@({int(spot[0])},{int(spot[1])}) [按黄色按钮定位]")
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
        cards = [it for it in items
                 if 0.12 < it["cy"] / h < 0.80
                 and 2 <= len(it["t"]) <= 8
                 and it["t"] not in skip
                 and not re.match(r"^[\d/\.\+%★]+$", it["t"])]
        if not cards:
            self.log_warning(f"行为卡页没找到可选项，文字: {[i['t'] for i in items][:14]}")
            self.click_relative(0.5, 0.5)
            return True
        # 玩家偏好：行为卡（无惧重围）> 得分手 > 其它，没偏好就取最右一张
        prefer = ["无惧重围", "看破擅长", "看破", "得分手"]
        best = None
        for p in prefer:
            best = next((c for c in cards if p in c["t"]), None)
            if best:
                break
        best = best or cards[-1]
        # 注意：OCR 读到的是卡片左上角的「名称标签」，那层标签点不动（22:14 连点 8 次无反应），
        # 要点卡片主体，所以往下偏移到卡面中部。
        x = best["cx"]
        y = min(best["cy"] + h * 0.20, h * 0.72)
        self.log_info(f"选行为卡「{best['t']}」→ 点卡面 @({int(x)},{int(y)})")
        self.click(x, y)
        self.sleep(0.8)
        return True

    def _do_egg_select(self, items, frame):
        """扭蛋三选一：只买目标关键词（笔记继承），且价格不超上限。"""
        kw = str(self.config.get("扭蛋目标关键词") or "笔记").split(",")
        kw = [k.strip() for k in kw if k.strip()]
        cap = int(self.config.get("扭蛋最高价") or 120)

        if self.notes_done:
            self.log_info("本轮已经拿过笔记继承，这次不买")
            return self._leave(items, frame)

        cands = [it for it in items if any(k in it["t"] for k in kw)]
        cands = [c for c in cands if 0.2 < c["cy"] / frame.shape[0] < 0.55]
        if not cands:
            self.log_info(f"三个扭蛋里没有「{kw}」，跳过不买")
            return self._leave(items, frame)

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
            return self._leave(items, frame)

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
        return self._leave(items, frame)

    def _do_enhance(self, items, frame):
        """加强训练三选一：优先最高加成。"""
        if self._click_confirm(items, frame):
            return True
        cands = self._all(items, r"\+\s*\d{2,}")
        cands = [c for c in cands if 0.2 < c["cy"] / frame.shape[0] < 0.8]
        best = None
        if cands:
            best = max(cands, key=lambda c: _num(c["t"]) or 0)
            self.log_info(f"加强训练候选项: {[c['t'] for c in cands]}")
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
    SYMBOL_GAIN = {"精力": (30, 70), "属性": (20, 40)}

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
            small, big = self.SYMBOL_GAIN.get(key, (1, 2))
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
        """统一的奖励优先级：笔记 > 技能 > 未溢出的精力 > 属性 > 零用钱。

        返回可直接比较的元组 (tier, eff, at, money)：
          tier 4 笔记 / 3 技能 / 2 能补进精力条 / 1 属性 / 0 其他
          eff  实际能吃进精力条的点数（**超出上限的部分不算**）
          at   属性点数

        「未溢出」是玩家 2026-11-23 06:43 专门点出来的：
          精力只差 21 时，+70 和 +30 都只能吃进 21，多出来的 49 是白扔的，
          两档精力打平，就该选那个还能顺带加属性的 +30 档。
        以前直接拿标签上的原始数值当收益（+70 永远赢），所以才会挑错。
        """
        t = (text or "").replace("＋", "+").replace(" ", "")
        money = 1 if any(k in t for k in ("零用钱", "金钱", "金币")) else 0

        # 概率型奖励一律不要（玩家 2026-11-23 明确）
        if re.search(r"\d+\s*%", t) and bool(
                (self._tuning().get("event") or {}).get("reject_probability", True)):
            return (0, 0, 0, money)

        if "笔记" in t:
            return (4, 0, 0, 0)
        if any(k in t for k in self.SKILL_WORDS):
            return (3, 0, 0, 0)

        gap = self._gap()
        st = self._gain(t, "精力")
        at = self._gain(t, "属性")
        if not at and any(k in t for k in self.ATTR_WORDS):
            at = 1                    # 有属性加成但读不到数值，至少别当 0
        eff = min(st, gap) if gap > 0 else 0
        if eff > 0:
            return (2, eff, at, 0)
        if at > 0:
            return (1, 0, at, 0)
        return (0, 0, 0, money)

    def _key_value(self, key):
        """把 _reward_key 的元组压成一个数值，方便沿用旧的「取最大值」写法。"""
        tier, eff, at, money = key
        return tier * 10000 + eff * 10 + at + money * 0.1

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

    def _can_probe_trip(self, day_key):
        """今天还能不能为「看笔记」去点出行。"""
        if self.trip_probe_disabled or not day_key:
            return False
        return self.trip_probe_done != day_key

    def _want_shop(self, items):
        """今天要不要进商店看一眼。

        商店现在不只买精力药了 —— 特训牛奶 / 金色秘籍 / 器材暴击 / 0 元馈赠都值得买
        （wiki 训练流口径）。但每天都绕一趟要白烧 2~3 步，所以只在两种时候进：
          1) 精力 < 50：缺精力，得看有没有 50 元以下的饮料；
          2) 换周了：商店「上新时间 7天后」，一周才刷一次货，例行进去把该买的买齐。
        按天记账，一天最多进一次，避免每日页 ↔ 商店来回打转。
        """
        shop_cfg = self._tuning().get("shop") or {}
        if not self.config.get("商店自动购买", True):
            return False
        day = self._day_key(items)
        if self.shop_day is not None and self.shop_day == day:
            return False
        if self.shop_bought >= int(shop_cfg.get("max_buy_per_run", 4)):
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
        """周末出行：笔记 > 技能 > 未溢出的精力 > 属性 > 零用钱，游戏厅排除。

        精力来源只有事件和公园（玩家 2026-11-23 明确），所以：
          * 精力没满 → 公园里挑能真正吃进精力条的档位；
          * 精力已满 → 补精力不值钱，改挑「属性+」。

        2026-11-23 06:43 玩家实测纠错：精力只差 21 时，脚本挑了「精力+70」，
        而同时存在「精力+30 且属性+」的选项 —— 那 49 点溢出是白扔的。
        现在统一走 `_reward_key`：先把精力按缺口截断，打平再看属性。
        """
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
            notes = [n for n in names if "笔记" in self._place_tag(n, items, frame)]
            if notes:
                self.log_info(f"出行有笔记 {[n['t'] for n in notes]} → 笔记优先，本次出行而不是训练")
            else:
                self.log_info("出行页没有笔记 → 退回每日页走训练")
                self.trip_back_tries += 1
                return self._leave(items, frame)
        elif self.trip_back_tries > 0 and self._day_key(items) == self.trip_probe_done:
            # 上一帧刚点过「退出」，这一帧还在出行页 → 这页根本退不出去。
            # 不再折腾：永久关掉探路，按原规则直接出行（否则会卡在这一页空转）。
            self.trip_back_tries = 0
            self.trip_probe_disabled = True
            self.log_warning("出行页退不出去（点过退出仍在原页）→ 以后不再探笔记，直接按原规则出行")

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
            order = self.PARK_FALLBACK if in_park else self.TRIP_FALLBACK
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
            if rule.get("on", True) is False:
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
        """在 frame 里找 assets/templates/<name>（多尺度），返回相对坐标 (rx, ry)。"""
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            tpl = cv2.imread(os.path.join(root, "assets", "templates", name))
            if tpl is None or frame is None:
                return None
            h, w = frame.shape[:2]
            th, tw = tpl.shape[:2]
            best = None
            for s in (0.85, 1.0, 1.15):
                t = cv2.resize(tpl, (max(8, int(tw * s)), max(8, int(th * s))))
                if t.shape[0] >= h or t.shape[1] >= w:
                    continue
                res = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if best is None or mx > best[0]:
                    best = (mx, loc[0] + t.shape[1] / 2.0, loc[1] + t.shape[0] / 2.0)
            if not best or best[0] < threshold:
                return None
            self.log_info(f"模板 {name} 命中 score={best[0]:.2f}")
            return (best[1] / w, best[2] / h)
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

    # 两种资源的上限指纹（用来在 OCR 吃掉「/」时反推）
    CAPS = (150, 130)      # 局外体力 / 局内精力
    FOCUS_CAP = 150
    STAMINA_CAP = 130

    def _ab_kind(self, a, b):
        """判断这个 a/b 是哪种资源；不是已知资源就返回 None。

        只认「贴着已知上限」的 b（允许 OCR ±8 的误差）。
        2026-11-23 06:51 实测：不加这个校验，页面上随便一个 218/271、303/164
        都会被当成体力写进 `out_focus`，日志里 221/150 和 303/164 交替出现。
        """
        if 143 <= b <= 158:
            return "focus"
        if 122 <= b <= 138:
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
        m = re.search(r"(\d{1,3})/(\d{2,3})", s)
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

    def _read_stamina(self, items, frame):
        """分别读两种资源 —— 完全不同的东西，绝不能混用。

        资源对照（2026-11-23 玩家两次当面纠正 + MCP 探针实测）：
          * **局内「精力」上限 130**：每日页顶栏 ⚡ 38/130。训练消耗它，
            决定「今天训练还是休息」；**只有局内事件(精力+10)和周日公园
            (+30/+70) 能补，药水补不了它**。
          * **局外「专注」上限 150**：账号级体力，不够就开不了训练，
            弹「专注恢复」时用药水补（不用钻石）。不参与训练/休息决策。

        判定方式：**按上限区分，不能按位置**。
        两个数字都出现在顶栏区域，光看 y 坐标会读反（130 被当成专注）。
        150 和 130 这两个上限目前稳定，用它们当指纹：
          b >= 145 → 局外专注
          40 <= b <= 140 → 局内精力
        """
        h = frame.shape[0]
        for it in items:
            ab = self._parse_ab(it["t"])
            if not ab:
                continue
            a, b = ab
            # 体力可以溢出（202/150 是实测见到的真实状态），所以不能要求 a <= b
            if not (0 <= a <= b * 2):
                continue
            kind = self._ab_kind(a, b)
            if kind == "focus":
                self.out_focus, self.out_focus_max = a, self.FOCUS_CAP
            elif kind == "stamina":
                # 上限固定 130，不用读到的 b —— OCR 有时读成 134/136，
                # 一旦把 134 当成上限，后面读到 130 就再也更新不了当前值
                # （2026-11-23 06:51 实测：精力卡在 108 不动）。
                self.stamina, self.stamina_max = a, self.STAMINA_CAP

    def _orange_spots(self, frame):
        """右侧技能列上的黄色小圆标：找橙色像素团的中心。"""
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
    # 被训练的「主力」，他自己不算队友（录制 122 张训练核心帧里 119 张都有她）
    # 只用于识别**粘连词**（「百希雅」=百目鬼+希雅，含主力名且超 2 字 → 判为拼接产物）。
    # 2026-11-23 起不再用它从队友里剔人：主训每局会换，靠 _resident_names 自动判。
    MAIN_CHAR = "希雅"
    # 场上角色的口径（玩家 2026-11-23 定稿）：
    #   「经理也是算作队友的」，而且训练核心页根本没有「替补 / 解说」这类标签，
    #   所以**场上站着的都算队友**（含经理、含主训本人），不做任何名字级剔除。
    #   恒在场的角色对每个技能都是 +1，不影响排名；剔掉反而会因为某帧 OCR
    #   漏读而让那个技能少算一分。只保留通用护栏（技能名 / 粘连词 / 加成浮层词）。
    # 出现在球场上但明显不是人名的词：加成浮层条目 + 出行地点名
    MATE_STOP_WORDS = ("加成", "队友", "表情", "倍率", "一致", "额外", "组合", "概率",
                       "上限", "游戏厅", "公园", "餐厅", "步行街", "散步", "野餐", "划船")

    def _badge_near(self, frame, row):
        """技能胶囊右上角那枚橙色小人角标；没有就返回 None。

        尺寸自适应（2026-11-23）：原来用的是 1920x1080 下标出来的绝对像素
        （偏移 40/190/110/25、角标 16~52px、面积 260）。玩家把窗口拖小或改成
        1600x900 之后，角标同比缩小，下限 16px 会把最小的角标漏掉 → 明明有角标
        却判成「没有」。现在统一按 1920x1080 折算成比例再乘回当前帧尺寸。
        """
        h, w = frame.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        b = row["b"]
        x1 = max(int(b.x + b.width - 40 * sx), 0)
        x2 = min(int(b.x + b.width + 190 * sx), w)
        y1 = max(int(b.y - 110 * sy), 0)
        y2 = min(int(b.y + 25 * sy), h)
        if x2 <= x1 or y2 <= y1:
            return None
        roi = frame[y1:y2, x1:x2]
        bb = roi[:, :, 0].astype(int)
        gg = roi[:, :, 1].astype(int)
        rr = roi[:, :, 2].astype(int)
        m = ((rr > 195) & (gg > 105) & (gg < 195) & (bb < 95)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            if not (16 * sx <= bw <= 52 * sx and 16 * sy <= bh <= 52 * sy):
                continue
            if not (0.6 < bw / max(bh, 1) < 1.7):
                continue
            if bw * bh < 260 * sx * sy:
                continue
            if best is None or bw * bh > best[0]:
                best = (bw * bh, (x1 + bx + bw / 2, y1 + by + bh / 2))
        return best[1] if best else None

    def _court_names(self, items, frame):
        """球场上站着的角色名（队友 + 主力），名字是 2~5 个字的小标签。

        2026-11-23 实测坐标（1920x1080，核心页）：球队经理(364,443)、真崎(555,448)、
        百目鬼(684,441)、希雅(888,455)，都落在 y≈0.41 这一行。
        「球队经理」是场边常驻角色，不是队友，必须排除，否则每个技能都白送 1 分。
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

        加成浮层也要防：05:29 实测点开技能时偶尔会弹出左侧那四张加成卡，
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
        """训练核心页的「决定」：**不判颜色**，OCR 读不到就按坐标点。

        实测（22:33，第4周技能全 MAX）：这时的「决定」是白底按钮
        BGR≈(152,212,211)，被「只有黄色才点」的保护挡掉，结果在核心页空转 15 步。
        又实测（2026-11-23 05:19）：黄底白字的「决定」OCR 有时整块读不出来，
        exact 匹配失败 → 页面判定和点击一起失灵。所以补一个坐标兜底。
        """
        it = self._find(items, ["决定"], exact=True)
        if it:
            self._click_item(it, "[决定-开训]")
            return True
        if self._click_confirm(items, frame):
            return True
        # 右下角「决定」按钮实测位置（1080p：x≈0.926、y≈0.83）
        self.log_warning("没读到「决定」文字，按固定坐标点右下角")
        self.click_relative(0.926, 0.831)
        return True

    def _do_training_core(self, items, frame):
        """训练核心页：只点右上角带角标的技能 → 读队友 / 器材 / 失败率 → 打分 → 决定。

        每轮都从画面上重新认「主训人员」（每个技能都在场的名字），不写死名字。
        """
        h, w = frame.shape[:2]
        rows = [it for it in items if it["t"] in self.SKILLS and it["cx"] / w > 0.70]
        rows.sort(key=lambda x: x["cy"])
        if not rows:
            self.log_warning("训练核心页没找到技能胶囊，直接决定")
            return self._decide(items, frame)

        if self.core_checked:
            return self._decide(items, frame)
        self.core_checked = True

        cfg = self._tuning().get("core_score", {}) or {}
        targets = []
        for r in rows:
            spot = self._badge_near(frame, r)
            if spot:
                targets.append((r, spot))
                self.log_info(f"「{r['t']}」右上角有角标 @({int(spot[0])},{int(spot[1])})，点开检查")
            else:
                self.log_info(f"「{r['t']}」没有角标，跳过")
        if not targets:
            self.log_warning("六个技能都没有角标，不逐项检查，直接决定")
            return self._decide(items, frame)

        scored = []
        for idx, (r, _spot) in enumerate(targets, 1):
            self.log_info(f"—— 点开「{r['t']}」（{idx}/{len(targets)}）看编队")
            self._click_item(r, "[展开编队]")
            # 选中态会一直保留，起手等 0.4s 就够（由 _sample_core 的 warmup 控制）
            names, pct, level, equips = self._sample_core(r)

            # 页面守卫：采样途中页面可能已经变了（实测第 6 周点开「体能」时，
            # 画面已经跳到「器材事件」，后面 3 步全在对着事件页点技能 + 点决定）。
            # 一旦不是训练核心页就立刻交回主循环，别把后续技能也点一遍。
            f_chk = self._frame()
            if f_chk is not None:
                now_page = self._classify(self._texts(f_chk), f_chk)
                if now_page != "训练核心":
                    self.log_warning(f"采样途中页面已变成「{now_page}」→ 放弃本次编队比较，交给主循环")
                    return True

            if not names and pct is None and level is None:
                # 2026-11-23 实测：「意识」这一档常点了没反应（选中的还是上一个技能），
                # 因为它贴在最底下、离属性条太近。往上挪 26px 重点一次再读。
                self.log_warning(f"「{r['t']}」没读到编队（可能点偏了），往上挪 26px 重点一次")
                self.click(r["cx"], r["cy"] - 26)
                names, pct, level, equips = self._sample_core(r)
            f2 = self._frame()
            if f2 is not None and self.config.get("保存截图"):
                self._shot(f2, f"core_{idx}")
            # 三项全空 = 这次采样整个失败了（取帧全 None / 画面还在渲染），
            # 不能当成「0 队友 0 分」参与排名，否则会把本来最好的选项排到最后。
            valid = not (level is None and pct is None and not names)
            scored.append({"row": r, "pct": pct, "mates": 0, "level": level,
                           "valid": valid, "names": set(names), "score": 0.0,
                           "equips": equips})

        if not scored:
            self.log_warning("带角标的技能都没读到编队，按当前默认项决定")
            return self._decide(items, frame)

        # 记下这一场器材最多有几张 —— 商店那边「器材暴击道具只有器材上站人才买」
        # 要用它（wiki 原话）。商店页看不到器材，只能靠训练核心页这一眼。
        seen_eq = max([len(d.get("equips") or []) for d in scored] or [0])
        if seen_eq:
            self.last_equip_count = seen_eq

        usable = [d for d in scored if d["valid"]]
        if usable:
            if len(usable) < len(scored):
                self.log_warning(f"{len(scored) - len(usable)} 个技能采样失败，已排除在排名外")
            scored = usable
        else:
            self.log_warning("全部技能采样都失败，按当前默认项决定")
            return self._decide(items, frame)

        # 主训人员：出现在**每个**技能里的那个名字（玩家：不能写死名字）。
        #   ・只用来「知道这轮主训是谁」并打进日志，**不从队友里剔除** ——
        #     玩家同时要求「经理也算队友」，而恒在场的名字对每个技能都是 +1，排名不受影响。
        main_char = self._main_char_of_round(scored, cfg)
        if main_char:
            self.main_char_round = main_char
            self._remember_main_char(main_char)
            self.log_info(f"本轮主训人员：{main_char}（每个技能都在场，按队友计入）")
        else:
            self.log_warning("没认出主训人员（只采到一个技能 / OCR 漏读），按场上名字直接算队友")

        # ===== 失败率硬门槛（玩家 2026-11-23：手动打从来不失败）=====
        scored = self._apply_fail_limit(scored, cfg)

        for d in scored:
            real = set(d["names"])          # 经理 / 主训 / 队友全算（玩家 2026-11-23）
            d["mates"] = len(real)
            d["score"] = self._score_core(sorted(real), d["level"], d["pct"], cfg,
                                          d.get("equips"))
            eq = d.get("equips") or []
            self.log_info(f"「{d['row']['t']}」{d['level'] or '?'} 失败率={d['pct']}% "
                          f"队友{d['mates']}{sorted(real)} 器材{len(eq)}{eq} "
                          f"→ 得分 {d['score']:.2f}")

        # ===== 意识加权 = 精力泵（玩家 2026-11-23）=====
        # 意识训练**不消耗精力、反而小幅回精力**，而且是所有技能里成功率最高的。
        # 所以精力见底时它不是「次优解」，而是唯一能让「周一到周六每天都训练」成立的选择。
        # 精力充足时不加权，避免抢掉正常成长。
        self._apply_consciousness_boost(scored, cfg)

        scored.sort(key=lambda d: (-d["score"],
                                   d["pct"] if d["pct"] is not None else 999))
        self.log_info("编队排名：" + " | ".join(
            f"{d['row']['t']} {d['score']:.2f}分(队友{d['mates']} 器材{len(d.get('equips') or [])}"
            f"/{d['level'] or '?'}) 失败率{d['pct']}%"
            for d in scored))

        # 玩家规则（2026-11-23）：
        #   能练的技能失败率**全部**高于阈值时，练下去就是白扔精力；
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
                             f"——采样可能不全，这一手不算决策")
        # 选中项带失败率 → 明确打一条「赌」的日志，事后复盘输赢用
        elif best.get("pct"):
            alt = [d for d in scored if d is not best and not d.get("pct")]
            self.log_warning(f"赌一把：「{best['row']['t']}」失败率 {best['pct']}%，"
                             f"期望得分仍排第一（{best['score']:.2f}）"
                             + (f"；稳的备选 {[(d['row']['t'], round(d['score'], 2)) for d in alt]}"
                                if alt else "；今天没有 0% 的稳选项"))

        self._click_item(best["row"], f"[最优编队 {best['row']['t']} 得分{best['score']:.1f}]")
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

    def _main_char_of_round(self, scored, cfg):
        """本轮主训人员是谁 —— 出现在**每个**技能里的那个名字。

        玩家 2026-11-23 两条要求：
          1.「每轮训练你要知道主训人员是谁，不能写死名字」
             主训每局会换（上一局希雅、这一局闪堂），所以按「每个技能都在场」自动判。
          2.「经理也是算作队友的」
             所以判出来**只记录、不剔除**；恒在场 = 对每个技能都 +1，排名不受影响。

        判不出来（只采到一个技能、或 OCR 漏读）就返回 None，不猜。
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
        """失败率全高于阈值 + 商店没精力道具 → 改练指定技能（默认「意识」）。"""
        force = cfg.get("force_when_risky") or {}
        if not force.get("enabled", True):
            return None
        thr = int(force.get("fail_over", 30))
        want = str(force.get("skill", "意识"))
        if any(d["pct"] is None for d in scored):
            # 有技能没读到失败率 → 不能断定「全部高风险」，那条未读到的可能正是安全项
            self.log_info("有技能没读到失败率，无法断定全部高风险 → 按原排名")
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

    def _sample_core(self, row, frames=6, gap=0.18, warmup=0.35, max_tries=10,
                     min_frames=4):
        """点开技能后等 0.4 秒（选中态出来），再连拍几帧投票成稳定值。

        2026-11-23 10:14 CoreSweepTask 逐帧扫查的结论（玩家提示「不用特定时间窗口」）：
            点完技能后**选中态会一直保留**，不是一闪而过的浮层：
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
        # 器材：训练核心页中部会摆 1~3 张卡（玩家：「中间一个器材（鞋子），不是很明显吗」），
        # 卡面是这次训练带的支援单位，外框金/彩光 = 品质。选中态一直保留，所以每帧都能读。
        equips = []
        used = 0
        tries = 0
        bad_streak = 0
        while used < frames and tries < max_tries:
            tries += 1
            f = self._frame_retry()
            if f is None:
                self.sleep(gap)
                continue
            its = self._texts(f)
            hits = len({it["t"].replace(" ", "") for it in its
                        if it["t"].replace(" ", "") in self.SKILLS})
            # 浮层盖住右列时 hits 会掉到 0~1，所以「还像训练核心页」不能只看技能名：
            # 技能名 ≥1 / 有「失败率」/ 有 Lv·MAX / 场上 ≥2 个队友名，任一命中就算。
            # 但也不能放太松，否则切场景的中途帧会把别的页面文字当队友
            # （05:29 出现过把出行页的「游戏厅」当队友）。
            names = self._court_names(its, f)
            has_pct = any("失败率" in it["t"] for it in its)
            has_lv = any(re.match(r"^(LV\.?\s*\d+|MAX)", it["t"].replace(" ", "").upper())
                         for it in its)
            # 反例加固（2026-11-23 10:32 实测）：采样中途点了按钮切到「赛前准备」，
            # 那一帧把阵容页的位置名（分卫/大前/控卫）当成队友、把球队大卡当成器材，
            # 结果唯一一项得分 8 分，直接定案选错技能。只要出现比赛/结算类的字眼就丢帧。
            off_core = any(w in " ".join(it["t"] for it in its)
                           for w in ("赛前准备", "跳过比赛", "前往比赛", "MVP", "目标达成",
                                     # 2026-11-23 12:10 实跑发现：点技能后有时先弹「心·技·体」
                                     # 属性/加成大浮层，把编队面板整个盖住。这类帧以前会被
                                     # 当成训练核心页的错误读数，现在明确丢掉（48% 的采样
                                     # 0 帧有效就是栽在这上面）。
                                     "心·技·体", "属性加成", "表情升级", "倍率加成"))
            looks_core = (not off_core) and (hits >= 1 or has_pct or has_lv or len(names) >= 2)
            if not looks_core:
                # 连续 4 帧都不像训练核心页 = 页面已经走了（训练结束弹出「器材事件」
                # 这类随机事件最常见），再采下去只是白烧 10 次 × 0.18s + OCR 时间。
                bad_streak += 1
                if bad_streak >= 4 and used == 0:
                    self.log_warning("连续 4 帧都不像训练核心页 → 页面已经切走，停止采样")
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
                    self.log_info(f"采样帧 {f.shape[1]}x{f.shape[0]} 器材 {len(cs)} "
                                  f"{[(int(c['x']), int(c['y']), card_glow(f, c)) for c in cs]}")
            except Exception as e:      # 器材读失败不能拖累编队采样
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
            # 写死一个名字必然剔错。谁该忽略交给 _do_training_core 用「每个技能都有」
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
            # 一帧都没通过「像训练核心页」的判据 —— 把画面文字和截图留下来，
            # 不然只能猜（实跑里 48% 的采样是这种情况）。
            try:
                self.log_warning(f"采样 0 帧有效，画面文字：{[it['t'] for it in its][:14]}")
                self._shot(f, "core_fail")
            except Exception:
                pass
        if used < frames:
            self.log_info(f"编队采样 {used}/{frames} 帧有效（试了 {tries} 次，废帧已丢弃）")
        return mates, pct, level, list(equip)

    def _skill_level(self, items, row):
        """技能胶囊右边那个 Lv.1 / Lv.2 / MAX。"""
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
          ③ **现在（玩家原话）：「失败可以赌一把，收益如果很高，小于 30 都能去」**
             → `fail_limit: 30`。30 以内不排除，交给期望收益（_score_core 里的
             ×(1-p)）排序：收益高到 5 分的课，赌 25% 失败仍然赢过稳拿 1 分的小课。
        全部技能都 >limit 时才退回原排名 —— 否则当天就彻底没得练了。
        读不到失败率（pct=None）仍视为不安全（不能假定它 0%）。
        """
        lim = cfg.get("fail_limit")
        if lim is None or not scored:
            return scored
        safe = [d for d in scored if d["pct"] is not None and float(d["pct"]) <= float(lim)]
        if not safe:
            self.log_warning(f"所有技能失败率都 >{lim}%，只能按原排名（今天没得选，硬着头皮练）")
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
        low = float(cons.get("stamina_below", 45))
        if st_now >= low:
            return
        target = str(cons.get("skill") or "意识")
        hit = [d for d in scored if d["row"]["t"] == target]
        if not hit:
            self.log_info(f"精力 {st_now} < {low}，但「{target}」今天没有角标，按原排名")
            return
        boost = float(cons.get("boost", 99))
        for d in hit:
            d["score"] += boost
        self.log_info(f"精力 {st_now} < {low} → 「{target}」加权 +{boost:g}"
                      f"（不耗精力、小幅回精力、成功率最高）")

    def _score_core(self, mates, level, pct, cfg, equips=None):
        """编队打分 = 出场队友数，再按失败率打折。

        2026-11-23 玩家口径（当次确定）：
          - **技能等级不计分**（Lv.1/Lv.2/MAX 三档全部当 0），只是照旧读出来打在日志里，
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
          4. 技能等级 Lv.1/Lv.2/MAX ← 只记录，**不计分**（玩家口径）
        """
        base = len(mates) * float(cfg.get("teammate", 1.0))

        # 器材：训练核心页中场摆着的 1~3 张支援卡，按外框光效计分。
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
            if ab and self._ab_kind(*ab) == "focus":
                focus, cap = ab[0], self.FOCUS_CAP
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
        limit = int(self._cfg("每轮最多补专注次数", "每轮最多补体力次数", default=4))
        close_xy = (0.927, 0.135)          # 右上角 X
        use_item = self._find(items, ["使用道具"], exact=True)

        def close(reason):
            self.log_warning(f"专注恢复：{reason}，关闭弹窗")
            self.click_relative(*close_xy)
            self.stamina_refills = 0        # 这次补药结束，下次弹窗重新计瓶数

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
            if ab and self._ab_kind(*ab) == "focus":
                cur, cap = ab[0], self.FOCUS_CAP
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
        self.log_info(f"编队评分：队友 {len(names)} 人（{sorted(names)}），器材 {len(equips)} 个，小计 {score}")
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
        """事件选项打分：统一走 `_reward_key`（笔记 > 技能 > 未溢出精力 > 属性）。

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
        """随机事件：技能 > 精力(未溢出) > 属性 > 金钱；判断不了选第一个。"""
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

    def _do_unknown(self, items, frame):
        joined = " ".join(i["t"] for i in items)
        if any(k in joined for k in ("拼图", "碎片", "小游戏")):
            self.log_warning("未知页面但看着像小游戏，按放弃处理")
            return self._do_minigame(items, frame)
        if self._event_cards(frame, items):
            self.log_info("未知页面但是事件卡片样式，按事件处理")
            return self._do_event(items, frame)
        if "初始加成" in joined:
            self.log_info("加成展示页，点屏幕中间推进")
            self.click_relative(0.5, 0.5)
            return True

        mode = str(self.config.get("未知页面兜底") or "点屏幕中间")

        if self._click_confirm(items, frame):
            return True
        if mode == "暂停":
            self.log_error(f"未知页面，按配置暂停。文字: {[i['t'] for i in items][:16]}", notify=True)
            return None
        self.unknown_count += 1
        if self.unknown_count <= 10:
            self.log_warning(f"未知页面（第 {self.unknown_count} 次），点屏幕中间推进。"
                             f"文字: {[i['t'] for i in items][:14]}")
            self.click_relative(0.5, 0.5)
            return True
        self.log_error(f"未知页面连续 {self.unknown_count} 次没有可点主按钮，暂停。"
                       f"文字: {[i['t'] for i in items][:16]}", notify=True)
        return None

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
        self.core_checked = False
        self.trip_picked = False
        # 「出行先探笔记，笔记 > 训练」（玩家 2026-11-23）：
        #   每日页同时有「训练」和「出行」时，先进出行页看地点标签里有没有笔记。
        #   有 → 直接出行；没有 → 退回每日页走训练。
        #   用「第N周-星期X」记今天已经探过，避免同一天反复进出。
        self.trip_probe = False          # 本次进出行页是「探笔记」，不是真要出行
        self.trip_probe_done = None      # 已经探过的那天，例如 "6-7"
        self.trip_probe_disabled = False # 出行页退不出去 → 以后不再探，直接按原顺序
        self.trip_back_tries = 0         # 本次退出出行页试了几次
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
        self.choice_idx = 0
        self.stamina_refills = 0
        self.stamina_misses = 0

        max_steps = int(self.config.get("最大步数") or 1500)
        wait = float(self.config.get("等待秒数") or 1.3)
        stop_after_run = bool(self.config.get("跑完一轮就停", True))

        self.log_info(f"开始自动训练：最多 {max_steps} 步")

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
            return self._run_loop(max_steps, wait, stop_after_run)
        finally:
            self._watchdog_stop = True

    def _run_loop(self, max_steps, wait, stop_after_run):
        last_key, repeat = None, 0
        last_page = None
        same_count = 0
        blind = 0
        blank = 0

        for step in range(1, max_steps + 1):
            self._last_step_at = time.time()
            self._ensure_foreground()
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
            self._read_stamina(items, frame)
            self.weekday = self._read_week(items)[1]
            page = self._classify(items, frame)
            # 事件页没有标题字，顶栏又带着「第N周」，很容易被认成每日页。
            # 成排黄卡片是它最硬的图像特征，所以在这里纠正一次。
            if page in ("未知", "每日页", "训练结束", "剧情事件", "选项") \
                    and self._event_cards(frame, items):
                self.log_info(f"判定 {page} 但底部有成排黄卡片 → 按事件页处理")
                page = "剧情事件"
            if page != "未知":
                self.unknown_count = 0
            if page != "训练核心":
                self.core_checked = False
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

            key = (page, len(items))
            repeat = repeat + 1 if key == last_key else 0
            last_key = key
            same_count = same_count + 1 if page == last_page else 0
            week, day = self._read_week(items)
            where = f"第{week}周 星期{day}" if week else ""
            self.log_info(f"--- 第 {step} 步 | 页面：{page} {where} | 精力 {self.stamina}/{self.stamina_max} "
                          f"| 局外专注 {self.out_focus}/{self.out_focus_max} | 文字 {len(items)} 段")

            if "笔记继承" in " ".join(i["t"] for i in items) and not self.notes_done:
                self.log_info("检测到「笔记继承」，标记本次已完成")
                self.notes_done = True

            # 局外体力不足弹窗：绝不点「购买」，直接取消
            joined = " ".join(i["t"] for i in items)
            if page not in ("专注恢复", "体力恢复") and ("购买" in joined or "补充" in joined) \
                    and ("体力" in joined or "精力" in joined):
                it = self._find(items, ["取消", "关闭", "稍后"])
                if it:
                    self.log_warning(f"体力不足弹窗，点「{it['t']}」不买")
                    self._click_item(it, "[拒绝购买]")
                else:
                    self.log_warning("体力不足弹窗，找不到取消按钮，点左上角关闭")
                    self.click_relative(0.02, 0.5)
                continue

            # 只要见过非大厅/非未知的页面，就说明「这一轮训练已经开跑」，
            # 之后再回到大厅才算跑完（不然后续训练还没开始就被判成跑完）。
            if page not in ("大厅", "未知"):
                self.saw_training = True

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
                if self.saw_training and stop_after_run:
                    self.log_info("★ 一轮训练已跑完，回到大厅 —— 收工", notify=True)
                    self._shot(frame, "done_hall")
                    break
                # 大厅右下角按钮实际写的是「训练中」（不是「训练」），
                # 且上次异常退出时点它会先弹「训练确认 → 继续训练」。
                it = self._find(items, ["训练中", "训练"], exact=True) or self._find(items, ["训练中"])
                if it:
                    self.in_training = True
                    self._click_item(it, "[进入训练]")
                else:
                    # OCR 漏读「训练」两个字也不能就这么收工：大厅入口固定在右下角
                    # （1920x1080 实测 0.764, 0.883），按比例点一下。
                    self.log_info("大厅没读到「训练」文字，按固定坐标点右下角入口")
                    self.in_training = True
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
                self._do_next("训练评分")(items, frame)
            elif page == "恭喜获得":
                self._do_next("恭喜获得")(items, frame)
            elif page == "训练结束":
                # 这一屏有「前往主界面 / 编队 / 记录笔记 / 下一步」四个按钮。
                # 「训练结束」四个字是标题，不是按钮（22:44 连点 15 次标题，原地空转）。
                it = (self._find(items, ["前往主界面"]) or self._find(items, ["前往"])
                      or self._find(items, ["下一步"], exact=True)
                      or self._find(items, ["记录笔记"]))
                if it:
                    self._click_item(it, "[训练结束-回主界面]")
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
            elif page == "加强训练":
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
            elif page == "训练核心":
                self._do_training_core(items, frame)
            elif page == "点击继续":
                it = self._find(items, ["点击屏幕继续"]) or self._find(items, ["点击继续"])
                if it:
                    self._click_item(it, "[点击屏幕继续]")
                else:
                    self.click_relative(0.5, 0.86)
            elif page == "训练结果":
                it = self._find(items, ["点击屏幕继续"])
                if it:
                    self._click_item(it, "[训练结果-继续]")
                else:
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
                    # 2026-11-23 实测（冷启动进训练第一次遇到）：刚进选人页时队伍没配满，
                    # 「下一步」是灰色的 BGR≈(195,198,200)，等多久都不会变黄 —— 之前这里
                    # 就是「等 2 秒再试」死循环，连续 15 步没变化就整轮退出。
                    # 正解：页面上有「自动选择」，点一下游戏会把队伍配满，「下一步」随即变黄。
                    auto = self._find(items, ["自动选择", "自动填充"], exact=True)
                    if auto:
                        self.log_info("主按钮是灰的（队伍没配满），先点「自动选择」再确认")
                        self._click_item(auto, "[自动选择]")
                    else:
                        self.log_warning(f"{page} 主按钮不是黄色，等 2 秒再试")
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
            if same_count >= 15:
                self.log_error(f"页面「{page}」连续 {same_count} 步没变化，停下看看", notify=True)
                self._shot(frame, f"stuck_{step}")
                break
            last_page = page
            self.sleep(wait)

        self.log_info("本轮结束")
        return True
