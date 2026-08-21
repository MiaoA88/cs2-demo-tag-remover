# -*- coding: utf-8 -*-
"""Simple GUI for removing name tags from CS2 demo files.

Run:  python gui.py        (or drag a .dem file onto gui.py)
"""
from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tag_strip import DEFAULT_TAG, strip_file

APP_TITLE = "CS2 Demo 名称标签清除工具"
DEFAULT_TAG_TEXT = DEFAULT_TAG.decode("ascii")


def default_output(path: str) -> str:
    base, ext = os.path.splitext(path)
    return base + "_cleaned" + ext


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("640x330")
        root.resizable(False, False)

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text="删除 CS2 demo 文件中的名称标签（皮肤等其他数据保持不变）。",
            wraplength=560,
        ).pack(anchor="w", **pad)

        row1 = ttk.Frame(frm)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="输入 demo：", width=12).pack(side="left")
        self.in_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.in_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="选择…", command=self.pick_input).pack(side="left", padx=6)

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="输出文件：", width=12).pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="选择…", command=self.pick_output).pack(side="left", padx=6)

        row3 = ttk.Frame(frm)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="标签文本：", width=12).pack(side="left")
        self.tag_var = tk.StringVar(value=DEFAULT_TAG_TEXT)
        ttk.Entry(row3, textvariable=self.tag_var).pack(side="left", fill="x", expand=True)
        ttk.Label(row3, text="（多个用逗号分隔）").pack(side="left", padx=6)

        self.deep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm, text="深度扫描所有帧（更彻底，较慢）", variable=self.deep_var
        ).pack(anchor="w", **pad)

        self.bar = ttk.Progressbar(frm, mode="determinate")
        self.bar.pack(fill="x", **pad)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status).pack(anchor="w", **pad)

        self.btn = ttk.Button(frm, text="开始清除", command=self.start)
        self.btn.pack(anchor="e", **pad)

    def pick_input(self):
        path = filedialog.askopenfilename(
            title="选择 CS2 demo 文件", filetypes=[("Demo 文件", "*.dem"), ("所有文件", "*.*")]
        )
        if path:
            self.in_var.set(path)
            self.out_var.set(default_output(path))

    def pick_output(self):
        path = filedialog.asksaveasfilename(
            title="保存为", defaultextension=".dem", filetypes=[("Demo 文件", "*.dem")]
        )
        if path:
            self.out_var.set(path)

    def _tags(self):
        raw = self.tag_var.get().replace("，", ",")
        tags = [p.strip().encode("utf-8") for p in raw.split(",")]
        tags = [t for t in tags if t]
        return tags or [DEFAULT_TAG]

    def start(self):
        src = self.in_var.get().strip()
        dst = self.out_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror(APP_TITLE, "请选择有效的输入 demo 文件")
            return
        if not dst:
            dst = default_output(src)
            self.out_var.set(dst)
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror(APP_TITLE, "输入和输出不能是同一个文件")
            return
        self.btn.config(state="disabled")
        self.bar["value"] = 0
        threading.Thread(
            target=self._work, args=(src, dst, self._tags(), self.deep_var.get()), daemon=True
        ).start()

    def _work(self, src, dst, tags, deep):
        def progress(done, total):
            self.root.after(0, lambda: self.bar.configure(maximum=total, value=done))
            self.root.after(0, lambda: self.status.set(f"扫描中… {done}/{total} 帧"))

        def done_ok(n, removed, delta):
            self.bar["value"] = self.bar["maximum"]
            self.btn.config(state="normal")
            self.status.set(f"完成：删除 {removed} 个标签（{n} 帧，大小变化 {delta:+d} 字节）")
            messagebox.showinfo(
                APP_TITLE,
                f"清除完成！\n\n删除标签：{removed} 个（{n} 帧）\n输出文件：{dst}",
            )

        def done_err(e):
            self.btn.config(state="normal")
            self.status.set("失败")
            messagebox.showerror(APP_TITLE, f"处理失败：\n{e}")

        try:
            n, removed, delta = strip_file(src, dst, tags=tags, deep=deep, progress=progress)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: done_err(exc))
            return
        self.root.after(0, lambda: done_ok(n, removed, delta))


def main(argv=None):
    argv = argv or []
    root = tk.Tk()
    app = App(root)
    # allow drag-and-drop: if a .dem path was passed as argument, prefill it
    if argv and os.path.isfile(argv[0]):
        app.in_var.set(os.path.abspath(argv[0]))
        app.out_var.set(default_output(argv[0]))
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
