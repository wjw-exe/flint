# -*- coding: utf-8 -*-
"""
flint_ui.py — Flint 纯 UI 库引擎 (PyQt6)

哲学: **纯 UI, 一切计算交给后端**。
* 引擎只负责: 组件创建/渲染/输入事件捕获(按钮点击、滑块拖动、复选框)
* 业务逻辑全部由 Flint 程序(后端)完成: 轮询组件状态 → 计算 → 更新组件
* 无回调、无对象: 组件以整数 ID 引用, 状态经 TRAP 29-41 读写

架构(与 flint_engine.py 同款):
* VM 线程(Flint 程序)通过 TRAP 29-41 声明组件/读状态/更新组件
* Qt 主线程(QApplication + QWidget)渲染组件/收集交互
* 线程安全: 命令队列与共享状态均由锁保护; Qt 定时器消费命令队列

内置(Flint 侧):
  ui_window(w,h) ui_button(x,y,w,h,label)->id ui_label(x,y,text)->id
  ui_slider(x,y,w,min,max,val)->id ui_progress(x,y,w,val)->id
  ui_checkbox(x,y,label,checked)->id
  ui_clicked(id)->1/0(边沿) ui_value(id)->当前值 ui_checked(id)->0/1
  ui_set_text(id,text) ui_set_value(id,v)
  ui_present() ui_closed()->1/0
"""

import sys
import threading

from PyQt6.QtWidgets import (QApplication, QWidget, QPushButton, QLabel,
                             QSlider, QProgressBar, QCheckBox)
from PyQt6.QtCore import Qt, QTimer


class UiWidget(QWidget):
    """UI 窗口: Qt 线程消费引擎的组件命令队列, 组件绝对定位。"""

    def __init__(self, eng):
        super().__init__()
        self.eng = eng
        self.setWindowTitle("Flint UI")

    def closeEvent(self, ev):
        with self.eng.lock:
            self.eng.closed = True
        super().closeEvent(ev)


class UiEngine:
    """纯 UI 引擎: 跨线程组件命令/状态队列 + Qt 应用生命周期。"""

    def __init__(self, width=420, height=320):
        self.width = width
        self.height = height
        self.lock = threading.Lock()
        self.cmds = []        # VM → Qt 组件命令队列
        self.id_seq = 0       # 组件 ID 分配器(VM 线程)
        self.states = {}      # id → {kind, clicked, value, checked, text}
        self.closed = False
        self.app = None
        self.win = None
        self.ready = threading.Event()
        self.max_frames = None
        self.frames = 0
        self._win_req = None

    # ================= VM 线程侧(Flint 程序调用) =================
    # 只做: 分配 ID、登记状态、入队组件命令。不直接碰 Qt 组件。

    def wait_ready(self, timeout=10.0):
        self.ready.wait(timeout)

    def ui_window(self, w, h):
        if w <= 0 or h <= 0 or w > 4000 or h > 4000:
            return 0
        with self.lock:
            self._win_req = (w, h)
        return 0

    def _new_id(self, kind, **kw):
        with self.lock:
            self.id_seq += 1
            i = self.id_seq
            self.states[i] = {"kind": kind, "clicked": 0, "value": 0,
                              "checked": 0, "text": ""}
            self.states[i].update(kw)
            return i

    def ui_button(self, x, y, w, h, label):
        i = self._new_id("button")
        with self.lock:
            self.cmds.append(("button", i, x, y, w, h, label))
        return i

    def ui_label(self, x, y, text):
        i = self._new_id("label", text=text)
        with self.lock:
            self.cmds.append(("label", i, x, y, text))
        return i

    def ui_slider(self, x, y, w, lo, hi, val):
        i = self._new_id("slider", value=val)
        with self.lock:
            self.cmds.append(("slider", i, x, y, w, lo, hi, val))
        return i

    def ui_progress(self, x, y, w, val):
        i = self._new_id("progress", value=val)
        with self.lock:
            self.cmds.append(("progress", i, x, y, w, val))
        return i

    def ui_checkbox(self, x, y, label, checked):
        i = self._new_id("checkbox", checked=checked, text=label)
        with self.lock:
            self.cmds.append(("checkbox", i, x, y, label, checked))
        return i

    def ui_clicked(self, i):
        """边沿触发: 返回 1 并清零(只在被点击的那一帧读到 1)。"""
        with self.lock:
            s = self.states.get(i)
            if s is None:
                return 0
            v = s["clicked"]
            s["clicked"] = 0
            return v

    def ui_value(self, i):
        with self.lock:
            s = self.states.get(i)
            return s["value"] if s else 0

    def ui_checked(self, i):
        with self.lock:
            s = self.states.get(i)
            return 1 if (s and s["checked"]) else 0

    def ui_set_text(self, i, text):
        with self.lock:
            s = self.states.get(i)
            if s:
                s["text"] = text
            self.cmds.append(("set_text", i, text))
        return 0

    def ui_set_value(self, i, v):
        with self.lock:
            s = self.states.get(i)
            if s:
                s["value"] = v
            self.cmds.append(("set_value", i, v))
        return 0

    def ui_present(self):
        return 0

    def ui_closed(self):
        with self.lock:
            return 1 if self.closed else 0

    # ================= Qt 主线程侧 =================

    def _apply_resize(self):
        with self.lock:
            req = self._win_req
            self._win_req = None
        if req and self.win is not None:
            self.win.resize(req[0], req[1])

    def _mark_clicked(self, i):
        with self.lock:
            s = self.states.get(i)
            if s:
                s["clicked"] = 1

    def _tick(self):
        self.frames += 1
        self._apply_resize()
        with self.lock:
            cmds = list(self.cmds)
            self.cmds = []
        for c in cmds:
            self._apply_cmd(c)
        if self.win is not None:
            self.win.update()
        if self.max_frames is not None and self.frames >= self.max_frames:
            with self.lock:
                self.closed = True
            self.app.quit()
            return
        if self.vm_done is not None and self.vm_done.is_set():
            if getattr(self, "close_on_done", False):
                self.close()
            else:
                self.app.quit()
            return

    def _apply_cmd(self, c):
        """Qt 线程: 创建/更新组件。"""
        try:
            op = c[0]
            if op == "button":
                _, i, x, y, w, h, label = c
                b = QPushButton(label, self.win)
                b.setGeometry(x, y, w, h)
                b.clicked.connect(lambda _=False, i=i: self._mark_clicked(i))
                b.show()
                self._widgets[i] = b
            elif op == "label":
                _, i, x, y, text = c
                lb = QLabel(text, self.win)
                lb.setGeometry(x, y, max(1, self.width - x - 8), 24)
                lb.show()
                self._widgets[i] = lb
            elif op == "slider":
                _, i, x, y, w, lo, hi, val = c
                s = QSlider(Qt.Orientation.Horizontal, self.win)
                s.setGeometry(x, y, w, 24)
                s.setRange(lo, hi)
                s.setValue(val)
                s.valueChanged.connect(lambda v, i=i: self._slider_changed(i, v))
                s.show()
                self._widgets[i] = s
            elif op == "progress":
                _, i, x, y, w, val = c
                p = QProgressBar(self.win)
                p.setGeometry(x, y, w, 20)
                p.setRange(0, 100)
                p.setValue(val)
                p.show()
                self._widgets[i] = p
            elif op == "checkbox":
                _, i, x, y, label, checked = c
                cb = QCheckBox(label, self.win)
                cb.setGeometry(x, y, 160, 24)
                cb.setChecked(bool(checked))
                cb.toggled.connect(lambda v, i=i: self._check_toggled(i, v))
                cb.show()
                self._widgets[i] = cb
            elif op == "set_text":
                _, i, text = c
                wd = self._widgets.get(i)
                if wd is not None and isinstance(wd, (QLabel, QPushButton, QCheckBox)):
                    wd.setText(text)
            elif op == "set_value":
                _, i, v = c
                wd = self._widgets.get(i)
                if isinstance(wd, QSlider):
                    wd.setValue(v)
                elif isinstance(wd, QProgressBar):
                    wd.setValue(v)
                elif isinstance(wd, QCheckBox):
                    wd.setChecked(bool(v))
        except Exception:
            pass  # 组件异常不拖垮引擎

    def _slider_changed(self, i, v):
        with self.lock:
            s = self.states.get(i)
            if s:
                s["value"] = v

    def _check_toggled(self, i, v):
        with self.lock:
            s = self.states.get(i)
            if s:
                s["checked"] = 1 if v else 0

    def run_app(self, max_frames=None, vm_done=None):
        self.max_frames = max_frames
        self.vm_done = vm_done
        self._widgets = {}
        self.app = QApplication(sys.argv[:1])
        self._create_window()
        self.ready.set()
        return self.app.exec()

    def attach(self, vm_done=None, max_frames=None, close_on_done=True):
        """挂到已存在的 QApplication(IDE 集成)。"""
        self.vm_done = vm_done
        self.max_frames = max_frames
        self.close_on_done = close_on_done
        self._widgets = {}
        self.app = QApplication.instance()
        self._create_window()
        self.ready.set()

    def _create_window(self):
        self.win = UiWidget(self)
        self.win.resize(self.width, self.height)
        self.win.show()
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def close(self):
        with self.lock:
            self.closed = True
        if self.win is not None:
            self.win.close()
