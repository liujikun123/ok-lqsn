"""「运行设置」面板：把 strategy.yaml / AutoTask.json 里该调的项做成能点的控件。

设计约束（很重要，别破坏）：

  · **界面只是 yaml 的编辑器**，不做第二套状态。每个控件对应
    `workbench.settings` 里的一个 Setting，读写都走那一层，
    所以「界面跑」和「命令行跑」行为必然一致。
  · 保存前会先把 diff 打出来（哪个键从多少变成多少），不做静默写入。
  · 保存时会自动备份到 `config/.backup/`（只留最近 20 份）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from workbench import settings as S

MUTED = "#8a9099"
TEXT = "#1f2329"
ACCENT = "#2f6fed"
WARN = "#c47f00"
BAD = "#d93025"
OKC = "#1a9f5a"

WEEK_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


class _Field(QWidget):
    """一行控件：左边标题，右边输入，下面一行浅灰说明。"""

    changed = Signal()

    def __init__(self, st: S.Setting, value, parent=None):
        super().__init__(parent)
        self.st = st
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 6)
        lay.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(8)
        lab = QLabel(st.label)
        lab.setMinimumWidth(150)
        lab.setStyleSheet(f"color:{TEXT}; font-weight:500;")
        row.addWidget(lab, 0, Qt.AlignTop)

        self.widget = self._make(st, value)
        row.addWidget(self.widget, 1)
        if st.unit:
            u = QLabel(st.unit)
            u.setStyleSheet(f"color:{MUTED};")
            row.addWidget(u)
        lay.addLayout(row)

        if st.help:
            tip = QLabel(st.help)
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color:{MUTED}; font-size:11px; padding-left:158px;")
            lay.addWidget(tip)

    def _make(self, st: S.Setting, value) -> QWidget:
        if st.kind == "switch":
            w = QCheckBox("开启")
            w.setChecked(bool(value))
            w.stateChanged.connect(lambda *_: self.changed.emit())
            return w
        if st.kind == "int":
            w = QSpinBox()
            w.setRange(st.minimum, st.maximum)
            w.setSingleStep(st.step)
            w.setValue(int(value if value is not None else st.minimum))
            w.valueChanged.connect(lambda *_: self.changed.emit())
            return w
        if st.kind == "choice":
            w = QComboBox()
            w.addItems(st.choices)
            if value in st.choices:
                w.setCurrentText(str(value))
            w.currentTextChanged.connect(lambda *_: self.changed.emit())
            return w
        if st.kind == "weekdays":
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            chosen = set(int(x) for x in (value or []))
            self._days: dict[int, QCheckBox] = {}
            for i, name in enumerate(WEEK_NAMES, start=1):
                cb = QCheckBox(name)
                cb.setChecked(i in chosen)
                cb.stateChanged.connect(lambda *_: self.changed.emit())
                cb.setToolTip(f"勾上 = 周{name}不进店")
                self._days[i] = cb
                h.addWidget(cb)
            h.addStretch(1)
            return box
        w = QLabel(str(value))
        return w

    def value(self):
        st = self.st
        if st.kind == "switch":
            return self.widget.isChecked()
        if st.kind == "int":
            return self.widget.value()
        if st.kind == "choice":
            return self.widget.currentText()
        if st.kind == "weekdays":
            return [i for i, cb in self._days.items() if cb.isChecked()]
        return None

    def set_value(self, v) -> None:
        st = self.st
        if st.kind == "switch":
            self.widget.setChecked(bool(v))
        elif st.kind == "int":
            self.widget.setValue(int(v))
        elif st.kind == "choice":
            self.widget.setCurrentText(str(v))
        elif st.kind == "weekdays":
            chosen = set(int(x) for x in (v or []))
            for i, cb in self._days.items():
                cb.setChecked(i in chosen)


class SettingsPanel(QWidget):
    """整块设置面板。saved = 已经落盘的配置快照。"""

    log = Signal(str, str)     # (级别, 文本) → 接到主窗口日志

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root
        self.docs = S.open_docs(root)
        self.saved = S.snapshot(self.docs)
        self.fields: dict[str, _Field] = {}
        self.other_rule = None      # 主窗口正在看、但面板不负责的规则文件名
        self._build()
        self.reload()

    # ------------------------------------------------------------ 外观
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        head = QHBoxLayout()
        self.lab_state = QLabel("")
        self.lab_state.setStyleSheet(f"color:{MUTED};")
        head.addWidget(self.lab_state, 1)
        self.btn_revert = QPushButton("撤销修改")
        self.btn_revert.clicked.connect(self.revert)
        self.btn_save = QPushButton("保存到配置")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        head.addWidget(self.btn_revert)
        head.addWidget(self.btn_save)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        grid = QVBoxLayout(body)
        grid.setContentsMargins(0, 0, 6, 0)
        grid.setSpacing(10)

        for group, items in S.SPEC:
            card = QFrame()
            card.setStyleSheet(
                "QFrame{background:#ffffff;border:1px solid #e3e6eb;border-radius:10px;}")
            v = QVBoxLayout(card)
            v.setContentsMargins(12, 8, 12, 8)
            v.setSpacing(0)
            title = QLabel(group)
            title.setStyleSheet(f"color:{MUTED}; font-weight:600; font-size:12px;")
            v.addWidget(title)
            for st in items:
                f = _Field(st, self.saved.get(st.id))
                f.changed.connect(self.refresh_dirty)
                self.fields[st.id] = f
                v.addWidget(f)
            grid.addWidget(card)
        grid.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self.lab_warn = QLabel("")
        self.lab_warn.setWordWrap(True)
        self.lab_warn.setStyleSheet(f"color:{WARN}; font-size:11px;")
        outer.addWidget(self.lab_warn)

    # ------------------------------------------------------------ 数据
    def values(self) -> dict:
        return {k: f.value() for k, f in self.fields.items()}

    def bind_yaml(self, path: str) -> None:
        """主窗口切换规则文件时回调。

        面板只编辑 `config/strategy.yaml`（控件都绑在它的键上）。切换文件时：
          · 选中的还是它 → 重读（有未保存改动就不覆盖，免得白改）；
          · 选中的是别的规则文件 → 面板保持不动，并显式提示，
            不让人以为「界面改的就是正在看的那份」。
        """
        try:
            own = os.path.abspath(os.path.join(self.root, S.DEFAULT_PATHS["yaml"]))
            cur = os.path.abspath(os.path.join(self.root, path))
        except Exception:
            return
        self.other_rule = None
        if cur == own:
            if self.dirty_ids():
                self.log.emit("warn", "设置面板有未保存改动，切换规则时没重读（先保存或撤销）")
            else:
                self.reload()
        else:
            self.other_rule = os.path.basename(path)
            self.refresh_dirty()

    def reload(self) -> None:
        """从磁盘重读（会丢掉未保存的修改）。"""
        self.docs = S.open_docs(self.root)
        self.saved = S.snapshot(self.docs)
        for k, f in self.fields.items():
            f.set_value(self.saved.get(k))
        self.refresh_dirty()

    def revert(self) -> None:
        for k, f in self.fields.items():
            f.set_value(self.saved.get(k))
        self.refresh_dirty()
        self.log.emit("info", "已撤销未保存的修改")

    def dirty_ids(self) -> list[str]:
        cur = self.values()
        return [k for k, v in cur.items() if v != self.saved.get(k)]

    def refresh_dirty(self) -> None:
        cur = self.values()
        dirty = [k for k, v in cur.items() if v != self.saved.get(k)]
        if self.other_rule:
            self.lab_state.setText(f"正在查看「{self.other_rule}」· 设置面板只改 strategy.yaml")
            self.lab_state.setStyleSheet(f"color:{WARN};")
        elif dirty:
            names = "、".join(S.index_by_id()[k].label for k in dirty)
            self.lab_state.setText(f"有 {len(dirty)} 项未保存：{names}")
            self.lab_state.setStyleSheet(f"color:{WARN};")
            self.btn_save.setText(f"保存到配置（{len(dirty)}）")
        else:
            self.lab_state.setText("配置已同步")
            self.lab_state.setStyleSheet(f"color:{OKC};")
            self.btn_save.setText("保存到配置")
        self.btn_save.setEnabled(bool(dirty))
        self.btn_revert.setEnabled(bool(dirty))

        msgs = S.validate(cur, self.docs)
        self.lab_warn.setText("\n".join("· " + m for m in msgs))
        self.lab_warn.setVisible(bool(msgs))

    # ------------------------------------------------------------ 保存
    def save(self) -> None:
        cur = self.values()
        idx = S.index_by_id()
        changes: list[str] = []
        for k in self.dirty_ids():
            for doc, path, old, new in idx[k].write(self.docs, cur[k]):
                changes.append(f"{path}：{old!r} → {new!r}")
        if not changes:
            self.refresh_dirty()
            return
        try:
            baks = S.save_docs(self.docs, self.root)
        except Exception as e:
            self.log.emit("error", f"保存失败：{e}")
            return
        self.log.emit("info", f"已保存 {len(changes)} 处配置修改：")
        for c in changes:
            self.log.emit("info", f"    {c}")
        if baks:
            self.log.emit("info", f"    备份：{'、'.join(baks)}")
        self.saved = cur
        self.refresh_dirty()

    def dirty_or_none(self) -> list[str]:
        return self.dirty_ids()
