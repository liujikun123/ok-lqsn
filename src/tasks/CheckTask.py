"""只读体检：一个按钮都不点，只报告当前画面被认成什么。

用途：
1. 改完规则后先跑这个，确认页面指纹没判错（比如把游戏厅当成普通地点）。
2. 卡在奇怪页面时跑这个，把日志发我，我能直接看到 OCR 文字和按钮位置。
"""
from src.tasks.AutoTask import AutoTask


class CheckTask(AutoTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "只读体检 / Check"
        self.description = "不点任何按钮：只报告页面判定、体力/星期、全部文字、黄色按钮与「放弃」按钮位置"
        self.default_config.update({
            "保存截图": True,
        })

    def run(self):
        frame = self._frame()
        if frame is None:
            self.log_error("取不到画面（窗口最小化的话 WGC 抓不到）", notify=True)
            return False

        items = self._texts(frame)
        self._read_stamina(items, frame)
        page = self._classify(items, frame)
        self.log_info(f"页面判定：{page} | 体力 {self.stamina}/{self.stamina_max} | 星期 {self.weekday}", notify=True)
        self.log_info(f"全部文字：{[i['t'] for i in items]}")

        if self.config.get("保存截图"):
            self._shot(frame, "check")

        if page == "小游戏":
            it = self._find(items, ["放弃"], exact=True) or self._find(items, ["放弃"])
            if it:
                self.log_info(f"「放弃」按钮 @({int(it['cx'])},{int(it['cy'])}) "
                              f"黄色占比 {self._yellow_ratio(frame, it['b']):.2f}")
            else:
                self.log_warning("小游戏页里没找到「放弃」，需要看截图确认按钮叫什么")
        elif page == "出行":
            names = [i for i in items if i["t"] in self.TRIP_NAMES]
            for n in names:
                banned = "【已排除，不会去】" if self._excluded_place(n["t"]) else ""
                self.log_info(f"出行地点「{n['t']}」{banned}")
        elif page == "训练核心":
            rows = [it for it in items if it["t"] in self.SKILLS]
            self.log_info(f"技能列：{[(r['t'], int(r['cy'])) for r in rows]}")

        cards = self._event_cards(frame, items)
        if cards:
            self.log_info("事件卡片：" + str([(c["title"], round(self._score_option(c["text"]), 1))
                                              for c in cards]))
        opts = self._yellow_options(frame, items)
        self.log_info(f"成排黄色选项（>=2 才算）：{[o['t'] for o in opts]}")
        self.log_info("体检结束，没有点任何按钮")
        return True
