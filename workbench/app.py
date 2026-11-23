"""工作台主界面。跑起来：`python -m workbench`（或用根目录的 run_workbench.bat）。

左：配置      中/右：日志与状态      下：启动/停止
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from workbench import profiles as P
from workbench import runner as R
from workbench.settings_ui import SettingsPanel

ROOT = R.project_root()

ACCENT = "#2f6fed"
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#e3e6eb"
TEXT = "#1f2329"
MUTED = "#8a9099"
OK_C = "#1a9f5a"
BAD_C = "#d93025"
WARN_C = "#c47f00"


STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT};
           font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; }}
QGroupBox {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;
             margin-top: 14px; padding: 10px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;
                    color: {MUTED}; font-weight: 600; }}
QPushButton {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
               padding: 7px 14px; font-weight: 500; }}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:disabled {{ color: #b9bec7; background: #f0f1f3; border-color: {BORDER}; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; }}
QPushButton#primary:hover {{ background: #255ad0; }}
QPushButton#danger {{ background: #fff1f0; color: {BAD_C}; border: 1px solid #ffd0cb; }}
QPushButton#danger:hover {{ background: #ffe3e0; }}
QComboBox {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 10px; }}
QPlainTextEdit, QTableWidget {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QTableWidget {{ gridline-color: #f0f1f3; }}
QHeaderView::section {{ background: #fafbfc; border: none; border-bottom: 1px solid {BORDER};
                        padding: 5px; color: {MUTED}; font-weight: 600; }}
QSplitter::handle {{ background: transparent; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; background: {CARD}; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 6px 14px; margin-right: 2px;
                border: 1px solid transparent; border-top-left-radius: 8px;
                border-top-right-radius: 8px; }}
QTabBar::tab:selected {{ background: {CARD}; color: {TEXT}; border-color: {BORDER};
                         border-bottom-color: {CARD}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {ACCENT}; }}
QSpinBox {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
            padding: 5px 8px; min-width: 72px; }}
QCheckBox {{ spacing: 6px; }}
"""

LOG_COLORS = {
    "info": TEXT,
    "warn": WARN_C,
    "error": BAD_C,
    "raw": "#b0b5bd",
}


def _fmt(v) -> str:
    """记录面板里的数值显示：None → -，整数不带小数点。"""
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class Chip(QLabel):
    """顶部状态小胶囊。"""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setStyleSheet(
            f"background:{CARD}; border:1px solid {BORDER}; border-radius: 11px;"
            f"padding: 3px 10px; color:{MUTED}; font-size:12px;")
        self.set_ok(None, "检测中…")

    def set_ok(self, ok: bool | None, detail: str) -> None:
        dot = MUTED if ok is None else (OK_C if ok else BAD_C)
        self.setText(f"<span style='color:{dot}'>●</span> {self.name}　{detail}")
        self.setToolTip(detail)


class Workbench(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = R.load_workbench_cfg(ROOT)
        self.game = self.cfg.get("game") or {}
        self.task = self.cfg.get("task") or {}
        self.auto_scroll = True
        self.hide_noise = True

        self.runner = R.Runner(ROOT, self)
        self.runner.log.connect(self.append_log)
        self.runner.state.connect(self.on_state)
        self.runner.done.connect(self.on_done)

        self.setWindowTitle("篮球少女 · 自动训练工作台")
        self.setMinimumSize(1040, 700)
        self.resize(1280, 820)
        self._build()

        self.refresh_all()
        self.refresh_records()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_game_state)
        self.timer.start(3000)

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 顶栏
        head = QHBoxLayout()
        title = QLabel("篮球少女 · 自动训练工作台")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        sub = QLabel("发布版 0.2 · 可改配置")
        sub.setStyleSheet(f"color:{MUTED}; font-size:12px; padding-top:4px;")
        head.addWidget(title)
        head.addWidget(sub)
        head.addStretch(1)

        self.chip_env = Chip("环境")
        self.chip_game = Chip("游戏")
        self.chip_win = Chip("窗口")
        for c in (self.chip_env, self.chip_game, self.chip_win):
            head.addWidget(c)
        btn_re = QPushButton("重新检测")
        btn_re.clicked.connect(self.refresh_all)
        head.addWidget(btn_re)
        outer.addLayout(head)

        # 主体
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._left_panel())
        split.addWidget(self._right_panel())
        split.setSizes([430, 750])
        outer.addWidget(split, 1)

        # 底部按钮
        bar = QHBoxLayout()
        self.btn_boot = QPushButton("启动游戏")
        self.btn_boot.clicked.connect(lambda: self.start_boot(auto=False))
        self.btn_full = QPushButton("启动游戏并训练")
        self.btn_full.setObjectName("primary")
        self.btn_full.clicked.connect(lambda: self.start_boot(auto=True))
        self.btn_train = QPushButton("只跑训练（游戏已开）")
        self.btn_train.clicked.connect(self.start_train)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.runner.stop)
        self.btn_stop.setEnabled(False)

        bar.addWidget(self.btn_boot)
        bar.addWidget(self.btn_full)
        bar.addWidget(self.btn_train)
        bar.addStretch(1)
        bar.addWidget(self.btn_stop)
        outer.addLayout(bar)

        self.statusBar().showMessage("就绪")

    def _left_panel(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(8)

        # 规则文件：多份规则（strategy / 周末冲量 …）就在这里切
        g1 = QGroupBox("规则文件")
        row = QHBoxLayout(g1)
        self.cmb_rule = QComboBox()
        self.cmb_rule.currentTextChanged.connect(self.preview_rule)
        btn_reload = QPushButton("重新读取")
        btn_reload.clicked.connect(self.reload_rules)
        btn_open = QPushButton("打开文件")
        btn_open.clicked.connect(self.open_rule_file)
        row.addWidget(self.cmb_rule, 1)
        row.addWidget(btn_reload)
        row.addWidget(btn_open)
        lay.addWidget(g1)

        # 训练方案：一份方案 = 整套局内规则（profiles/<名字>.json，可导入导出）。
        # 切换 = 把方案里的 strategy.yaml 原文写回，现有设置面板/任务读取链路不变。
        g2 = QGroupBox("训练方案")
        g2v = QVBoxLayout(g2)
        row2 = QHBoxLayout()
        self.cmb_profile = QComboBox()
        self.cmb_profile.currentTextChanged.connect(self.preview_profile)
        row2.addWidget(self.cmb_profile, 1)
        self.btn_apply_profile = QPushButton("应用")
        self.btn_apply_profile.clicked.connect(self.apply_selected_profile)
        row2.addWidget(self.btn_apply_profile)
        g2v.addLayout(row2)
        row3 = QHBoxLayout()
        self._profile_buttons = []
        for label, slot in (("保存改动", self.save_current_profile),
                            ("另存为", self.save_as_profile),
                            ("重命名", self.rename_profile),
                            ("删除", self.delete_profile),
                            ("导入", self.import_profile),
                            ("导出", self.export_profile)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row3.addWidget(b)
            self._profile_buttons.append(b)
        g2v.addLayout(row3)
        self.lab_profile = QLabel("")
        self.lab_profile.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.lab_profile.setWordWrap(True)
        g2v.addWidget(self.lab_profile)
        lay.addWidget(g2)

        self.tabs = QTabWidget()
        self.panel_settings = SettingsPanel(ROOT, self)
        self.panel_settings.log.connect(self.append_log)
        self.tabs.addTab(self.panel_settings, "运行设置")
        self.tabs.addTab(self._summary_tab(), "规则摘要")
        self.tabs.addTab(self._raw_tab(), "规则原文")
        self.tabs.addTab(self._records_tab(), "运行记录")
        lay.addWidget(self.tabs, 1)
        return box

    def _records_tab(self) -> QWidget:
        """运行记录：读 logs/runs.jsonl，顶部汇总当前运行，下面列出每次选课得分。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        head = QHBoxLayout()
        self.lab_record_title = QLabel("暂无运行记录，先点「启动训练」跑一轮。")
        self.lab_record_title.setStyleSheet("font-weight:600;")
        head.addWidget(self.lab_record_title, 1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh_records)
        head.addWidget(btn_refresh)
        v.addLayout(head)

        self.lab_record_sum = QLabel("")
        self.lab_record_sum.setWordWrap(True)
        self.lab_record_sum.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        v.addWidget(self.lab_record_sum)

        self.tbl_records = QTableWidget(0, 4)
        self.tbl_records.setHorizontalHeaderLabels(["轮", "技能", "最终得分", "失败率"])
        self.tbl_records.verticalHeader().setVisible(False)
        self.tbl_records.horizontalHeader().setStretchLastSection(True)
        self.tbl_records.setColumnWidth(0, 40)
        self.tbl_records.setColumnWidth(1, 90)
        self.tbl_records.setColumnWidth(2, 80)
        self.tbl_records.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_records.setSelectionMode(QTableWidget.NoSelection)
        v.addWidget(self.tbl_records, 1)

        tip = QLabel("数据来自 logs/runs.jsonl · 每完成一次选课记一行 · 体力是选课那一刻的读数")
        tip.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        v.addWidget(tip)
        return w

    def refresh_records(self) -> None:
        """显示最近一次运行的汇总与明细；训练中反复调用，最后一条自然是本次。"""
        if not hasattr(self, "tbl_records"):
            return
        runs = R.read_run_log(ROOT)
        if not runs:
            self.lab_record_title.setText("暂无运行记录，先点「启动训练」跑一轮。")
            self.lab_record_sum.setText("")
            self.tbl_records.setRowCount(0)
            return
        r = runs[0]
        status = r.get("status") or "?"
        self.lab_record_title.setText(
            f"最近一次运行 · {r.get('start') or r.get('id', '')} · {status}")
        sm = (f"完成 {r.get('rounds_done', 0)}/{r.get('rounds', 0)} 轮　"
              f"训练 {r.get('trains', 0)} 次　"
              f"精力 {_fmt(r.get('stamina_start'))}→{_fmt(r.get('stamina_end'))}　"
              f"专注 {_fmt(r.get('focus_start'))}→{_fmt(r.get('focus_end'))}"
              f"（喝药 {r.get('drinks', 0)} 瓶）")
        if r.get("resumed"):
            sm += "　· 中途断点续跑"
        self.lab_record_sum.setText(sm)

        items = r.get("items") or []
        self.tbl_records.setRowCount(len(items))
        for i, it in enumerate(items):
            score = it.get("score")
            fail = it.get("fail")
            vals = [
                str(it.get("round") or "-"),
                str(it.get("skill") or "-"),
                f"{score:.2f}" if isinstance(score, (int, float)) else "-",
                f"{fail}%" if fail else "0%",
            ]
            for c, val in enumerate(vals):
                cell = QTableWidgetItem(val)
                if c == 2 and isinstance(score, (int, float)):
                    # 得分高亮：>=6 绿、<=3 橙，一眼看出这轮选得好不好
                    if score >= 6:
                        cell.setForeground(QColor(OK_C))
                    elif score <= 3:
                        cell.setForeground(QColor(WARN_C))
                self.tbl_records.setItem(i, c, cell)
        self.tbl_records.resizeRowsToContents()

    def _summary_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["项目", "当前值"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setColumnWidth(0, 120)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionMode(QTableWidget.NoSelection)
        self.tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self.tbl)
        v.addStretch(1)
        note = QLabel("这一页只是速览，要改值请到「运行设置」。")
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        v.addWidget(note)
        return w

    def _raw_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self.txt_rule = QPlainTextEdit()
        self.txt_rule.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.txt_rule.setFont(f)
        v.addWidget(self.txt_rule)
        return w

    def _right_panel(self) -> QWidget:
        g = QGroupBox("运行日志")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.chk_noise = QPushButton("隐藏 OCR 噪音：开")
        self.chk_noise.setCheckable(True)
        self.chk_noise.setChecked(True)
        self.chk_noise.clicked.connect(self.toggle_noise)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(lambda: self.txt_log.clear())
        btn_save = QPushButton("存成文件")
        btn_save.clicked.connect(self.save_log)
        btn_shot = QPushButton("打开截图目录")
        btn_shot.clicked.connect(lambda: self.open_dir("screenshots"))
        row.addWidget(self.chk_noise)
        row.addStretch(1)
        row.addWidget(btn_shot)
        row.addWidget(btn_clear)
        row.addWidget(btn_save)
        v.addLayout(row)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.txt_log.setFont(f)
        v.addWidget(self.txt_log, 1)
        return g

    # ------------------------------------------------------------ 配置
    # ------------------------------------------------------------ 方案管理
    def reload_profiles(self) -> None:
        names = P.list_profiles(ROOT)
        cur = P.active_name(ROOT)
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        self.cmb_profile.addItems(names)
        if cur in names:
            self.cmb_profile.setCurrentText(cur)
        self.cmb_profile.blockSignals(False)
        self._show_profile_desc()

    def _show_profile_desc(self) -> None:
        name = self.cmb_profile.currentText()
        if not name:
            self.lab_profile.setText("（还没有方案，点「另存为」创建一份）")
            return
        mark = "当前生效" if name == P.active_name(ROOT) else "未应用"
        self.lab_profile.setText(f"{mark}：{P.describe(name, ROOT)}")

    def preview_profile(self) -> None:
        """下拉选中即应用；程序化 reload 时信号被 block，不会误触发。"""
        name = self.cmb_profile.currentText()
        if not name or name == P.active_name(ROOT):
            self._show_profile_desc()
            return
        try:
            P.apply_profile(name, ROOT)
            self.reload_rules()          # 规则原文/摘要跟着刷新
            self.append_log("info", f"已切换训练方案「{name}」→ 规则文件已更新")
        except Exception as e:
            self.append_log("error", f"切换方案失败：{e}")
        self._show_profile_desc()

    def apply_selected_profile(self) -> None:
        self.preview_profile()

    def save_current_profile(self) -> None:
        name = self.cmb_profile.currentText()
        if not name:
            QMessageBox.information(self, "保存方案", "请先选中一份方案")
            return
        desc = ""
        try:
            desc = P.load_profile(name, ROOT).get("description") or ""
        except Exception:
            pass
        ret = QMessageBox.question(
            self, "保存方案",
            f"把当前规则原文（含注释）存到方案「{name}」？\n会覆盖这份方案的旧快照。")
        if ret != QMessageBox.Yes:
            return
        try:
            P.snapshot_current(name, desc, ROOT)
            self.append_log("info", f"方案「{name}」已保存")
            self._show_profile_desc()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def save_as_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "另存为方案", "方案名字：",
                                        text="百目鬼训练-副本")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in P.list_profiles(ROOT):
            QMessageBox.warning(self, "另存为", f"方案「{name}」已存在")
            return
        desc, _ = QInputDialog.getText(self, "另存为方案", "一句话说明（可留空）：")
        try:
            P.snapshot_current(name, desc, ROOT)
            P.set_active(name, ROOT)
            self.reload_profiles()
            self.reload_rules()
            self.append_log("info", f"新方案「{name}」已创建并启用")
        except Exception as e:
            QMessageBox.warning(self, "另存为失败", str(e))

    def rename_profile(self) -> None:
        old = self.cmb_profile.currentText()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "重命名方案", "新名字：", text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        try:
            P.rename_profile(old, new.strip(), ROOT)
            self.reload_profiles()
            self.append_log("info", f"方案「{old}」已改名为「{new.strip()}」")
        except Exception as e:
            QMessageBox.warning(self, "重命名失败", str(e))

    def delete_profile(self) -> None:
        name = self.cmb_profile.currentText()
        if not name:
            return
        ret = QMessageBox.question(
            self, "删除方案",
            f"确定删除方案「{name}」？\n（删的是 profiles/ 里的快照，不影响当前规则文件）")
        if ret != QMessageBox.Yes:
            return
        try:
            P.delete_profile(name, ROOT)
            self.reload_profiles()
            self.reload_rules()
            self.append_log("info", f"方案「{name}」已删除")
        except Exception as e:
            QMessageBox.warning(self, "删除失败", str(e))

    def import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入方案", "", "方案 JSON (*.json)")
        if not path:
            return
        try:
            name = P.import_profile(path, ROOT)
            self.reload_profiles()
            self.append_log("info", f"方案「{name}」已导入（选中即应用）")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def export_profile(self) -> None:
        name = self.cmb_profile.currentText()
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出方案", f"{name}.json",
                                              "方案 JSON (*.json)")
        if not path:
            return
        try:
            out = P.export_profile(name, path, ROOT)
            self.append_log("info", f"方案「{name}」已导出：{out}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def reload_rules(self) -> None:
        files = R.list_rule_files(ROOT)
        cur = self.cmb_rule.currentText()
        self.cmb_rule.blockSignals(True)
        self.cmb_rule.clear()
        self.cmb_rule.addItems(files)
        if cur in files:
            self.cmb_rule.setCurrentText(cur)
        elif self.task.get("default_rule"):
            name = os.path.basename(self.task["default_rule"])
            if name in files:
                self.cmb_rule.setCurrentText(name)
        self.cmb_rule.blockSignals(False)
        self.preview_rule()

    def current_rule_path(self) -> str:
        return os.path.join(ROOT, "config", self.cmb_rule.currentText())

    def preview_rule(self) -> None:
        p = self.current_rule_path()
        name = self.cmb_rule.currentText()
        if name and hasattr(self, "panel_settings"):
            # 设置面板改的永远是「当前选中的这份规则文件」
            self.panel_settings.bind_yaml(os.path.join("config", name))
        if not os.path.exists(p):
            self.txt_rule.setPlainText("（没有可读的规则文件）")
            self.tbl.setRowCount(0)
            return
        try:
            with open(p, encoding="utf-8") as f:
                self.txt_rule.setPlainText(f.read())
        except Exception as e:
            self.txt_rule.setPlainText(f"读取失败：{e}")
        rows = R.summarize_rule(p)
        self.tbl.setRowCount(len(rows))
        for i, (k, val) in enumerate(rows):
            self.tbl.setItem(i, 0, QTableWidgetItem(k))
            it = QTableWidgetItem(val)
            if k == "空槽检查" and val == "关闭":
                it.setForeground(QColor(WARN_C))
            self.tbl.setItem(i, 1, it)
        # 表格高度跟着行数走：行多了也不会出现滚动条（左侧空间够放十行）
        self.tbl.resizeRowsToContents()
        body = sum(self.tbl.rowHeight(i) for i in range(len(rows))) or 28 * len(rows)
        self.tbl.setFixedHeight(self.tbl.horizontalHeader().height() + body
                                + 2 * self.tbl.frameWidth() + 8)

    def open_rule_file(self) -> None:
        p = self.current_rule_path()
        if os.path.exists(p):
            os.startfile(p)  # noqa: S606 - Windows 上就是用系统默认程序打开

    # ------------------------------------------------------------ 自检
    def refresh_all(self) -> None:
        self.reload_profiles()
        self.reload_rules()
        checks = R.preflight(ROOT)
        bad = [c for c in checks if not c.ok]
        self.chip_env.set_ok(not bad,
                             "全部通过" if not bad else f"{len(bad)} 项异常")
        tip = "\n".join(f"{'✓' if c.ok else '✗'} {c.name}：{c.detail}" for c in checks)
        self.chip_env.setToolTip(tip)
        for c in bad:
            self.append_log("error", f"自检失败 · {c.name}：{c.detail}")
        self.refresh_game_state()

    def refresh_game_state(self) -> None:
        run, detail = R.game_running(ROOT)
        self.chip_game.set_ok(run, detail)
        win, wdetail = R.game_window(ROOT)
        self.chip_win.set_ok(win, wdetail)
        if not self.runner.busy():
            self.statusBar().showMessage(
                f"空闲 · 游戏{'运行中' if run else '未启动'} · 窗口{wdetail}")

    # ------------------------------------------------------------ 动作
    def _guard(self) -> bool:
        if self.runner.busy():
            QMessageBox.information(self, "先等一下", "已经有一项在跑了，先停止或等它结束。")
            return False
        return True

    def start_boot(self, auto: bool) -> None:
        if not self._guard():
            return
        if not self._flush_settings():
            return
        timeout = int(self.game.get("boot_timeout") or 150)
        args = ["tools/start_game.py", "--timeout", str(timeout)]
        if auto:
            args.append("--auto")
        label = "启动游戏并训练" if auto else "启动游戏"
        self.runner.run(args, label)

    def _flush_settings(self) -> bool:
        """开跑前处理「设置面板有改动但没保存」。返回 False 表示用户取消。"""
        dirty = self.panel_settings.dirty_or_none()
        if not dirty:
            return True
        r = QMessageBox.question(
            self, "设置还没保存",
            f"设置面板里有 {len(dirty)} 项改动没保存。\n\n"
            "是：保存后运行　　否：按文件里现有的配置运行　　取消：回去改",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Yes:
            self.panel_settings.save()
        return True

    def start_train(self) -> None:
        if not self._guard():
            return
        if not self._flush_settings():
            return
        run, _ = R.game_running(ROOT)
        if not run:
            if QMessageBox.question(self, "游戏没开",
                                    "没检测到游戏进程，还是要直接跑训练吗？") != QMessageBox.Yes:
                return
        args = ["-m", "ok", "run_task", "-c", self.task.get("config", "src.config:config"),
                self.task.get("name", "AutoTask")]
        self.runner.run(args, "自动训练")

    def on_state(self, st: str) -> None:
        busy = st in ("running", "stopping")
        for b in (self.btn_boot, self.btn_full, self.btn_train):
            b.setEnabled(not busy)
        # 方案切换会改写 strategy.yaml，运行中禁止（任务已缓存配置，改了也不生效）
        for b in getattr(self, "_profile_buttons", []) + [self.btn_apply_profile, self.cmb_profile]:
            b.setEnabled(not busy)
        self.btn_stop.setEnabled(st == "running")
        self.statusBar().showMessage({"running": "运行中…", "stopping": "正在停止…",
                                      "idle": "空闲"}.get(st, st))
        # 焦点根治：任务一启动就把工作台最小化，前台让给游戏。
        # Pynput 是前台点击，工作台停在前面（或你点它看日志）会把焦点抢走，
        # Pynput 的 click() 检测到不在前台就静默丢弃 → 游戏一动不动。
        # 最小化后从任务栏点回来即可看日志/停止，不影响训练。
        if st == "running" and (self.cfg.get("run") or {}).get("minimize_on_run", True):
            try:
                self.showMinimized()
                self.append_log("info", "工作台已最小化，前台已让给游戏（任务栏可点回来）")
            except Exception:
                pass

    def on_done(self, code: int, label: str) -> None:
        self.refresh_game_state()
        self.refresh_records()
        if code != 0:
            self.append_log("error", f"「{label}」非正常结束（{code}），看日志最后几行")

    # ------------------------------------------------------------ 日志
    def append_log(self, level: str, text: str) -> None:
        if level == "raw" and self.hide_noise:
            return
        color = LOG_COLORS.get(level, TEXT)
        stamp = time.strftime("%H:%M:%S")
        self.txt_log.appendHtml(
            f"<span style='color:{MUTED}'>{stamp}</span> "
            f"<span style='color:{color}'>{self._esc(text)}</span>")
        if self.auto_scroll:
            self.txt_log.moveCursor(QTextCursor.End)

    @staticmethod
    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def toggle_noise(self) -> None:
        self.hide_noise = self.chk_noise.isChecked()
        self.chk_noise.setText(f"隐藏 OCR 噪音：{'开' if self.hide_noise else '关'}")

    def save_log(self) -> None:
        d = os.path.join(ROOT, self.cfg.get("run", {}).get("log_dir", "logs"), "workbench")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, time.strftime("workbench_%Y%m%d_%H%M%S.log"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.txt_log.toPlainText())
        self.append_log("info", f"日志已保存：{p}")

    def open_dir(self, name: str) -> None:
        d = os.path.join(ROOT, name)
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606

    def closeEvent(self, e) -> None:
        if self.runner.busy():
            if QMessageBox.question(self, "还在跑", "关掉窗口会一起停掉，确定？") != QMessageBox.Yes:
                e.ignore()
                return
            self.runner.stop()
        e.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = Workbench()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
