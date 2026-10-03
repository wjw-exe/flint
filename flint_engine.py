# -*- coding: utf-8 -*-
"""
flint_engine.py — Flint 2D 游戏引擎 (PyQt6)

架构:
* VM 线程(Flint 程序)通过 TRAP 20-28 系统调用提交图元命令 / 读输入
* Qt 主线程(QApplication + QWidget)渲染帧 / 收集键盘事件
* 线程安全: 命令列表与按键队列均由锁保护
* 16 色调色板(索引 0-15)

内置(Flint 侧): window(w,h) clear(c) fill_rect(x,y,w,h,c) fill_circle(x,y,r,c)
              draw_line(x1,y1,x2,y2,c) draw_char(x,y,size,ch,c)
              poll_key() window_closed() present()
"""

import sys
import threading

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtGui import QPainter, QColor, QFont, QPen
from PyQt6.QtCore import Qt, QTimer

PALETTE = [
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0xFF, 0x4B, 0x4B), (0x4B, 0xFF, 0x6B),
    (0x4B, 0x6B, 0xFF), (0xFF, 0xE1, 0x4B), (0x4B, 0xFF, 0xFF), (0xC4, 0x4B, 0xFF),
    (0xFF, 0x9A, 0x3C), (0x3C, 0x3C, 0x3C), (0x9A, 0x9A, 0x9A), (0xFF, 0x7E, 0xB6),
    (0x8B, 0x5A, 0x2B), (0x1E, 0x7A, 0x2E), (0x1E, 0x3A, 0x8B), (0x8B, 0x1E, 0x1E),
]


class GfxWidget(QWidget):
    """渲染窗口: Qt 线程消费引擎的待渲染帧, 收集键盘事件。"""

    def __init__(self, eng):
        super().__init__()
        self.eng = eng
        self.setWindowTitle("Flint GFX")
        self._font = QFont("Consolas", 16)

    def paintEvent(self, event):
        p = QPainter(self)
        with self.eng.lock:
            frame = list(self.eng.pending)
        for c in frame:
            kind = c[0]
            try:
                if kind == "clear":
                    p.fillRect(0, 0, self.width(), self.height(), QColor(*PALETTE[c[1] & 15]))
                elif kind == "rect":
                    p.fillRect(c[1], c[2], c[3], c[4], QColor(*PALETTE[c[5] & 15]))
                elif kind == "circle":
                    r = c[3]
                    p.setBrush(QColor(*PALETTE[c[4] & 15]))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(c[1] - r, c[2] - r, r * 2, r * 2)
                elif kind == "line":
                    pen = QPen(QColor(*PALETTE[c[5] & 15]))
                    pen.setWidth(max(1, c[6]) if len(c) > 6 else 1)
                    p.setPen(pen)
                    p.drawLine(c[1], c[2], c[3], c[4])
                elif kind == "char":
                    f = QFont("Consolas", max(1, c[3]))
                    p.setFont(f)
                    p.setPen(QColor(*PALETTE[c[5] & 15]))
                    p.drawText(c[1], c[2], chr(c[4] & 0xFF))
            except Exception:
                pass  # 渲染异常不拖垮整个引擎

    def keyPressEvent(self, ev):
        with self.eng.lock:
            if not self.eng.closed and len(self.eng.key_q) < 64:
                t = ev.text()
                if t:
                    self.eng.key_q.append(ord(t[0]))
                elif ev.key() == Qt.Key.Key_Left:
                    self.eng.key_q.append(0x100)
                elif ev.key() == Qt.Key.Key_Up:
                    self.eng.key_q.append(0x101)
                elif ev.key() == Qt.Key.Key_Right:
                    self.eng.key_q.append(0x102)
                elif ev.key() == Qt.Key.Key_Down:
                    self.eng.key_q.append(0x103)
                elif ev.key() == Qt.Key.Key_Escape:
                    self.eng.key_q.append(27)
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        with self.eng.lock:
            self.eng.closed = True
        super().closeEvent(ev)


class Engine:
    """2D 游戏引擎: 跨线程命令/事件队列 + Qt 应用生命周期。"""

    def __init__(self, width=640, height=480):
        self.width = width
        self.height = height
        self.lock = threading.Lock()
        self.cmds = []          # VM 线程当前帧命令(写)
        self.pending = []       # Qt 线程待渲染帧
        self.key_q = []         # 按键队列(VM 读)
        self.closed = False
        self.app = None
        self.win = None
        self.ready = threading.Event()
        self.max_frames = None
        self.frames = 0
        self._win_req = None

    # ---------- VM 线程侧(Flint 程序调用) ----------
    def wait_ready(self, timeout=10.0):
        self.ready.wait(timeout)

    def syscall(self, code, regs):
        if code == 20:                       # window(w, h)
            self._req_resize(regs[0], regs[1])
            return 0
        if code == 21:                       # clear(c)
            with self.lock:
                self.cmds.append(("clear", regs[0] & 15))
            return 0
        if code == 22:                       # fill_rect(x, y, w, h, c)
            with self.lock:
                self.cmds.append(("rect", regs[0], regs[1], regs[2], regs[3], regs[4] & 15))
            return 0
        if code == 23:                       # fill_circle(x, y, r, c)
            with self.lock:
                self.cmds.append(("circle", regs[0], regs[1], regs[2], regs[3] & 15))
            return 0
        if code == 24:                       # draw_line(x1, y1, x2, y2, c)
            with self.lock:
                self.cmds.append(("line", regs[0], regs[1], regs[2], regs[3], regs[4] & 15, 1))
            return 0
        if code == 25:                       # draw_char(x, y, size, ch, c)
            with self.lock:
                self.cmds.append(("char", regs[0], regs[1], regs[2], regs[3] & 0xFF, regs[4] & 15))
            return 0
        if code == 26:                       # poll_key()
            with self.lock:
                if self.closed:
                    return -1
                if self.key_q:
                    return self.key_q.pop(0)
                return 0
        if code == 27:                       # window_closed()
            with self.lock:
                return 1 if self.closed else 0
        if code == 28:                       # present()
            with self.lock:
                self.pending = list(self.cmds)
                self.cmds = []
            if self.win is not None:
                self.win.update()
            return 0
        return 0

    def _req_resize(self, w, h):
        if w <= 0 or h <= 0 or w > 4000 or h > 4000:
            return
        with self.lock:
            self._win_req = (w, h)
        if self.win is not None:
            self.win.update()

    # ---------- Qt 主线程侧 ----------
    def _apply_resize(self):
        with self.lock:
            req = self._win_req
            self._win_req = None
        if req and self.win is not None:
            self.win.resize(req[0], req[1])

    def _tick(self):
        self.frames += 1
        self._apply_resize()
        if self.max_frames is not None and self.frames >= self.max_frames:
            with self.lock:
                self.closed = True
            self.app.quit()
            return
        if self.vm_done is not None and self.vm_done.is_set():
            self.app.quit()
            return

    def run_app(self, max_frames=None, vm_done=None):
        self.max_frames = max_frames
        self.vm_done = vm_done
        self.app = QApplication(sys.argv[:1])
        self.win = GfxWidget(self)
        self.win.resize(self.width, self.height)
        self.win.show()
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)
        self.ready.set()
        return self.app.exec()
