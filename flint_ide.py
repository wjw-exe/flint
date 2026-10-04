#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flint_ide.py — 燧石语言 Flint v3.2.1 专属 IDE (PyQt6)

功能
----
* 代码编辑器: 语法高亮 / 行号 / 当前行高亮 / 编译错误自动跳转并标红
* 一键运行: 词法 → 语法 → 静态类型检查 → 代码生成 → 汇编 → 虚拟机执行
* 底部四视图: 输出 / 汇编(编译产物) / 错误 / 输入(stdin)
* 停止执行(可打断死循环), 运行统计(退出码/执行指令数/耗时)
* 深色主题, 快捷键齐全

依赖: PyQt6        (pip install PyQt6)
运行: python3 flint_ide.py [文件.fl]
"""

import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# 兼容两种布局: 仓库/Windows 包内 flint-lang 与本目录同级; Linux 开发布局 flint-lang 与上级目录同级
for _cand in (
    os.path.join(_HERE, "flint-lang"),
    os.path.join(os.path.dirname(_HERE), "flint-lang"),
):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break

try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRect, QSize
    from PyQt6.QtGui import (QFontDatabase, QFont, QColor, QTextCharFormat,
                             QSyntaxHighlighter, QPainter, QTextCursor,
                             QKeySequence, QAction, QTextFormat)
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QPlainTextEdit,
                                 QTabWidget, QSplitter, QToolBar, QLabel,
                                 QStatusBar, QFileDialog, QMessageBox,
                                 QWidget, QTextEdit)
except ImportError:
    sys.stderr.write("缺少 PyQt6, 请先安装: pip install PyQt6\n")
    sys.exit(1)

from lexer import tokenize, LexError
from parser import Parser, ParseError
from typecheck import TypeChecker, TypeCheckError
from codegen import CodeGen

try:
    from flint_lang.assembler import assemble, AsmError
    from flint_lang.vm import VM, VMError
except ImportError:
    sys.stderr.write("缺少依赖: 请把 flint-v2 与 flint-lang 放在同一级目录下\n")
    sys.exit(1)

__version__ = "3.2.1"

MAX_STEPS = 20_000_000          # 运行步数上限(防死循环, 可停止)

STYLESHEET = """
QMainWindow, QDialog { background: #282c34; }
QPlainTextEdit { background: #1e222a; color: #abb2bf; border: 1px solid #3b4048;
                 selection-background-color: #3d566f; }
QTabWidget::pane { border: 1px solid #3b4048; background: #1e222a; }
QTabBar::tab { background: #21252b; color: #7f848e; padding: 6px 16px; border: 1px solid #3b4048; }
QTabBar::tab:selected { background: #2c313c; color: #e6e6e6; }
QMenuBar { background: #21252b; color: #abb2bf; }
QMenuBar::item:selected { background: #2c313c; }
QMenu { background: #21252b; color: #abb2bf; }
QMenu::item:selected { background: #3b4048; }
QToolBar { background: #21252b; border-bottom: 1px solid #3b4048; spacing: 2px; }
QToolButton { padding: 5px 12px; color: #abb2bf; background: transparent; border: none; }
QToolButton:hover { background: #2c313c; }
QStatusBar { background: #21252b; color: #8a919c; }
QStatusBar QLabel { color: #8a919c; }
QLineEdit { background: #1e222a; color: #abb2bf; border: 1px solid #3b4048; padding: 2px 4px; }
"""


# =====================================================================
# 语法高亮
# =====================================================================
class FlintHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        kw = QTextCharFormat()
        kw.setForeground(QColor("#c678dd"))
        kw.setFontWeight(QFont.Weight.Bold)
        typ = QTextCharFormat()
        typ.setForeground(QColor("#56b6c2"))
        typ.setFontWeight(QFont.Weight.Bold)
        st = QTextCharFormat()
        st.setForeground(QColor("#98c379"))
        num = QTextCharFormat()
        num.setForeground(QColor("#d19a66"))
        com = QTextCharFormat()
        com.setForeground(QColor("#5c6370"))
        com.setFontItalic(True)
        fn = QTextCharFormat()
        fn.setForeground(QColor("#61afef"))

        self.rules = [
            (r"#[^\n]*", com),
            (r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', st),
            (r"\b\d+\b", num),
            (r"\b(?:def|return|if|elif|else|while|for|in|range|print|input|True|False|and|or|not|list|break|continue|pass|len|abs|min|max|sum|pow)\b", kw),
            (r"\b(?:i32|bool|str)\b", typ),
            (r"\b(?:main|fib|is_prime)\b", fn),  # 常见函数名提示色
        ]

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            for m in re.finditer(pattern, text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


# =====================================================================
# 行号区 + 代码编辑器
# =====================================================================
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_paint_event(event)


class CodeEditor(QPlainTextEdit):
    """带行号、当前行高亮、错误行高亮的代码编辑器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_number_area = LineNumberArea(self)
        self._error_line = None

        self.blockCountChanged.connect(self._update_line_area_width)
        self.updateRequest.connect(self._update_line_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._update_line_area_width(0)
        self._highlight_current_line()

    # ---- 行号 ----
    def line_number_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_area_width(self, _):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(0, rect.y(), self._line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def line_number_paint_event(self, event):
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#21252b"))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#5c6370"))
                painter.drawText(0, top, self._line_number_area.width() - 8,
                                 self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1

    # ---- 高亮 ----
    def _error_selections(self):
        sels = []
        if self._error_line is not None and 1 <= self._error_line <= self.blockCount():
            b = self.document().findBlockByNumber(self._error_line - 1)
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#5c1a1a"))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = QTextCursor(b)
            sel.cursor.clearSelection()
            sels.append(sel)
        return sels

    def _highlight_current_line(self):
        sels = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#2c313c"))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            sels.append(sel)
        sels += self._error_selections()
        self.setExtraSelections(sels)

    def set_error_line(self, line):
        """跳转到错误行并标红。line 从 1 开始, None 清除。"""
        self._error_line = line
        self._highlight_current_line()
        if line is not None and 1 <= line <= self.blockCount():
            b = self.document().findBlockByNumber(line - 1)
            cur = QTextCursor(b)
            self.setTextCursor(cur)
            self.centerCursor()


# =====================================================================
# 后台运行线程: 编译 + 虚拟机执行
# =====================================================================
class RunWorker(QThread):
    done = pyqtSignal(dict)
    attach_gfx = pyqtSignal(object)   # 程序含图形内置时发出引擎实例(主线程 attach)

    def __init__(self, source: str, input_data: bytes, max_steps: int = MAX_STEPS, parent=None):
        super().__init__(parent)
        self.source = source
        self.input_data = input_data
        self.max_steps = max_steps

    def run(self):
        t0 = time.perf_counter()
        result = {"ok": False, "output": b"", "exit_code": None,
                  "steps": 0, "asm": "", "error": "", "kind": "", "line": None}
        try:
            tokens = tokenize(self.source)
            prog = Parser(tokens).parse_program()
            TypeChecker().check(prog)
            asm = CodeGen().generate(prog)
            image, _ = assemble(asm)
            eng = None
            if re.search(r"TRAP 2[0-8]\b", asm):
                # 程序用了图形内置 → 挂 2D 游戏引擎(窗口由主线程创建)
                from flint_engine import Engine
                from threading import Event
                eng = Engine()
                self.gfx_eng = eng
                self.gfx_done = Event()
                self.attach_gfx.emit(eng)
            vm = VM(image, input_data=self.input_data, gfx=eng)
            out = vm.run(max_steps=self.max_steps)
            if eng is not None:
                self.gfx_done.set()
            result.update(ok=True, output=bytes(out), exit_code=vm.exit_code,
                          steps=vm.steps, asm=asm)
        except (LexError, ParseError, TypeCheckError) as e:
            msg = str(e)
            m = re.search(r"第 (\d+) 行", msg)
            result.update(error=msg, kind="编译错误", line=int(m.group(1)) if m else None)
        except AsmError as e:
            result.update(error=str(e), kind="汇编错误")
        except VMError as e:
            result.update(error=str(e), kind="运行时错误")
        except Exception as e:  # noqa: BLE001 —— IDE 兜底, 任何异常都要反馈给用户
            result.update(error=f"{type(e).__name__}: {e}", kind="内部错误")
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        self.done.emit(result)


# =====================================================================
# 主窗口
# =====================================================================
TEMPLATE = """# 新建 Flint v2 程序
def main() -> i32:
    print("Hello, Flint IDE!")
    return 0
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_path = None
        self.worker = None
        self._build_ui()
        self._build_menu_toolbar()
        self._connect_signals()
        self.setWindowTitle("Flint IDE — 燧石语言 v2[*]")
        self.resize(1080, 760)

    # ---------------- UI ----------------
    def _build_ui(self):
        # 编辑器
        self.editor = CodeEditor()
        self.highlighter = FlintHighlighter(self.editor.document())

        # 底部四视图
        self.tab_output = QPlainTextEdit()
        self.tab_output.setReadOnly(True)
        self.tab_output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tab_asm = QPlainTextEdit()
        self.tab_asm.setReadOnly(True)
        self.tab_asm.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tab_error = QPlainTextEdit()
        self.tab_error.setReadOnly(True)
        self.tab_input = QPlainTextEdit()
        self.tab_input.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tab_input.setPlaceholderText("这里的内容会作为程序的 stdin(输入)字节流…")

        self.tabs = QTabWidget()
        for t, n in [(self.tab_output, "输出"), (self.tab_asm, "汇编"), (self.tab_error, "错误"), (self.tab_input, "输入")]:
            self.tabs.addTab(t, n)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.editor)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # 状态栏
        sb = self.statusBar()
        self.lbl_cursor = QLabel("行 1, 列 1")
        self.lbl_run = QLabel("就绪")
        sb.addWidget(self.lbl_cursor)
        sb.addPermanentWidget(self.lbl_run)

    def _build_menu_toolbar(self):
        act = lambda text, slot, sc=None, tip="": (lambda a: (a.triggered.connect(slot),
                                                             a.setShortcut(sc) if sc else None,
                                                             a.setStatusTip(tip), a))(
            QAction(text, self))[-1]

        m_file = self.menuBar().addMenu("文件(&F)")
        m_file.addAction(act("新建", self.new_file, QKeySequence.StandardKey.New, "新建程序"))
        m_file.addAction(act("打开…", self.open_file_dialog, QKeySequence.StandardKey.Open, "打开 .fl 文件"))
        m_file.addAction(act("保存", self.save_file, QKeySequence.StandardKey.Save, "保存当前文件"))
        m_file.addAction(act("另存为…", self.save_file_as, QKeySequence.StandardKey.SaveAs))
        m_file.addSeparator()
        m_file.addAction(act("退出", self.close, QKeySequence.StandardKey.Quit))

        m_run = self.menuBar().addMenu("运行(&R)")
        m_run.addAction(act("运行 (F5)", self.run_program, "F5", "编译并运行当前程序"))
        m_run.addAction(act("停止", self.stop_program, "F6", "终止正在运行的程序"))
        m_run.addAction(act("仅编译, 查看汇编", self.compile_only, "Ctrl+E", "只做编译, 不执行"))

        m_help = self.menuBar().addMenu("帮助(&H)")
        m_help.addAction(act("关于", self.about))

        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        tb.addAction(act("新建", self.new_file))
        tb.addAction(act("打开", self.open_file_dialog))
        tb.addAction(act("保存", self.save_file))
        tb.addSeparator()
        tb.addAction(act("▶ 运行", self.run_program))
        tb.addAction(act("■ 停止", self.stop_program))
        tb.addAction(act("编译", self.compile_only))
        self.addToolBar(tb)

    def _connect_signals(self):
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.cursorPositionChanged.connect(self._update_cursor)

    # ---------------- 文件 ----------------
    def _on_text_changed(self):
        self.setWindowModified(True)
        self.lbl_run.setText("未保存的修改")

    def _update_cursor(self):
        c = self.editor.textCursor()
        self.lbl_cursor.setText(f"行 {c.blockNumber() + 1}, 列 {c.columnNumber() + 1}")

    def new_file(self):
        if self._maybe_save():
            self.editor.setPlainText(TEMPLATE)
            self.current_path = None
            self.setWindowTitle("Flint IDE — 燧石语言 v2[*]")
            self.setWindowModified(False)
            self.tab_error.clear()
            self.tab_asm.clear()

    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 Flint 源码", "", "Flint 源码 (*.fl);;所有文件 (*)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "打开失败", f"无法读取文件:\n{e}")
            return
        self.current_path = path
        self.editor.setPlainText(text)
        self.setWindowTitle(f"Flint IDE — {os.path.basename(path)}[*]")
        self.setWindowModified(False)
        self.tab_error.clear()
        self.tab_asm.clear()

    def save_file(self):
        if self.current_path is None:
            return self.save_file_as()
        return self._write_file(self.current_path)

    def save_file_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存 Flint 源码", "", "Flint 源码 (*.fl);;所有文件 (*)")
        if not path:
            return False
        return self._write_file(path)

    def _write_file(self, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.editor.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "保存失败", f"无法写入文件:\n{e}")
            return False
        self.current_path = path
        self.setWindowTitle(f"Flint IDE — {os.path.basename(path)}[*]")
        self.setWindowModified(False)
        self.lbl_run.setText("已保存")
        return True

    def _maybe_save(self):
        if self.isWindowModified():
            r = QMessageBox.question(self, "未保存", "当前文件有未保存的修改, 保存吗?",
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                     QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Save:
                return self.save_file()
            if r == QMessageBox.StandardButton.Cancel:
                return False
        return True

    def closeEvent(self, event):
        if self._maybe_save():
            if self.worker is not None and self.worker.isRunning():
                self.worker.terminate()
                self.worker.wait(1000)
            event.accept()
        else:
            event.ignore()

    # ---------------- 运行 ----------------
    def _source(self):
        return self.editor.toPlainText()

    def _input_bytes(self):
        return self.tab_input.toPlainText().encode("utf-8")

    def _start_worker(self, run_vm: bool):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "运行中", "程序正在运行, 请先停止。")
            return
        src = self._source()
        if not src.strip():
            QMessageBox.information(self, "空程序", "编辑器里还没有代码。")
            return
        self.tab_error.clear()
        self.editor.set_error_line(None)
        self.lbl_run.setText("编译中…")
        self.worker = RunWorker(src, self._input_bytes() if run_vm else b"", parent=self)
        self.worker.done.connect(self._on_done)
        self.worker.attach_gfx.connect(self._on_attach_gfx)
        self.worker.start()

    def run_program(self):
        self._start_worker(run_vm=True)

    def compile_only(self):
        self._start_worker(run_vm=False)

    def stop_program(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(1000)
            self.lbl_run.setText("已停止")

    def _on_attach_gfx(self, eng):
        """主线程: 为图形程序创建引擎窗口(不阻塞 IDE 事件循环)。"""
        try:
            eng.attach(vm_done=getattr(self.worker, "gfx_done", None), close_on_done=True)
        except Exception as e:  # noqa: BLE001
            self.tab_error.setPlainText(f"图形引擎启动失败: {e}")

    def _on_done(self, r):
        gfx = getattr(self.worker, "gfx_eng", None)
        if gfx is not None:
            gfx.close()
        self.lbl_run.setText(
            f"{r['kind'] or '完成'} · {r['elapsed_ms']} ms"
            + (f" · 退出码 {r['exit_code']} · {r['steps']} 条指令 · 输出 {len(r['output'])} 字节"
               if r["ok"] else ""))
        if r["asm"]:
            self.tab_asm.setPlainText(r["asm"])
        if r["ok"]:
            self.tab_output.setPlainText(r["output"].decode("utf-8", errors="replace"))
            self.tabs.setCurrentWidget(self.tab_output)
        else:
            self.tab_error.setPlainText(f"【{r['kind']}】\n{r['error']}")
            self.tabs.setCurrentWidget(self.tab_error)
            if r["line"] is not None:
                self.editor.set_error_line(r["line"])

    # ---------------- 其他 ----------------
    def about(self):
        QMessageBox.about(
            self, "关于 Flint IDE",
            f"<b>Flint IDE {__version__}</b><br><br>"
            "燧石语言 Flint v2 专属集成开发环境。<br>"
            "语法: Python 风格缩进 · 静态类型 · 编译型<br><br>"
            "一键运行会完成:<br>词法 → 语法 → 类型检查 → 代码生成 → 汇编 → 虚拟机执行。<br><br>"
            "底层复用 flint-lang 的汇编器与虚拟机(图灵完备)。")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Flint IDE")
    app.setStyleSheet(STYLESHEET)
    w = MainWindow()
    if len(sys.argv) > 1:
        w.open_file(sys.argv[1])
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
