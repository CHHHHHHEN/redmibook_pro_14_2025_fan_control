#!/usr/bin/env python3
"""TM2411 dual-fan curve editor.

Run as Administrator:
  python fan_gui.py

Tabs:
  Fan A (independent)   Fan A follows this curve only.
  Fan B (independent)   Fan B follows this curve only.
  联动（共用）           Both fans follow one shared curve.

Curve editing:
  left click empty plot area -> add a point
  left drag a point          -> move it
  right click a point        -> delete it
  Delete / Backspace         -> delete selected point
  at least two points are kept

Y axis is duty percent.  EC level is 0x80 = 100%, so percent is converted
with fanctl.percent_to_level(); the selected point shows the raw level.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import fanctl as fc

X_MIN = 30.0
X_MAX = 100.0
Y_MAX = 100.0
DEFAULT_POINTS = [
    [30, 0],
    [40, 20],
    [50, 40],
    [60, 60],
    [70, 80],
    [85, 100],
    [100, 100],
]
POLL_MS = 400
CONTROL_INTERVAL = 0.15
STATUS_INTERVAL = 1.5


def default_points() -> list[list[int]]:
    return [list(point) for point in DEFAULT_POINTS]


def curve_value(points, temp: float) -> float:
    pts = sorted(points, key=lambda point: point[0])
    if not pts:
        return 0.0
    if temp <= pts[0][0]:
        return float(pts[0][1])
    if temp >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= temp <= x1:
            if x1 == x0:
                return float(y1)
            return y0 + (y1 - y0) * (temp - x0) / (x1 - x0)
    return float(pts[-1][1])


class CurveEditor(ttk.Frame):
    def __init__(self, master, key: str, title: str, on_change) -> None:
        super().__init__(master, padding=8)
        self.key = key
        self.title = title
        self.on_change = on_change
        self.points = default_points()
        self.selected = None
        self.drag_index = None
        self.current_temp = 0
        self.protect = True
        self.info_var = tk.StringVar()

        self.canvas = tk.Canvas(
            self,
            bg="white",
            height=340,
            highlightthickness=1,
            highlightbackground="#cfcfcf",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_left)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right)
        self.canvas.bind("<Delete>", self._on_delete_key)
        self.canvas.bind("<BackSpace>", self._on_delete_key)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="添加点", command=self.add_point).pack(
            side=tk.LEFT
        )
        ttk.Button(
            bar, text="删除选中点", command=self.delete_selected
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="重置曲线", command=self.reset).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        ttk.Label(
            self,
            textvariable=self.info_var,
            font=("Consolas", 9),
            anchor=tk.W,
            wraplength=840,
        ).pack(fill=tk.X, pady=(6, 0))

        self._update_info()

    def _plot_rect(self):
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        left = 58
        right = max(left + 80, width - 24)
        top = 26
        bottom = max(top + 80, height - 46)
        return left, top, right, bottom

    def _sx(self, temp: float) -> float:
        left, _top, right, _bottom = self._plot_rect()
        return left + (temp - X_MIN) / (X_MAX - X_MIN) * (right - left)

    def _sy(self, percent: float) -> float:
        _left, top, _right, bottom = self._plot_rect()
        return bottom - percent / Y_MAX * (bottom - top)

    def _to_data(self, x: float, y: float):
        left, top, right, bottom = self._plot_rect()
        temp = X_MIN + (x - left) / max(1.0, right - left) * (X_MAX - X_MIN)
        percent = (bottom - y) / max(1.0, bottom - top) * Y_MAX
        return temp, percent

    def _in_plot(self, x: float, y: float) -> bool:
        left, top, right, bottom = self._plot_rect()
        return left <= x <= right and top <= y <= bottom

    def _hit_test(self, x: float, y: float):
        best = None
        best_dist = 10.0 * 10.0
        for index, (temp, percent) in enumerate(self.points):
            px = self._sx(temp)
            py = self._sy(percent)
            dist = (px - x) ** 2 + (py - y) ** 2
            if dist < best_dist:
                best = index
                best_dist = dist
        return best

    def _near_temp(self, temp: int, tolerance: int):
        for index, point in enumerate(self.points):
            if abs(point[0] - temp) < tolerance:
                return index
        return None

    def _index_for_temp(self, temp: int, percent: int) -> int:
        for index, point in enumerate(self.points):
            if point[0] == temp:
                return index
        return 0

    def _clamp_temp(self, index: int, temp: int) -> int:
        low = int(X_MIN)
        high = int(X_MAX)
        if index > 0:
            low = max(low, self.points[index - 1][0] + 1)
        if index < len(self.points) - 1:
            high = min(high, self.points[index + 1][0] - 1)
        if low > high:
            return self.points[index][0]
        return max(low, min(high, temp))

    def _on_left(self, event) -> None:
        self.canvas.focus_set()
        if not self._in_plot(event.x, event.y):
            self.selected = None
            self._update_info()
            self.redraw()
            return

        hit = self._hit_test(event.x, event.y)
        if hit is not None:
            self.selected = hit
            self.drag_index = hit
            self._update_info()
            self.redraw()
            return

        temp, percent = self._to_data(event.x, event.y)
        temp = max(int(X_MIN), min(int(X_MAX), int(round(temp))))
        percent = max(0, min(int(Y_MAX), int(round(percent))))

        near = self._near_temp(temp, 2)
        if near is not None:
            self.selected = near
            self.drag_index = near
            self._update_info()
            self.redraw()
            return

        self.points.append([temp, percent])
        self.points.sort(key=lambda point: point[0])
        self.selected = self._index_for_temp(temp, percent)
        self.drag_index = self.selected
        self._notify()
        self._update_info()
        self.redraw()

    def _on_motion(self, event) -> None:
        if self.drag_index is None:
            return
        temp, percent = self._to_data(event.x, event.y)
        temp = self._clamp_temp(
            self.drag_index,
            max(int(X_MIN), min(int(X_MAX), int(round(temp)))),
        )
        percent = max(0, min(int(Y_MAX), int(round(percent))))
        self.points[self.drag_index] = [temp, percent]
        self._update_info()
        self.redraw()

    def _on_release(self, _event) -> None:
        if self.drag_index is not None:
            self.drag_index = None
            self._notify()
            self._update_info()
            self.redraw()

    def _on_right(self, event) -> None:
        if not self._in_plot(event.x, event.y):
            return
        hit = self._hit_test(event.x, event.y)
        if hit is None:
            return
        self.selected = hit
        self.delete_selected()

    def _on_delete_key(self, _event) -> None:
        self.delete_selected()

    def _free_temp_near(self, temp: int):
        if self._near_temp(temp, 2) is None and X_MIN <= temp <= X_MAX:
            return temp
        for offset in range(1, int(X_MAX - X_MIN) + 1):
            for candidate in (temp - offset, temp + offset):
                if (
                    X_MIN <= candidate <= X_MAX
                    and self._near_temp(candidate, 2) is None
                ):
                    return candidate
        return None

    def add_point(self) -> None:
        temp = self.current_temp if self.current_temp > 0 else 65
        temp = int(round(max(X_MIN, min(X_MAX, temp))))
        free = self._free_temp_near(temp)
        if free is None:
            self.info_var.set("没有足够空间添加点（点太密）")
            return
        percent = int(round(curve_value(self.points, free)))
        self.points.append([free, percent])
        self.points.sort(key=lambda point: point[0])
        self.selected = self._index_for_temp(free, percent)
        self.drag_index = None
        self._notify()
        self._update_info()
        self.redraw()

    def delete_selected(self) -> None:
        if self.selected is None:
            self.info_var.set("先选中一个点（左键点击）")
            return
        if len(self.points) <= 2:
            self.info_var.set("至少保留 2 个点")
            return
        del self.points[self.selected]
        self.selected = min(self.selected, len(self.points) - 1)
        self.drag_index = None
        self._notify()
        self._update_info()
        self.redraw()

    def reset(self) -> None:
        self.set_points(default_points())

    def get_points(self):
        return [list(point) for point in self.points]

    def set_points(self, points) -> None:
        self.points = [
            [int(round(temp)), int(round(percent))]
            for temp, percent in points
        ]
        self.points.sort(key=lambda point: point[0])
        self.selected = None
        self.drag_index = None
        self._notify()
        self._update_info()
        self.redraw()

    def set_current(self, temp: int, protect: bool) -> None:
        if temp == self.current_temp and protect == self.protect:
            return
        self.current_temp = temp
        self.protect = protect
        self._update_info()
        self.redraw()

    def set_protect(self, protect: bool) -> None:
        if protect != self.protect:
            self.protect = protect
            self.redraw()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self)

    def _update_info(self) -> None:
        parts = []
        if self.selected is not None and self.selected < len(self.points):
            temp, percent = self.points[self.selected]
            parts.append(
                "选中: %d°C → %d%% (0x%02X)"
                % (temp, percent, fc.percent_to_level(percent))
            )
        else:
            parts.append("选中: 无")

        if self.current_temp > 0:
            percent = int(round(curve_value(self.points, self.current_temp)))
            parts.append(
                "当前: %d°C → %d%% (0x%02X)"
                % (self.current_temp, percent, fc.percent_to_level(percent))
            )
        else:
            parts.append("当前: 等待温度")

        parts.append("左键空白加点 / 拖动点 / 右键删点")
        self.info_var.set("    ".join(parts))

    def redraw(self) -> None:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return

        left, top, right, bottom = self._plot_rect()
        self.canvas.delete("all")

        if self.protect:
            self.canvas.create_rectangle(
                self._sx(90),
                top,
                self._sx(100),
                bottom,
                fill="#ffebee",
                outline="",
            )
        else:
            self.canvas.create_rectangle(
                self._sx(90),
                top,
                self._sx(100),
                bottom,
                fill="#f5f5f5",
                outline="",
            )

        for temp in range(int(X_MIN), int(X_MAX) + 1, 10):
            x = self._sx(temp)
            self.canvas.create_line(
                x, top, x, bottom, fill="#e8e8e8"
            )
            self.canvas.create_text(
                x,
                bottom + 16,
                text="%d" % temp,
                fill="#555555",
                font=("Segoe UI", 8),
            )

        for percent in range(0, 101, 10):
            y = self._sy(percent)
            self.canvas.create_line(
                left, y, right, y, fill="#e8e8e8"
            )
            self.canvas.create_text(
                left - 8,
                y,
                text="%d%%" % percent,
                anchor="e",
                fill="#555555",
                font=("Segoe UI", 8),
            )

        self.canvas.create_line(left, top, left, bottom, fill="#888888")
        self.canvas.create_line(left, bottom, right, bottom, fill="#888888")
        self.canvas.create_text(
            left,
            10,
            text="占空比 %",
            anchor="w",
            fill="#444444",
            font=("Segoe UI", 9, "bold"),
        )
        self.canvas.create_text(
            right,
            height - 12,
            text="温度 °C",
            anchor="e",
            fill="#444444",
            font=("Segoe UI", 9, "bold"),
        )

        if self.protect:
            self.canvas.create_text(
                self._sx(90) + 4,
                top + 10,
                text="90°C+ → 100%",
                anchor="w",
                fill="#c62828",
                font=("Segoe UI", 8, "bold"),
            )
        else:
            self.canvas.create_text(
                self._sx(90) + 4,
                top + 10,
                text="90°C+ 保护关闭",
                anchor="w",
                fill="#777777",
                font=("Segoe UI", 8),
            )

        points = self.points
        extended = []
        if points and points[0][0] > X_MIN:
            extended.append([X_MIN, points[0][1]])
        extended.extend(points)
        if points and points[-1][0] < X_MAX:
            extended.append([X_MAX, points[-1][1]])

        if len(extended) >= 2:
            coords = []
            for temp, percent in extended:
                coords.extend([self._sx(temp), self._sy(percent)])
            polygon = [
                self._sx(X_MIN),
                self._sy(0),
                *coords,
                self._sx(X_MAX),
                self._sy(0),
            ]
            self.canvas.create_polygon(
                polygon, fill="#e3f2fd", outline=""
            )
            self.canvas.create_line(
                coords,
                fill="#1976d2",
                width=2.5,
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
            )

        for index, (temp, percent) in enumerate(points):
            x = self._sx(temp)
            y = self._sy(percent)
            radius = 5
            if index == self.selected:
                self.canvas.create_oval(
                    x - radius - 3,
                    y - radius - 3,
                    x + radius + 3,
                    y + radius + 3,
                    outline="#e65100",
                    width=2,
                )
            self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill="#ff9800" if index == self.selected else "#ffffff",
                outline="#1976d2",
                width=2,
            )

        if self.current_temp > 0:
            temp = max(X_MIN, min(X_MAX, float(self.current_temp)))
            x = self._sx(temp)
            self.canvas.create_line(
                x,
                top,
                x,
                bottom,
                fill="#2e7d32",
                dash=(4, 3),
                width=1,
            )
            if x < right - 44:
                self.canvas.create_text(
                    x + 4,
                    top + 4,
                    text="%d°C" % self.current_temp,
                    anchor="nw",
                    fill="#2e7d32",
                    font=("Segoe UI", 8, "bold"),
                )
            else:
                self.canvas.create_text(
                    x - 4,
                    top + 4,
                    text="%d°C" % self.current_temp,
                    anchor="ne",
                    fill="#2e7d32",
                    font=("Segoe UI", 8, "bold"),
                )

            percent = max(
                0, min(100, int(round(curve_value(points, temp))))
            )
            y = self._sy(percent)
            self.canvas.create_oval(
                x - 4, y - 4, x + 4, y + 4, fill="#2e7d32", outline=""
            )

            if self.protect and self.current_temp >= fc.TEMP_LIMIT_C:
                y2 = self._sy(100)
                self.canvas.create_oval(
                    x - 5,
                    y2 - 5,
                    x + 5,
                    y2 + 5,
                    fill="#c62828",
                    outline="",
                )
                if x < right - 78:
                    self.canvas.create_text(
                        x + 8,
                        y2,
                        text="保护 100%",
                        anchor="w",
                        fill="#c62828",
                        font=("Segoe UI", 8, "bold"),
                    )
                else:
                    self.canvas.create_text(
                        x - 8,
                        y2,
                        text="保护 100%",
                        anchor="e",
                        fill="#c62828",
                        font=("Segoe UI", 8, "bold"),
                    )

        if self.selected is not None and self.selected < len(points):
            temp, percent = points[self.selected]
            x = self._sx(temp)
            y = self._sy(percent)
            text = "%d°C  %d%% (0x%02X)" % (
                temp,
                percent,
                fc.percent_to_level(percent),
            )
            above = y - 12 >= top + 10
            ty = y - 12 if above else y + 12
            anchor = "s" if above else "n"
            tx = min(max(x, left + 60), right - 60)
            text_id = self.canvas.create_text(
                tx,
                ty,
                text=text,
                anchor=anchor,
                fill="#333333",
                font=("Segoe UI", 8),
            )
            box = self.canvas.bbox(text_id)
            if box:
                rect_id = self.canvas.create_rectangle(
                    box[0] - 4,
                    box[1] - 2,
                    box[2] + 4,
                    box[3] + 2,
                    fill="#fffde7",
                    outline="#e0c200",
                )
                self.canvas.tag_lower(rect_id, text_id)


class FanGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("TM2411 双风扇曲线控制")
        self.root.geometry("940x760")
        self.root.minsize(820, 680)

        self.lock = threading.Lock()
        self.curves = {
            "a": default_points(),
            "b": default_points(),
            "linked": default_points(),
        }
        self.mode = "separate"
        self.manual = False
        self.protect = True
        self.running = True
        self.was_manual = False
        self.current_temp = 0
        self.status_text = "正在初始化 EC..."
        self.version_text = "?"

        self.port = None
        self.ec = None
        self.map = None
        self.worker = None
        self.editors = {}

        self._build_ui()
        self._init_ec()
        if not self.running:
            return

        self.version_text = self._read_version()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(POLL_MS, self._update_ui)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text="Redmi Book Pro 14 2025 / TM2411 双风扇曲线",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W, pady=(0, 8))

        top = ttk.Frame(outer)
        top.pack(fill=tk.X, pady=(0, 8))

        self.manual_btn = tk.Button(
            top,
            text="启用手动曲线",
            width=18,
            height=2,
            bg="#d9d9d9",
            activebackground="#c9c9c9",
            relief=tk.RAISED,
            command=self._toggle_manual,
        )
        self.manual_btn.pack(side=tk.LEFT)

        self.mode_var = tk.StringVar(
            value="当前编辑：Fan A（独立曲线）"
        )
        ttk.Label(
            top,
            textvariable=self.mode_var,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.protect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top,
            text="90°C 强制 100%",
            variable=self.protect_var,
            command=self._on_protect,
        ).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.editor_a = CurveEditor(
            self.notebook, "a", "Fan A", self._on_curve_changed
        )
        self.editor_b = CurveEditor(
            self.notebook, "b", "Fan B", self._on_curve_changed
        )
        self.editor_l = CurveEditor(
            self.notebook, "linked", "联动", self._on_curve_changed
        )
        self.editors = {
            "a": self.editor_a,
            "b": self.editor_b,
            "linked": self.editor_l,
        }

        self.notebook.add(self.editor_a, text="Fan A（独立）")
        self.notebook.add(self.editor_b, text="Fan B（独立）")
        self.notebook.add(self.editor_l, text="联动（共用）")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.status_var = tk.StringVar(value=self.status_text)
        ttk.Label(
            outer,
            textvariable=self.status_var,
            font=("Consolas", 9),
            justify=tk.LEFT,
            wraplength=880,
        ).pack(fill=tk.X, pady=(8, 4))

        bottom = ttk.Frame(outer)
        bottom.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            bottom, text="恢复 EC 自动", command=self._restore
        ).pack(side=tk.LEFT)
        ttk.Button(
            bottom,
            text="复制 Fan A → 当前页签",
            command=self._copy_a_to_active,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text="退出", command=self.on_close).pack(
            side=tk.RIGHT
        )

    def _init_ec(self) -> None:
        try:
            self.port = fc.WinRing0Port()
            self.ec = fc.Ec(self.port)
            try:
                self.map = fc.ThrottleStopMap()
            except Exception:
                self.map = None
        except Exception as exc:
            self.status_text = "EC 初始化失败：%s" % exc
            self.running = False
            messagebox.showerror(
                "EC 初始化失败",
                "无法访问 EC。\n\n请以管理员身份运行，并确认 "
                "WinRing0x64.sys 与 fan_gui.py 在同一目录。\n\n%s"
                % exc,
            )
            self.root.after(100, self.root.destroy)

    def _read_version(self) -> str:
        try:
            if self.map is not None:
                version = self.map.read_block(fc.ACPI_BASE, 5)
            else:
                version = self.ec.read_block(fc.ACPI_BASE, 5)
            return ".".join("%02X" % byte for byte in version)
        except Exception:
            return "?"

    def _read_temps(self):
        if self.map is not None:
            return list(
                self.map.read_block(fc.REG_TEMPS, fc.TEMP_COUNT)
            )
        return fc.read_temps(
            self.ec, None, 3, fc.TEMP_COUNT - 3
        )

    def _active_key(self) -> str:
        index = self.notebook.index(self.notebook.select())
        return ("a", "b", "linked")[index]

    def _on_tab_changed(self, _event=None) -> None:
        if not self.editors:
            return
        key = self._active_key()
        with self.lock:
            self.mode = "linked" if key == "linked" else "separate"
        if key == "linked":
            self.mode_var.set("当前编辑：联动（两风扇共用）")
        elif key == "a":
            self.mode_var.set("当前编辑：Fan A（独立曲线）")
        else:
            self.mode_var.set("当前编辑：Fan B（独立曲线）")

    def _on_curve_changed(self, editor: CurveEditor) -> None:
        with self.lock:
            self.curves[editor.key] = editor.get_points()

    def _on_protect(self) -> None:
        self.protect = bool(self.protect_var.get())
        for editor in self.editors.values():
            editor.set_protect(self.protect)

    def _toggle_manual(self) -> None:
        self._set_manual(not self.manual)

    def _set_manual(self, state: bool) -> None:
        self.manual = state
        if state:
            self.manual_btn.config(
                text="手动曲线：开（点击关闭）",
                bg="#4CAF50",
                activebackground="#43A047",
                fg="white",
            )
        else:
            self.manual_btn.config(
                text="启用手动曲线",
                bg="#d9d9d9",
                activebackground="#c9c9c9",
                fg="black",
            )

    def _restore(self) -> None:
        self._set_manual(False)

    def _copy_a_to_active(self) -> None:
        key = self._active_key()
        if key == "a":
            return
        self.editors[key].set_points(self.editor_a.get_points())

    def _worker(self) -> None:
        last_status = 0.0
        last_apply = 0.0
        last_temps = 0.0
        last_levels = None
        temps: list[int] = []

        while self.running:
            try:
                manual = self.manual
                now = time.perf_counter()
                temp_interval = 0.4 if self.map is not None else 1.0

                if now - last_temps >= temp_interval or not temps:
                    new_temps = self._read_temps()
                    last_temps = now
                    if new_temps:
                        temps = new_temps

                hottest = fc.max_temp(temps) if temps else 0
                if hottest > 0:
                    self.current_temp = hottest

                if not manual:
                    if self.was_manual:
                        fc.restore_auto(self.ec, settle=0.0)
                        self.was_manual = False
                        last_levels = None

                    if now - last_status >= STATUS_INTERVAL:
                        last_status = now
                        if hottest > 0:
                            with self.lock:
                                mode = self.mode
                                curves = {
                                    key: list(value)
                                    for key, value in self.curves.items()
                                }
                            if mode == "linked":
                                pct_a = pct_b = curve_value(
                                    curves["linked"], hottest
                                )
                            else:
                                pct_a = curve_value(
                                    curves["a"], hottest
                                )
                                pct_b = curve_value(
                                    curves["b"], hottest
                                )
                            self.status_text = (
                                "模式：EC 自动（曲线仅预览）   "
                                "最高温度：%d°C\n"
                                "预览目标：Fan A %d%% (0x%02X)   "
                                "Fan B %d%% (0x%02X)   EC %s"
                                % (
                                    hottest,
                                    int(round(pct_a)),
                                    fc.percent_to_level(pct_a),
                                    int(round(pct_b)),
                                    fc.percent_to_level(pct_b),
                                    self.version_text,
                                )
                            )
                        else:
                            self.status_text = (
                                "模式：EC 自动   等待温度读取...   "
                                "EC %s" % self.version_text
                            )
                    time.sleep(0.25)
                    continue

                with self.lock:
                    mode = self.mode
                    curves = {
                        key: list(value)
                        for key, value in self.curves.items()
                    }
                    protect = self.protect

                temp_failed = hottest <= 0
                if temp_failed:
                    pct_a = pct_b = 100.0
                    protect_active = True
                else:
                    if mode == "linked":
                        pct_a = pct_b = curve_value(
                            curves["linked"], hottest
                        )
                    else:
                        pct_a = curve_value(curves["a"], hottest)
                        pct_b = curve_value(curves["b"], hottest)
                    protect_active = bool(
                        protect and hottest >= fc.TEMP_LIMIT_C
                    )
                    if protect_active:
                        pct_a = pct_b = 100.0

                level_a = fc.percent_to_level(pct_a)
                level_b = fc.percent_to_level(pct_b)

                if (
                    (level_a, level_b) != last_levels
                    or now - last_apply >= 1.5
                ):
                    fc.apply_levels(
                        self.ec, level_a, level_b, manual=True
                    )
                    last_levels = (level_a, level_b)
                    last_apply = now
                    self.was_manual = True

                if now - last_status >= STATUS_INTERVAL:
                    last_status = now
                    duty_a = fc.current_level(self.ec)
                    try:
                        duty_b = self.ec.read(fc.REG_FAN_B_DUTY)
                    except Exception:
                        duty_b = -1

                    if temp_failed:
                        reason = "   温度读取失败，强制 100%"
                    elif protect_active:
                        reason = "   保护 100%"
                    else:
                        reason = ""

                    self.status_text = (
                        "模式：手动曲线（%s）   最高温度：%d°C%s\n"
                        "曲线目标：Fan A %d%% (0x%02X)   "
                        "Fan B %d%% (0x%02X)\n"
                        "实际输出：Fan A %s   Fan B %s   EC %s"
                        % (
                            "联动" if mode == "linked" else "独立",
                            hottest,
                            reason,
                            int(round(pct_a)),
                            fc.percent_to_level(pct_a),
                            int(round(pct_b)),
                            fc.percent_to_level(pct_b),
                            self._duty_text(duty_a),
                            self._duty_text(duty_b),
                            self.version_text,
                        )
                    )

                time.sleep(CONTROL_INTERVAL)
            except Exception as exc:
                self.status_text = "EC 错误：%s" % exc
                time.sleep(0.5)

    def _duty_text(self, duty: int) -> str:
        if duty < 0:
            return "?"
        percent = min(100.0, fc.level_to_percent(duty))
        return "0x%02X (%d%%)" % (duty, int(round(percent)))

    def _update_ui(self) -> None:
        if not self.running:
            return
        self.status_var.set(self.status_text)
        for editor in self.editors.values():
            editor.set_current(self.current_temp, self.protect)
        self.root.after(POLL_MS, self._update_ui)

    def on_close(self) -> None:
        if not self.running:
            return
        self.running = False
        self._set_manual(False)
        try:
            if self.worker is not None:
                self.worker.join(timeout=5.0)
        except Exception:
            pass
        try:
            if self.ec is not None:
                fc.restore_auto(self.ec, settle=0.2)
        except Exception:
            pass
        try:
            if self.map is not None:
                self.map.close()
        except Exception:
            pass
        try:
            if self.port is not None:
                self.port.close()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FanGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
