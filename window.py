"""PicToPDF 图形界面：导入图片并转换为 PDF。"""
from __future__ import annotations

import platform
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from converter import (
    IMAGE_EXTS,
    PAGE_SIZES,
    ConvertSettings,
    convert_images,
)

try:
    from tkinterdnd2 import COPY, DND_FILES, TkinterDnD
    from tkinterdnd2.TkinterDnD import DnDWrapper
except ImportError:
    COPY = DND_FILES = TkinterDnD = None

    class DnDWrapper:  # 未安装 tkinterdnd2 时退化为空基类，应用仍可正常运行
        pass

_IMAGE_FILETYPES = [
    ("图片文件", " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTS))),
    ("所有文件", "*.*"),
]
_PDF_FILETYPES = [("PDF 文件", "*.pdf")]


def _natural_key(path_str: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path_str)]


class App(tk.Tk, DnDWrapper):
    def __init__(self) -> None:
        super().__init__()
        self.title("PicToPDF - 图片转 PDF")
        self.geometry("990x660")
        self.minsize(720, 440)

        self.files: list[str] = []
        self._events: queue.Queue = queue.Queue()
        self._busy = False
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_after_id: str | None = None
        self._preview_path: str | None = None
        self.dnd_enabled = self._setup_dnd()

        self._build_ui()
        self._apply_theme()
        self._refresh_list()
        self.after(100, self._poll_events)

    # ---------------- 拖放 ----------------

    def _setup_dnd(self) -> bool:
        if TkinterDnD is None:
            return False
        # tkinterdnd2 未提供 Intel macOS + Tcl 9 的预编译 tkdnd，
        # 使用项目内置的构建（见 vendor/tkdnd）
        if (
            platform.system() == "Darwin"
            and platform.machine() == "x86_64"
            and int(self.tk.call("info", "tclversion").split(".")[0]) >= 9
        ):
            vendored = Path(__file__).parent / "vendor" / "tkdnd"
            if vendored.is_dir():
                self.tk.call("lappend", "auto_path", str(vendored))
        require = getattr(TkinterDnD, "require", None) or TkinterDnD._require
        try:
            require(self)
        except Exception:  # noqa: BLE001 - 拖放不可用时静默降级为按钮添加
            return False
        return True

    def _register_drop_targets(self) -> None:
        for widget in (self, self.list_frame, self.listbox, self.preview_frame, self.preview_label):
            try:
                widget.drop_target_register(DND_FILES)
            except tk.TclError:
                continue
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropPosition>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop_enter(self, _event) -> str:
        self._set_status("松开以添加图片")
        return COPY

    def _on_drop_leave(self, _event) -> None:
        self._set_status("就绪")

    def _on_drop(self, event) -> str:
        paths = list(self.tk.splitlist(event.data))
        images = [p for p in paths if Path(p).suffix.lower() in IMAGE_EXTS]
        skipped = len(paths) - len(images)
        added = self._merge_paths(images)
        if added:
            note = f"，忽略 {skipped} 个非图片" if skipped else ""
            self._set_status(f"已添加 {added} 张图片{note}")
        elif skipped:
            self._set_status("没有可识别的图片文件", error=True)
        else:
            self._set_status("文件已在列表中")
        return COPY


    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, **pad)
        for text, command in (
            ("添加图片…", self.add_images),
            ("移除选中", self.remove_selected),
            ("清空", self.clear_all),
            ("↑ 上移", lambda: self.move_selection(up=True)),
            ("↓ 下移", lambda: self.move_selection(up=False)),
        ):
            ttk.Button(toolbar, text=text, command=command).pack(side=tk.LEFT, padx=(0, 6))

        # 列表 + 预览：左右分栏，可拖动分隔条
        paned = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, **pad)

        self.list_frame = ttk.Frame(paned)
        paned.add(self.list_frame, weight=3)
        self.listbox = tk.Listbox(
            self.list_frame, selectmode=tk.EXTENDED, exportselection=False, activestyle="none"
        )
        # 经典 Scrollbar 才能在 macOS 上完整吃到背景色；ttk 滚动条会留下白槽
        self.yscroll = tk.Scrollbar(
            self.list_frame, orient=tk.VERTICAL, command=self.listbox.yview
        )
        self.xscroll = tk.Scrollbar(
            self.list_frame, orient=tk.HORIZONTAL, command=self.listbox.xview
        )
        self.listbox.configure(
            yscrollcommand=self._on_list_yview,
            xscrollcommand=self._on_list_xview,
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll.grid(row=1, column=0, sticky="ew")
        self.list_frame.rowconfigure(0, weight=1)
        self.list_frame.columnconfigure(0, weight=1)

        self.preview_frame = ttk.LabelFrame(paned, text="预览")
        paned.add(self.preview_frame, weight=2)
        self.preview_label = ttk.Label(
            self.preview_frame,
            text="点击列表中的图片进行预览",
            anchor=tk.CENTER,
            justify=tk.CENTER,
        )
        self.preview_label.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 2))
        self.preview_info = ttk.Label(self.preview_frame, text="", anchor=tk.CENTER)
        self.preview_info.pack(fill=tk.X, padx=8, pady=(0, 8))

        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self.listbox.bind("<KeyRelease>", self._on_list_select)
        self.preview_label.bind("<Configure>", self._on_preview_resize)

        self.count_label = ttk.Label(self, text="共 0 张图片")
        self.count_label.pack(anchor=tk.W, padx=10)

        settings = ttk.LabelFrame(self, text="转换设置")
        settings.pack(fill=tk.X, padx=10, pady=(6, 4))

        row1 = ttk.Frame(settings)
        row1.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(row1, text="页面大小:").pack(side=tk.LEFT)
        self.page_var = tk.StringVar(value="A4")
        page_combo = ttk.Combobox(
            row1,
            textvariable=self.page_var,
            values=list(PAGE_SIZES),
            state="readonly",
            width=10,
        )
        page_combo.pack(side=tk.LEFT, padx=(4, 20))
        page_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_orientation_state())

        ttk.Label(row1, text="页面方向:").pack(side=tk.LEFT)
        self.orientation_var = tk.StringVar(value="portrait")
        self.orientation_buttons = []
        for value, label in (("portrait", "纵向"), ("landscape", "横向"), ("auto", "自动")):
            btn = ttk.Radiobutton(row1, text=label, variable=self.orientation_var, value=value)
            btn.pack(side=tk.LEFT, padx=(4, 8))
            self.orientation_buttons.append(btn)

        row2 = ttk.Frame(settings)
        row2.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Label(row2, text="边距 (mm):").pack(side=tk.LEFT)
        self.margin_var = tk.StringVar(value="0")
        ttk.Spinbox(
            row2, from_=0, to=100, increment=1, textvariable=self.margin_var, width=6
        ).pack(side=tk.LEFT, padx=(4, 20))
        self.fit_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row2, text="图片适配页面（等比缩放不放大，居中）", variable=self.fit_var
        ).pack(side=tk.LEFT)

        output = ttk.LabelFrame(self, text="输出")
        output.pack(fill=tk.X, padx=10, pady=4)
        out_row = ttk.Frame(output)
        out_row.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(out_row, text="输出文件:").pack(side=tk.LEFT)
        self.out_var = tk.StringVar()
        ttk.Entry(out_row, textvariable=self.out_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )
        ttk.Button(out_row, text="浏览…", command=self.choose_output).pack(side=tk.LEFT)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=10, pady=(4, 10))
        self.convert_btn = ttk.Button(bottom, text="转换为 PDF", command=self.start_convert)
        self.convert_btn.pack(side=tk.LEFT)
        self.status_label = ttk.Label(bottom, text="就绪")
        self.status_label.pack(side=tk.LEFT, padx=12)

        if self.dnd_enabled:
            self._register_drop_targets()

    def _apply_theme(self) -> None:
        """统一各组件背景色，与主窗口/程序框架保持一致。"""
        style = ttk.Style(self)
        bg = str(style.lookup("TFrame", "background") or self.cget("background"))
        fg = str(style.lookup("TLabel", "foreground") or self.cget("foreground"))
        # 列表选中用系统强调色，避免与背景同灰导致不可见
        sel_bg = str(style.lookup("TCombobox", "selectbackground") or "#0a5fff")
        if sel_bg.startswith("system") or sel_bg == bg:
            sel_bg = "#0a5fff"
        sel_fg = "#ffffff"

        self.configure(background=bg)
        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TButton", background=bg)
        style.configure("TCombobox", background=bg, fieldbackground=bg, arrowcolor=fg)
        style.configure("TSpinbox", background=bg, fieldbackground=bg, arrowcolor=fg)
        style.configure("Horizontal.TScrollbar", background=bg, troughcolor=bg)
        style.configure("Vertical.TScrollbar", background=bg, troughcolor=bg)
        # 滚动条与列表框四边同色，消掉右侧/底部白条
        for sb in (self.yscroll, self.xscroll):
            sb.configure(
                background=bg,
                troughcolor=bg,
                activebackground=bg,
                highlightthickness=0,
                borderwidth=0,
                elementborderwidth=0,
                relief=tk.FLAT,
            )

        self.listbox.configure(
            background=bg,
            foreground=fg,
            selectbackground=sel_bg,
            selectforeground=sel_fg,
            highlightbackground=bg,
            highlightcolor=bg,
            highlightthickness=0,
            borderwidth=0,
            relief=tk.FLAT,
        )
        self.status_label.configure(background=bg)
        self.list_frame.configure(style="TFrame")
        self.preview_frame.configure(style="TLabelframe")
        self.preview_label.configure(background=bg, foreground=fg)
        self.preview_info.configure(background=bg, foreground=fg)

    def _on_list_yview(self, first: str, last: str) -> None:
        """滚动条按需显示，避免空内容时仍留白条。"""
        self.yscroll.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.yscroll.grid_remove()
        else:
            self.yscroll.grid()

    def _on_list_xview(self, first: str, last: str) -> None:
        self.xscroll.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.xscroll.grid_remove()
        else:
            self.xscroll.grid()

    # ---------------- 图片预览 ----------------

    def _on_list_select(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            self._clear_preview()
            return
        path = self.files[selection[0]]
        # 短延迟合并快速点选，避免连续解码大图
        if self._preview_after_id is not None:
            self.after_cancel(self._preview_after_id)
        self._preview_after_id = self.after(80, lambda: self._show_preview(path))

    def _on_preview_resize(self, _event=None) -> None:
        if self._preview_path:
            self._show_preview(self._preview_path)

    def _clear_preview(self) -> None:
        self._preview_path = None
        self._preview_photo = None
        self.preview_label.configure(image="", text="点击列表中的图片进行预览")
        self.preview_info.configure(text="")

    def _show_preview(self, path: str) -> None:
        self._preview_after_id = None
        self._preview_path = path
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                width, height = im.size
                # 预览区实际可用尺寸（留出内边距）
                box_w = max(self.preview_label.winfo_width() - 16, 64)
                box_h = max(self.preview_label.winfo_height() - 16, 64)
                im.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
                self._preview_photo = ImageTk.PhotoImage(im)
        except Exception as exc:  # noqa: BLE001 - 预览失败不影响主流程
            self._preview_photo = None
            self.preview_label.configure(image="", text=f"无法预览\n{exc}")
            self.preview_info.configure(text=Path(path).name)
            return

        self.preview_label.configure(image=self._preview_photo, text="")
        self.preview_info.configure(
            text=f"{Path(path).name}　{width}×{height}"
        )

    def _update_orientation_state(self) -> None:
        # 跟随图片时方向由图片本身决定，禁用方向选择
        enabled = self.page_var.get() != "跟随图片"
        state = "normal" if enabled else "disabled"
        for btn in self.orientation_buttons:
            btn.configure(state=state)

    # ---------------- 文件列表操作 ----------------

    def add_images(self) -> None:
        chosen = filedialog.askopenfilenames(filetypes=_IMAGE_FILETYPES)
        if chosen:
            self._merge_paths(chosen)

    def _merge_paths(self, paths) -> int:
        existing = set(self.files)
        added = 0
        for path in sorted(paths, key=_natural_key):
            if path not in existing:
                self.files.append(path)
                existing.add(path)
                added += 1
        self._refresh_list()
        return added

    def remove_selected(self) -> None:
        selected = list(self.listbox.curselection())
        if not selected:
            return
        for index in reversed(selected):
            del self.files[index]
        self._refresh_list()

    def clear_all(self) -> None:
        self.files.clear()
        self._refresh_list()

    def move_selection(self, up: bool) -> None:
        selected = list(self.listbox.curselection())
        if not selected:
            return
        if up and selected[0] == 0:
            return
        if not up and selected[-1] == len(self.files) - 1:
            return
        items = [self.files[i] for i in selected]
        for i in reversed(selected):
            del self.files[i]
        position = selected[0] - 1 if up else selected[0] + 1
        self.files[position:position] = items
        moved = [i - 1 if up else i + 1 for i in selected]
        self._refresh_list(select=moved)

    def _refresh_list(self, select: tuple | list = ()) -> None:
        self.listbox.delete(0, tk.END)
        for path in self.files:
            self.listbox.insert(tk.END, path)
        for index in select:
            self.listbox.selection_set(index)
        if self.files:
            self.listbox.see(select[0] if select else 0)
            text = f"共 {len(self.files)} 张图片"
        else:
            hint = "（可将图片直接拖放到窗口添加）" if self.dnd_enabled else ""
            text = f"共 0 张图片{hint}"
            self._clear_preview()
        self.count_label.configure(text=text)
        self._on_list_select()

    # ---------------- 输出路径 ----------------

    def _default_output_proposal(self) -> dict:
        if self.files:
            first = Path(self.files[0])
            return {"initialdir": str(first.parent), "initialfile": first.stem + ".pdf"}
        return {"initialdir": str(Path.cwd()), "initialfile": "output.pdf"}

    def choose_output(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=_PDF_FILETYPES, **self._default_output_proposal()
        )
        if path:
            self.out_var.set(path)

    # ---------------- 转换 ----------------

    def _read_settings(self) -> ConvertSettings | None:
        try:
            margin = float(self.margin_var.get())
        except ValueError:
            messagebox.showwarning("边距无效", "请输入数字作为边距（毫米）")
            return None
        if not 0 <= margin <= 100:
            messagebox.showwarning("边距无效", "边距需在 0 ~ 100 毫米之间")
            return None
        return ConvertSettings(
            page_size=self.page_var.get(),
            orientation=self.orientation_var.get(),
            margin_mm=margin,
            fit_to_page=self.fit_var.get(),
        )

    def start_convert(self) -> None:
        if self._busy:
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加要转换的图片")
            return
        output = self.out_var.get().strip()
        if not output:
            path = filedialog.asksaveasfilename(
                defaultextension=".pdf", filetypes=_PDF_FILETYPES, **self._default_output_proposal()
            )
            if not path:
                return
            output = path
            self.out_var.set(path)
        settings = self._read_settings()
        if settings is None:
            return

        self._busy = True
        self.convert_btn.configure(state="disabled")
        self._set_status("转换中…")
        threading.Thread(
            target=self._convert_worker,
            args=(list(self.files), output, settings),
            daemon=True,
        ).start()

    def _convert_worker(
        self, files: list[str], output: str, settings: ConvertSettings
    ) -> None:
        try:
            result = convert_images(
                files,
                output,
                settings,
                progress=lambda cur, total: self._events.put(
                    ("progress", f"转换中… ({cur}/{total}) {Path(files[cur - 1]).name}")
                ),
            )
            self._events.put(("done", str(result)))
        except Exception as exc:  # noqa: BLE001 - 转换失败统一提示
            self._events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._set_status(payload)
                elif kind == "done":
                    self._busy = False
                    self.convert_btn.configure(state="normal")
                    size_kb = Path(payload).stat().st_size / 1024
                    self._set_status(
                        f"✓ 已生成：{Path(payload).name}"
                        f"（{len(self.files)} 页, {size_kb:.1f} KB）"
                    )
                elif kind == "error":
                    self._busy = False
                    self.convert_btn.configure(state="normal")
                    self._set_status("✗ 转换失败", error=True)
                    messagebox.showerror("转换失败", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.configure(
            text=text,
            foreground="#c0392b" if error else (ttk.Style(self).lookup("TLabel", "foreground") or "#333"),
        )


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
