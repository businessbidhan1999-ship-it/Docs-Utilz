"""Tkinter interface for local PDF and image processing."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cv2
    from .engine import fitz  # noqa: F401  (checked here for a friendly error)
    import numpy as np
    from PIL import Image, ImageTk
except ImportError as exc:  # pragma: no cover
    print("Missing dependency:", exc)
    print("Install everything with:  pip install -r requirements.txt")
    sys.exit(1)

from . import engine
from . import tools
from . import devices
from . import ui_theme
from .engine import Settings

APP_TITLE = "Docs-Utilz"

# Presets share their detailed settings with the advanced controls.
PRESETS: dict[str, dict] = {
    "complete": {
        "label": "Remove it completely   (recommended)",
        "help": "Rebuilds the paper where the mark was, so nothing faint is "
                "left behind and the patch matches the page around it. Text "
                "and pictures underneath are kept. Saved pages become images.",
        "mode": "raster", "fill": "clean", "protect_dark": True,
        "threshold": 0.58,
    },
    "wipe": {
        "label": "Wipe the whole area blank",
        "help": "The same, except nothing inside the mark is spared — reach "
                "for this when a solid dark logo is the thing that must go. "
                "Anything overlapping the mark goes with it.",
        "mode": "raster", "fill": "clean", "protect_dark": False,
        "threshold": 0.58,
    },
    "selectable": {
        "label": "Keep the text selectable",
        "help": "Deletes the watermark's own objects from the PDF instead of "
                "repainting the page, so the document stays searchable. Only "
                "possible when the mark is a separate object — it will not "
                "touch a mark that is baked into a photographed scan.",
        "mode": "smart", "fill": "clean", "protect_dark": True,
        "threshold": 0.62,
    },
    "custom": {
        "label": "Custom",
        "help": "Whatever is set in Advanced options below.",
    },
}


def config_dir() -> Path:
    """Where to keep the remembered watermark and settings.

    Beside the executable when that is writable — so the whole thing stays
    portable on a memory stick, watermark included, and works the same on every
    machine it is plugged into. Falls back to the usual per-user location when
    the program sits somewhere read-only, such as /opt or Program Files.
    """
    here = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
    probe = here / ".write_test"
    try:
        probe.touch()
        probe.unlink()
        return here
    except OSError:
        pass
    base = os.environ.get("APPDATA") or (Path.home() / ".config")
    path = Path(base) / "Docs-Utilz"
    path.mkdir(parents=True, exist_ok=True)
    return path


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        # Keep the window usable on 1366×768 displays.
        available_width = max(800, self.winfo_screenwidth() - 64)
        available_height = max(540, self.winfo_screenheight() - 96)
        self.geometry(f"{min(1340, available_width)}x{min(860, available_height)}")
        self.minsize(min(1080, available_width), min(680, available_height))

        self.theme_name = tk.StringVar(value="Paper")
        self.colors = ui_theme.apply(self, "Paper")
        self.pdf_path = tk.StringVar()
        self.wm_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.preset = tk.StringVar(value="complete")
        self.remember_wm = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value="raster")
        self.dpi = tk.IntVar(value=200)
        self.threshold = tk.DoubleVar(value=0.58)
        self.fill = tk.StringVar(value="clean")
        self.fill_label = tk.StringVar(value=engine.FILL_LABELS["clean"])
        self.zoom = tk.StringVar(value="Fit width")
        self.protect_dark = tk.BooleanVar(value=True)
        self.scan_rotations = tk.BooleanVar(value=False)
        self.page_range = tk.StringVar(value="")
        self.preview_page = tk.IntVar(value=1)
        self.t_images = tk.BooleanVar(value=True)
        self.t_graphics = tk.BooleanVar(value=True)
        self.t_text = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready.")

        self.target_kb = tk.IntVar(value=1000)
        self.size_mode = tk.StringVar(value="Reduce to at most")
        self.print_path = tk.StringVar()
        self.printer_name = tk.StringVar()
        self.print_copies = tk.IntVar(value=1)
        self.scanner_name = tk.StringVar()
        self.scan_dpi = tk.IntVar(value=300)
        self.scan_color = tk.StringVar(value="Color")
        self.scan_source = tk.StringVar(value="Driver default")
        self.scan_driver_settings = tk.BooleanVar(value=True)
        self.print_paper = tk.StringVar(value="A4")
        self.print_orientation = tk.StringVar(value="Portrait")
        self.print_scaling = tk.StringVar(value="Fit")
        self.print_margin = tk.DoubleVar(value=0)
        self.print_duplex = tk.StringVar(value="Off")
        self.print_review = tk.BooleanVar(value=True)
        self._device_states = {}
        self.cmp_outdir = tk.StringVar(value="")
        self.cmp_keep_text = tk.BooleanVar(value=True)
        self.cmp_allow_jpeg = tk.BooleanVar(value=True)

        self.p2i_pdf = tk.StringVar(value="")
        self.p2i_dir = tk.StringVar(value="")
        self.p2i_dpi = tk.IntVar(value=200)
        self.p2i_fmt = tk.StringVar(value="png")
        self.p2i_pages = tk.StringVar(value="")
        self.i2p_out = tk.StringVar(value="")
        self.i2p_size = tk.StringVar(value="image")
        self.i2p_dpi = tk.IntVar(value=200)
        self.i2p_quality = tk.IntVar(value=92)

        self._msgq: queue.Queue = queue.Queue()
        self._busy = False
        self._busy_buttons: list[ttk.Button] = []
        self._thumbs: dict[str, ImageTk.PhotoImage] = {}
        # Suppress setting traces while applying a preset.
        self._applying_preset = False
        self._total_pages = 1

        self._scroll_regions = []
        self._build_ui()
        self._load_prefs()
        self._apply_theme()
        self._watch_custom()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)
        self.after(500, self._refresh_devices)

    # Persist settings between sessions.

    _PREF_KEYS = ("theme_name", "preset", "remember_wm", "mode", "dpi", "threshold", "fill",
                  "protect_dark", "scan_rotations", "t_images", "t_graphics",
                  "t_text", "zoom",
                  "target_kb", "size_mode", "cmp_keep_text", "cmp_allow_jpeg", "cmp_outdir",
                  "p2i_dpi", "p2i_fmt", "i2p_size", "i2p_dpi", "i2p_quality",
                  "printer_name", "scanner_name", "scan_dpi", "scan_color", "scan_source",
                  "scan_driver_settings", "print_paper", "print_orientation", "print_scaling",
                  "print_margin", "print_duplex", "print_review")

    def _prefs_file(self) -> Path:
        return config_dir() / "settings.json"

    def _load_prefs(self) -> None:
        try:
            data = json.loads(self._prefs_file().read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._applying_preset = True   # restoring is not the user editing
        for key in self._PREF_KEYS:
            if key in data:
                try:
                    getattr(self, key).set(data[key])
                except (tk.TclError, AttributeError):
                    pass
        self._applying_preset = False
        # Restore the previous watermark only when requested.
        saved = data.get("watermark")
        if self.remember_wm.get() and saved and Path(saved).is_file():
            self._set_watermark(saved, quiet=True)
            self._log(f"Reusing the watermark from last time: "
                      f"{Path(saved).name}")
        self._apply_preset()
        self._sync_mode()
        self._sync_labels()
        self._sync_zoom()

    def _save_prefs(self) -> None:
        data = {k: getattr(self, k).get() for k in self._PREF_KEYS}
        if self.wm_path.get() and self.remember_wm.get():
            data["watermark"] = self.wm_path.get()
        try:
            self._prefs_file().write_text(json.dumps(data, indent=2), "utf-8")
        except OSError:
            pass

    def _on_close(self) -> None:
        self._save_prefs()
        self.destroy()

    WORKSPACES = (
        ("Remove watermark", "Clean a document", "Choose a file and a watermark sample, then preview the result."),
        ("File size", "Set the right file size", "Reduce documents to a limit, or increase them to an exact size."),
        ("Convert", "Move between formats", "Export PDF pages as pictures or combine pictures into one PDF."),
        ("Scan & Print", "From paper to file, and back", "Scan a page or print a saved document with your connected devices."),
    )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        masthead = ttk.Frame(outer, padding=(24, 14))
        masthead.grid(row=0, column=0, columnspan=2, sticky="ew")
        masthead.columnconfigure(1, weight=1)
        self.brand_mark = tk.Canvas(masthead, width=32, height=36,
                                     highlightthickness=0, takefocus=False)
        self.brand_mark.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(masthead, text=APP_TITLE, style="Brand.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(masthead, text="Your everyday document desk", style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))
        ttk.Label(masthead, text="Theme", style="Muted.TLabel").grid(row=0, column=2, rowspan=2, padx=(12, 8))
        picker = ttk.Combobox(masthead, textvariable=self.theme_name, values=("Paper", "Ink"),
                              width=8, state="readonly")
        picker.grid(row=0, column=3, rowspan=2)
        picker.bind("<<ComboboxSelected>>", lambda _e: self._apply_theme())

        self.rail = tk.Frame(outer, width=196, padx=12, pady=24)
        self.rail.grid(row=1, column=0, rowspan=2, sticky="ns")
        self.rail.grid_propagate(False)
        self.rail.columnconfigure(0, weight=1)
        self.rail.rowconfigure(5, weight=1)
        self.nav_buttons = []
        for index, (label, _, _) in enumerate(self.WORKSPACES):
            button = tk.Button(self.rail, text=label, anchor="w", padx=14, pady=14,
                               relief="flat", borderwidth=0, highlightthickness=2,
                               command=lambda i=index: self.tabs.select(i))
            button.grid(row=index, column=0, sticky="ew", pady=(0, 8))
            self.nav_buttons.append(button)
        self.rail_note = tk.Label(self.rail, text="PDF · JPEG · PNG\n\nOriginals stay intact.\nProcessing stays local.",
                                  anchor="w", justify="left", padx=14, pady=16, wraplength=148)
        self.rail_note.grid(row=6, column=0, sticky="sw")

        work = ttk.Frame(outer, padding=(22, 16, 22, 12))
        work.grid(row=1, column=1, sticky="nsew")
        work.columnconfigure(0, weight=1)
        work.rowconfigure(2, weight=1)
        self.workspace_title = ttk.Label(work, style="Title.TLabel")
        self.workspace_title.grid(row=0, column=0, sticky="w")
        self.workspace_description = ttk.Label(work, style="Muted.TLabel")
        self.workspace_description.grid(row=1, column=0, sticky="w", pady=(6, 20))
        self.tabs = ttk.Notebook(work, style="Workspace.TNotebook", takefocus=False)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        frames = [ttk.Frame(self.tabs) for _ in self.WORKSPACES]
        for frame, (label, _, _) in zip(frames, self.WORKSPACES):
            self.tabs.add(frame, text=label)
        self.tabs.bind("<<NotebookTabChanged>>", self._sync_navigation)
        self._build_statusbar(outer)
        self._build_watermark_tab(frames[0])
        self._build_compress_tab(self._scrollable_workspace(frames[1]))
        self._build_convert_tab(self._scrollable_workspace(frames[2]))
        self._build_device_tab(self._scrollable_workspace(frames[3]))
        self._apply_preset()
        self._sync_mode()
        self._sync_labels()
        self._sync_navigation()
        self.bind("<FocusIn>", self._reveal_focus)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(event, self._scroll_workspace)
        for index in range(4):
            self.bind(f"<Control-Key-{index + 1}>", lambda _e, i=index: self.tabs.select(i))

    def _sync_navigation(self, _event=None) -> None:
        index = self.tabs.index(self.tabs.select())
        _, title, description = self.WORKSPACES[index]
        self.workspace_title.configure(text=title)
        self.workspace_description.configure(text=description)
        c = self.colors
        for i, button in enumerate(self.nav_buttons):
            selected = i == index
            button.configure(background=c['rail_hover'] if selected else c['rail'],
                             foreground=c['rail_text'] if selected else c['rail_muted'],
                             activebackground=c['rail_hover'], activeforeground=c['rail_text'],
                             highlightbackground=c['rail'], highlightcolor=c['accent'],
                             font=(self.ui_font_family, 10, "bold") if selected else "TkDefaultFont")

    def _apply_theme(self) -> None:
        if self.theme_name.get() not in ui_theme.PALETTES:
            self.theme_name.set("Paper")
        self.colors = ui_theme.apply(self, self.theme_name.get())
        c = self.colors
        self.rail.configure(background=c['rail'])
        self.rail_note.configure(background=c['rail'], foreground=c['rail_muted'])
        self.brand_mark.configure(background=c['background'])
        self.brand_mark.delete("all")
        self.brand_mark.create_polygon(5, 3, 21, 3, 29, 11, 29, 32, 5, 32,
                                       fill=c['accent'], outline="")
        ink = '#FFFFFF' if self.theme_name.get() == 'Paper' else '#172B1D'
        self.brand_mark.create_line(11, 15, 23, 15, fill=ink, width=2)
        self.brand_mark.create_line(11, 21, 23, 21, fill=ink, width=2)
        self.brand_mark.create_line(11, 27, 19, 27, fill=ink, width=2)
        def recolor(widget):
            if isinstance(widget, ttk.Label):
                current = str(widget.cget('foreground'))
                if current in {'#666', '#595959', '#566158', '#BBC9BE'}:
                    widget.configure(foreground=c['muted'])
                elif current in {'#333', '#202923', '#EEF2EB'}:
                    widget.configure(foreground=c['text'])
            elif isinstance(widget, (tk.Text, tk.Listbox)):
                widget.configure(background=c['surface'], foreground=c['text'],
                                 selectbackground=c['selection'], selectforeground=c['selected_text'],
                                 highlightbackground=c['border'], highlightcolor=c['accent'],
                                 borderwidth=0, highlightthickness=1)
                if isinstance(widget, tk.Text):
                    widget.configure(insertbackground=c['text'])
            elif isinstance(widget, tk.Canvas) and widget is not self.brand_mark:
                if any(widget is canvas for canvas, _ in self._scroll_regions):
                    widget.configure(background=c['background'])
                else:
                    widget.configure(background=c['desk'], highlightbackground=c['border'])
            for child in widget.winfo_children():
                recolor(child)
        recolor(self)
        self.log.tag_configure("fail", foreground=c['error'])
        self.log.tag_configure("note", foreground=c['muted'])
        for view in (self.view_before, self.view_after):
            view.empty_ink = c['muted']
            view._draw()
        self._file_checks = {}
        for checked in (False, True):
            image = tk.PhotoImage(width=22, height=22, master=self)
            image.put(c['surface'], to=(0, 0, 22, 22))
            image.put(c['accent'] if checked else c['muted'], to=(3, 3, 19, 19))
            image.put(c['accent'] if checked else c['surface'], to=(4, 4, 18, 18))
            if checked:
                for x, y in ((7, 11), (8, 12), (9, 13), (10, 14), (11, 13),
                             (12, 12), (13, 11), (14, 10), (15, 9), (16, 8)):
                    image.put(ink, to=(x, y, x + 2, y + 2))
            self._file_checks[checked] = image
        for iid, (_, checked) in self.cmp_files.items():
            self.cmp_list.item(iid, image=self._file_checks[checked])
        self._sync_navigation()
        for kind, (state, text) in tuple(self._device_states.items()):
            self._device_status(kind, state, text)

    def _build_statusbar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, padding=(22, 8, 22, 12))
        bar.grid(row=2, column=1, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self.status, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.activity_button = ttk.Button(bar, text="Show activity", command=self._toggle_activity)
        self.activity_button.grid(row=0, column=1, padx=(12, 0))
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.activity_frame = ttk.Frame(bar, padding=(0, 10, 0, 0))
        self.activity_frame.columnconfigure(0, weight=1)
        self.activity_shown = False
        self.log = tk.Text(self.activity_frame, height=4, wrap="word", relief="flat", state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")
        logbar = ttk.Scrollbar(self.activity_frame, orient="vertical", command=self.log.yview)
        logbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=logbar.set)
        row = ttk.Frame(self.activity_frame)
        row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(row, text="Copy activity", command=self._copy_log).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Clear", command=self._clear_log).pack(side="left")

    def _toggle_activity(self) -> None:
        self.activity_shown = not self.activity_shown
        self.activity_button.configure(text="Hide activity" if self.activity_shown else "Show activity")
        if self.activity_shown:
            self.activity_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        else:
            self.activity_frame.grid_remove()

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end").strip())
        self.status.set("Log copied to the clipboard.")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _build_watermark_tab(self, root: ttk.Frame) -> None:
        root.columnconfigure(0, weight=0, minsize=350)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # Keep action buttons visible while settings scroll.
        col = ttk.Frame(root)
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        col.rowconfigure(0, weight=1)
        col.columnconfigure(0, weight=1)

        left = self._scrollable_column(col)
        right = ttk.Frame(root)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_wm_input(left)
        self._build_wm_sample(left)
        self._build_wm_method(left)
        self._build_wm_advanced(left)
        self._build_wm_run(col)
        self._build_wm_preview(right)

        self._log("Pick a file and a cropped picture of the watermark, then "
                  "hit Preview.")

    def _build_wm_input(self, left: ttk.Frame) -> None:
        f = ttk.LabelFrame(left, text="  File to clean  ", padding=10)
        f.pack(fill="x")
        ttk.Entry(f, textvariable=self.pdf_path).pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Choose file…",
                   command=self._pick_pdf).pack(side="left")
        self.pdf_info = ttk.Label(row, text="no file", foreground="#666")
        self.pdf_info.pack(side="left", padx=8)
        ttk.Label(f, text="A PDF, or a JPEG or PNG picture.",
                  foreground="#666").pack(anchor="w", pady=(6, 0))

    def _build_wm_sample(self, left: ttk.Frame) -> None:
        f = ttk.LabelFrame(left, text="  Watermark sample  ",
                           padding=10)
        f.pack(fill="x", pady=10)
        ttk.Entry(f, textvariable=self.wm_path).pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Snip from the page…",
                   command=self._snip_from_pdf).pack(side="left")
        ttk.Button(row, text="Open image…",
                   command=self._pick_wm).pack(side="left", padx=6)
        ttk.Button(row, text="Clear",
                   command=self._clear_watermark).pack(side="left")
        self.wm_canvas = tk.Canvas(f, height=90, bg="#f2f2f2",
                                   highlightthickness=1,
                                   highlightbackground="#ccc")
        self.wm_canvas.pack(fill="x", pady=(8, 0))
        ttk.Label(f, text="Crop tightly around the mark, on a clean part of "
                          "the page.", foreground="#666",
                  wraplength=295).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(f, text="Keep this watermark for next time",
                        variable=self.remember_wm).pack(anchor="w", pady=(4, 0))

    def _build_wm_method(self, left: ttk.Frame) -> None:
        f = ttk.LabelFrame(left, text="  Removal method  ", padding=10)
        f.pack(fill="x")
        for key in ("complete", "wipe", "selectable", "custom"):
            ttk.Radiobutton(f, text=PRESETS[key]["label"], value=key,
                            variable=self.preset,
                            command=self._apply_preset).pack(anchor="w")
        self.preset_help = ttk.Label(f, foreground="#666", wraplength=295,
                                     justify="left")
        self.preset_help.pack(anchor="w", pady=(6, 0))

    def _build_wm_advanced(self, left: ttk.Frame) -> None:
        """The old settings panel, folded away.

        Nothing here was wrong, there was just far too much of it in the face of
        somebody who wants a watermark gone. It stays reachable, one click down,
        for the document that needs it.
        """
        holder = ttk.Frame(left)
        holder.pack(fill="x", pady=(10, 0))
        self.adv_shown = tk.BooleanVar(value=False)
        self.adv_button = ttk.Button(holder, text="Advanced options",
                                     style="TButton",
                                     command=self._toggle_advanced)
        self.adv_button.pack(fill="x")
        self.adv_frame = ttk.Frame(holder, padding=(10, 8))
        f = self.adv_frame
        wrap = 295   # the column is narrow; every explanation must wrap inside it

        def heading(text: str) -> None:
            ttk.Label(f, text=text).pack(anchor="w", pady=(8, 2))

        def note(parent: ttk.Frame, text: str) -> ttk.Label:
            lbl = ttk.Label(parent, text=text, foreground="#666",
                            wraplength=wrap, justify="left")
            lbl.pack(anchor="w")
            return lbl

        heading("Output style")
        ttk.Radiobutton(f, text="Repaint the page (most reliable)",
                        variable=self.mode, value="raster",
                        command=self._sync_mode).pack(anchor="w")
        ttk.Radiobutton(f, text="Edit the PDF's objects (keeps text)",
                        variable=self.mode, value="smart",
                        command=self._sync_mode).pack(anchor="w")

        self.smart_box = ttk.Frame(f)
        self.smart_box.pack(fill="x", pady=(6, 0))
        note(self.smart_box, "Watermark is made of:")
        ttk.Checkbutton(self.smart_box, text="image / logo",
                        variable=self.t_images).pack(anchor="w")
        ttk.Checkbutton(self.smart_box, text="lines / shapes",
                        variable=self.t_graphics).pack(anchor="w")
        ttk.Checkbutton(self.smart_box, text="text — also deletes body text "
                        "under it", variable=self.t_text).pack(anchor="w")

        self.raster_box = ttk.Frame(f)
        self.raster_box.pack(fill="x", pady=(8, 0))
        ttk.Label(self.raster_box, text="Erase by").pack(anchor="w",
                                                         pady=(0, 2))
        # Show the fill description below the selected mode.
        self.fill_box = ttk.Combobox(
            self.raster_box, textvariable=self.fill_label, state="readonly",
            values=[label for _, label, _ in engine.FILL_MODES])
        self.fill_box.pack(fill="x")
        self.fill_box.bind("<<ComboboxSelected>>", self._on_fill_chosen)
        self.fill_help = ttk.Label(self.raster_box, foreground="#666",
                                   wraplength=wrap, justify="left")
        self.fill_help.pack(anchor="w", pady=(3, 5))
        ttk.Checkbutton(self.raster_box,
                        text="Keep dark text and logos under the mark",
                        variable=self.protect_dark).pack(anchor="w")

        heading("Sensitivity")
        sens = ttk.Frame(f)
        sens.pack(fill="x")
        ttk.Scale(sens, from_=0.35, to=0.95, variable=self.threshold,
                  command=lambda _e: self._sync_labels()).pack(
                      side="left", fill="x", expand=True)
        self.thr_lbl = ttk.Label(sens, width=5)
        self.thr_lbl.pack(side="left")
        note(f, "lower finds more marks, and more false ones")

        heading("Quality (DPI)")
        ttk.Combobox(f, textvariable=self.dpi, width=8, state="readonly",
                     values=[120, 150, 200, 250, 300]).pack(anchor="w")
        ttk.Checkbutton(f, text="Also scan rotated copies (slower)",
                        variable=self.scan_rotations).pack(anchor="w",
                                                           pady=(6, 0))

        heading("Pages")
        ttk.Entry(f, textvariable=self.page_range, width=16).pack(anchor="w")
        note(f, "blank = all · e.g. 1-3,7")

    def _build_wm_run(self, col: ttk.Frame) -> None:
        f = ttk.Frame(col, padding=(0, 10))
        f.grid(row=1, column=0, sticky="ew")
        self.btn_preview = ttk.Button(f, text="Preview this page",
                                      command=self._on_preview)
        self.btn_preview.pack(fill="x")
        self.btn_run = ttk.Button(f, text="Remove watermark & save", style="Primary.TButton",
                                  command=self._on_run)
        self.btn_run.pack(fill="x", pady=(6, 0))
        self._busy_buttons += [self.btn_preview, self.btn_run]

    def _build_wm_preview(self, right: ttk.Frame) -> None:
        bar = ttk.Frame(right)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(3, weight=1)

        self.btn_prev_page = ttk.Button(bar, text="Previous", width=8,
                                        command=lambda: self._step_page(-1))
        self.btn_prev_page.grid(row=0, column=0)
        ttk.Spinbox(bar, from_=1, to=99999, width=6,
                    textvariable=self.preview_page,
                    command=self._on_page_spun).grid(row=0, column=1, padx=4)
        self.btn_next_page = ttk.Button(bar, text="Next", width=6,
                                        command=lambda: self._step_page(1))
        self.btn_next_page.grid(row=0, column=2)
        self.page_lbl = ttk.Label(bar, text="of 1", foreground="#666")
        self.page_lbl.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(bar, text="Zoom").grid(row=0, column=4, sticky="e")
        box = ttk.Combobox(bar, textvariable=self.zoom, width=10,
                           state="readonly", values=list(ImageView.ZOOMS))
        box.grid(row=0, column=5, padx=(4, 0))
        box.bind("<<ComboboxSelected>>", lambda _e: self._sync_zoom())

        nb = ttk.Notebook(right)
        nb.grid(row=1, column=0, sticky="nsew")
        self.view_before = ImageView(
            nb, "Hit “Preview this page” to see what was found.\n\n"
                "Red boxes mark every watermark the program spotted.")
        self.view_after = ImageView(
            nb, "The same page with the watermark taken out.\n\n"
                "Scroll to move around, or drag the page with the mouse.")
        nb.add(self.view_before, text="  Detected  ")
        nb.add(self.view_after, text="  Cleaned  ")
        self._busy_buttons += [self.btn_prev_page, self.btn_next_page]

    def _reveal(self, widget: tk.Misc) -> None:
        """Scroll the settings column so `widget` is at the top of the pane."""
        canvas, inner = self._left_canvas, self._left_inner
        canvas.update_idletasks()
        box = canvas.bbox("all")
        if not box or box[3] <= 0:
            return
        offset = widget.winfo_rooty() - inner.winfo_rooty()
        canvas.yview_moveto(max(0.0, min(1.0, offset / box[3])))

    def _toggle_advanced(self) -> None:
        shown = not self.adv_shown.get()
        self.adv_shown.set(shown)
        self.adv_button.config(text=("Hide advanced options" if shown
                                     else "Advanced options"))
        if shown:
            self.adv_frame.pack(fill="x")
            # Opening a panel that lands below the fold looks like the click did
            # nothing at all, so the column is scrolled to it.
            self.after_idle(lambda: self._reveal(self.adv_button))
        else:
            self.adv_frame.pack_forget()

    def _apply_preset(self) -> None:
        """Write the chosen preset into the detailed settings."""
        spec = PRESETS[self.preset.get()]
        self.preset_help.config(text=spec["help"])
        if "mode" not in spec:            # Custom — leave everything be
            return
        self._applying_preset = True
        try:
            self.mode.set(spec["mode"])
            self.fill.set(spec["fill"])
            self.fill_label.set(engine.FILL_LABELS[spec["fill"]])
            self.protect_dark.set(spec["protect_dark"])
            self.threshold.set(spec["threshold"])
        finally:
            self._applying_preset = False
        self._sync_mode()
        self._sync_labels()

    def _watch_custom(self) -> None:
        """Editing a detail by hand means the preset no longer describes the job.

        Rather than lock the advanced panel behind the Custom choice — which
        just makes people click twice — touching anything in it moves the
        selection to Custom, so what the panel shows is always what will run.
        """
        for var in (self.mode, self.fill, self.protect_dark, self.threshold,
                    self.t_images, self.t_graphics, self.t_text):
            var.trace_add("write", self._maybe_custom)

    def _maybe_custom(self, *_a: object) -> None:
        if not self._applying_preset and self.preset.get() != "custom":
            self.preset.set("custom")
            self.preset_help.config(text=PRESETS["custom"]["help"])

    def _on_fill_chosen(self, _e: object = None) -> None:
        for key, label, _ in engine.FILL_MODES:
            if label == self.fill_label.get():
                self.fill.set(key)
                break
        self._sync_labels()

    def _sync_zoom(self) -> None:
        self.view_before.set_zoom(self.zoom.get())
        self.view_after.set_zoom(self.zoom.get())

    def _step_page(self, delta: int) -> None:
        if self._busy:
            return
        page = max(1, min(self.preview_page.get() + delta, self._total_pages))
        if page == self.preview_page.get():
            return
        self.preview_page.set(page)
        self._on_preview()

    def _on_page_spun(self) -> None:
        if not self._busy and self.pdf_path.get() and self.wm_path.get():
            self._on_preview()

    def _scrollable_column(self, parent: ttk.Frame, *, remember=True) -> ttk.Frame:
        """The settings column, in a pane that scrolls if the screen is short.

        All four steps stacked come to some 800 pixels, which does not fit the
        1366×768 laptops that are still everywhere in an office — and what falls
        off the bottom is step 4, the button that actually does the work. Rather
        than demand a tall window, the column scrolls.
        """
        wrap = ttk.Frame(parent)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        canvas = tk.Canvas(wrap, highlightthickness=0, bg=bg, width=340)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        if remember:
            self._left_canvas, self._left_inner = canvas, inner
        self._scroll_regions.append((canvas, inner))

        def resized(_e: object = None) -> None:
            options = {"width": canvas.winfo_width()}
            if not remember:
                options['width'] = max(canvas.winfo_width(), inner.winfo_reqwidth())
                options['height'] = max(canvas.winfo_height(), inner.winfo_reqheight())
            canvas.itemconfigure(window, **options)
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", resized)
        canvas.bind("<Configure>", resized)

        if not remember:
            hbar = ttk.Scrollbar(wrap, orient="horizontal", command=canvas.xview)
            hbar.grid(row=1, column=0, sticky="ew")
            canvas.configure(xscrollcommand=hbar.set)
        return inner

    def _scrollable_workspace(self, parent: ttk.Frame) -> ttk.Frame:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        return self._scrollable_column(parent, remember=False)

    def _reveal_focus(self, event) -> None:
        widget = event.widget
        for canvas, inner in self._scroll_regions:
            ancestor = widget
            while ancestor is not None and ancestor is not inner:
                ancestor = getattr(ancestor, 'master', None)
            if ancestor is None or not canvas.winfo_ismapped():
                continue
            canvas.update_idletasks()
            region = canvas.bbox("all")
            if not region:
                continue
            top = widget.winfo_rooty() - inner.winfo_rooty()
            bottom = top + widget.winfo_height()
            visible_top = canvas.canvasy(0)
            visible_bottom = visible_top + canvas.winfo_height()
            if top < visible_top:
                canvas.yview_moveto(max(0, top - 10) / max(1, region[3]))
            elif bottom > visible_bottom:
                canvas.yview_moveto(max(0, bottom - canvas.winfo_height() + 10) / max(1, region[3]))
            left = widget.winfo_rootx() - inner.winfo_rootx()
            right = left + widget.winfo_width()
            visible_left = canvas.canvasx(0)
            if left < visible_left:
                canvas.xview_moveto(max(0, left - 10) / max(1, region[2]))
            elif right > visible_left + canvas.winfo_width():
                canvas.xview_moveto(max(0, right - canvas.winfo_width() + 10) / max(1, region[2]))
            break

    def _scroll_workspace(self, event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        # Lists, image previews and value pickers retain their own wheel behavior.
        if isinstance(widget, (tk.Listbox, tk.Text, ttk.Treeview, ttk.Combobox, ttk.Spinbox)):
            return
        if isinstance(widget, tk.Canvas) and all(widget is not canvas for canvas, _ in self._scroll_regions):
            return
        for canvas, inner in self._scroll_regions:
            ancestor = widget
            while ancestor is not None and ancestor not in (inner, canvas):
                ancestor = getattr(ancestor, 'master', None)
            if ancestor is None:
                continue
            number = getattr(event, 'num', 0)
            step = -1 if number == 4 or getattr(event, 'delta', 0) > 0 else 1
            canvas.yview_scroll(step * 3, "units")
            break

    def _build_compress_tab(self, root: ttk.Frame) -> None:
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, minsize=310)
        root.rowconfigure(0, weight=1)
        queue_panel = ttk.Frame(root, padding=(0, 0, 22, 0))
        queue_panel.grid(row=0, column=0, sticky="nsew")
        queue_panel.columnconfigure(0, weight=1)
        ttk.Label(queue_panel, text="Selected files", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        toolbar = ttk.Frame(queue_panel)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for label, command in (("Add files…", self._cmp_add),
                               ("Remove selected", self._cmp_remove), ("Clear", self._cmp_clear)):
            button = ttk.Button(toolbar, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self._busy_buttons.append(button)
        table_wrap = ttk.Frame(queue_panel)
        table_wrap.grid(row=2, column=0, sticky="ew")
        table_wrap.columnconfigure(0, weight=1)
        self.cmp_files: dict[str, tuple[str, bool]] = {}
        self.cmp_list = ttk.Treeview(table_wrap, columns=("name", "type", "size", "folder"),
                                     show="tree headings", selectmode="extended", height=1)
        self.cmp_list.heading("#0", text="Use")
        self.cmp_list.column("#0", width=48, minwidth=48, stretch=False, anchor="center")
        for key, label, width in (("name", "File", 210), ("type", "Type", 64),
                                  ("size", "Size", 76), ("folder", "Folder", 160)):
            self.cmp_list.heading(key, text=label)
            self.cmp_list.column(key, width=width, minwidth=60, stretch=key in {"name", "folder"})
        self.cmp_list.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.cmp_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        hbar = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.cmp_list.xview)
        hbar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.cmp_list.configure(yscrollcommand=sb.set, xscrollcommand=hbar.set)
        self.cmp_list.bind("<Button-1>", self._cmp_click)
        self.cmp_list.bind("<space>", self._cmp_toggle_selected)
        self.cmp_count = tk.StringVar(value="No files added. Choose Add files to begin.")
        ttk.Label(queue_panel, textvariable=self.cmp_count, style="Muted.TLabel", wraplength=460).grid(
            row=3, column=0, sticky="w", pady=(12, 8))
        selection = ttk.Frame(queue_panel)
        selection.grid(row=4, column=0, sticky="w")
        for label, use in (("Check all", True), ("Uncheck all", False)):
            button = ttk.Button(selection, text=label, command=lambda checked=use: self._cmp_check_all(checked))
            button.pack(side="left", padx=(0, 6))
            self._busy_buttons.append(button)
        ttk.Label(queue_panel, text="Click a checkbox or select rows and press Space.\nOnly checked files are processed.",
                  style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(16, 0))

        settings = ttk.Frame(root, padding=(16, 0, 0, 0))
        settings.grid(row=0, column=1, sticky="nsew")
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="Output size", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        for index, label in enumerate(("Reduce to at most", "Set exact file size")):
            ttk.Radiobutton(settings, text=label, variable=self.size_mode, value=label).grid(row=index+1, column=0, sticky="w")
        size_row = ttk.Frame(settings)
        size_row.grid(row=3, column=0, sticky="w", pady=(12, 0))
        ttk.Label(size_row, text="Target").pack(side="left", padx=(0, 8))
        ttk.Spinbox(size_row, from_=1, to=1024000, increment=50, width=9,
                    textvariable=self.target_kb).pack(side="left")
        ttk.Label(size_row, text="KB").pack(side="left", padx=8)
        presets = ttk.Frame(settings)
        presets.grid(row=4, column=0, sticky="w", pady=(8, 12))
        for kb in (200, 500, 1000):
            ttk.Button(presets, text=f"{kb} KB", width=7, command=lambda value=kb: self.target_kb.set(value)).pack(side="left", padx=(0, 5))
        ttk.Label(settings, text="Exact size adds padding when needed. Increasing bytes does not improve image quality.",
                  style="Muted.TLabel", wraplength=280).grid(row=5, column=0, sticky="w", pady=(0, 12))
        ttk.Checkbutton(settings, text="Keep PDF text selectable", variable=self.cmp_keep_text).grid(row=6, column=0, sticky="w")
        ttk.Label(settings, text="Preserves live text; some PDFs may stay above the target.", style="Muted.TLabel",
                  wraplength=280).grid(row=7, column=0, sticky="w", pady=(0, 6))
        ttk.Checkbutton(settings, text="Allow PNG to become JPEG", variable=self.cmp_allow_jpeg).grid(row=8, column=0, sticky="w")
        ttk.Label(settings, text="Only if needed to reduce size. Transparency is lost.", style="Muted.TLabel",
                  wraplength=280).grid(row=9, column=0, sticky="w", pady=(0, 16))
        ttk.Label(settings, text="Output folder").grid(row=10, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(settings, textvariable=self.cmp_outdir).grid(row=11, column=0, sticky="ew")
        ttk.Button(settings, text="Choose folder…", command=lambda: self._pick_dir(self.cmp_outdir)).grid(row=12, column=0, sticky="w", pady=6)
        ttk.Label(settings, text="Blank saves beside each original. Copies use _small or _sized.", style="Muted.TLabel",
                  wraplength=280).grid(row=13, column=0, sticky="w")
        self.btn_compress = ttk.Button(settings, text="Apply file size", style="Primary.TButton", command=self._on_compress)
        self.btn_compress.grid(row=14, column=0, sticky="ew", pady=(18, 0))
        self._busy_buttons.append(self.btn_compress)

    def _cmp_add(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose files to compress",
            filetypes=[("PDF and pictures", "*.pdf *.png *.jpg *.jpeg"),
                       ("PDF files", "*.pdf"),
                       ("Pictures", "*.png *.jpg *.jpeg")])
        self._cmp_add_paths(paths)

    def _cmp_add_paths(self, paths) -> None:
        existing = {str(Path(p).resolve()) for p, _ in self.cmp_files.values()}
        for path in paths:
            p = Path(path)
            if str(p.resolve()) not in existing:
                iid = self.cmp_list.insert("", "end", values=(p.name,
                    p.suffix.lstrip(".").upper(), tools.human_size(p.stat().st_size), str(p.parent)), image=self._file_checks[True], text="")
                self.cmp_files[iid] = (str(p), True)
                existing.add(str(p.resolve()))
        self._cmp_refresh()

    def _cmp_refresh(self) -> None:
        count = len(self.cmp_files)
        checked = sum(use for _, use in self.cmp_files.values())
        self.cmp_list.configure(height=max(1, min(count, 7)))
        self.cmp_count.set(f"{checked} of {count} files checked" if count else
                           "No files added. Choose Add files to begin.")

    def _cmp_toggle(self, iid: str) -> None:
        if self._busy or iid not in self.cmp_files:
            return
        path, use = self.cmp_files[iid]
        self.cmp_files[iid] = (path, not use)
        self.cmp_list.item(iid, image=self._file_checks[not use])
        self._cmp_refresh()

    def _cmp_click(self, event) -> None:
        if self.cmp_list.identify_column(event.x) == "#0":
            self._cmp_toggle(self.cmp_list.identify_row(event.y))

    def _cmp_toggle_selected(self, _event=None) -> str:
        for iid in self.cmp_list.selection():
            self._cmp_toggle(iid)
        return "break"

    def _cmp_check_all(self, use: bool) -> None:
        for iid, (path, _) in self.cmp_files.items():
            self.cmp_files[iid] = (path, use)
            self.cmp_list.item(iid, image=self._file_checks[use])
        self._cmp_refresh()

    def _cmp_remove(self) -> None:
        for iid in self.cmp_list.selection():
            self.cmp_list.delete(iid)
            self.cmp_files.pop(iid, None)
        self._cmp_refresh()

    def _cmp_clear(self) -> None:
        for iid in list(self.cmp_files):
            self.cmp_list.delete(iid)
        self.cmp_files.clear()
        self._cmp_refresh()

    def _build_device_tab(self, root: ttk.Frame) -> None:
        root.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, text="Connected devices", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        for col, text, action in ((1, "Refresh devices", self._refresh_devices),
                                  (2, "Add devices…", self._device_setup)):
            button = ttk.Button(toolbar, text=text, command=action)
            button.grid(row=0, column=col, padx=(8, 0))
            self._busy_buttons.append(button)

        printing = ttk.LabelFrame(root, text="  Print a PDF or picture  ", padding=12)
        printing.grid(row=1, column=0, sticky="ew")
        printing.columnconfigure(1, weight=1)
        ttk.Label(printing, text="File").grid(row=0, column=0, sticky="w")
        ttk.Entry(printing, textvariable=self.print_path).grid(row=0, column=1, sticky="ew", padx=8)
        def choose_print():
            path = filedialog.askopenfilename(filetypes=[("PDF and pictures", "*.pdf *.png *.jpg *.jpeg")])
            if path:
                self.print_path.set(path)
        ttk.Button(printing, text="Choose file…", command=choose_print).grid(row=0, column=2)
        ttk.Label(printing, text="Printer").grid(row=1, column=0, sticky="w", pady=8)
        self.printer_combo = ttk.Combobox(printing, textvariable=self.printer_name)
        self.printer_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        self._build_device_indicator(printing, "printer", 2)

        options = ttk.Frame(printing)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        options.columnconfigure(1, weight=1); options.columnconfigure(3, weight=1)
        def choice(parent, row, col, text, var, values):
            ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", pady=6)
            ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=17).grid(
                row=row, column=col + 1, sticky="ew", padx=(8, 20), pady=6)
        choice(options, 0, 0, "Paper", self.print_paper, ("A4", "Letter", "Legal", "Printer default"))
        choice(options, 0, 2, "Orientation", self.print_orientation, ("Portrait", "Landscape"))
        choice(options, 1, 0, "Scaling", self.print_scaling, ("Fit", "Fill", "Actual size"))
        choice(options, 1, 2, "Two-sided", self.print_duplex, ("Off", "Long edge", "Short edge"))
        ttk.Label(options, text="Margin (mm)").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Spinbox(options, from_=0, to=50, increment=1, textvariable=self.print_margin, width=7).grid(
            row=2, column=1, sticky="w", padx=8)
        ttk.Label(options, text="Copies").grid(row=2, column=2, sticky="w")
        ttk.Spinbox(options, from_=1, to=999, textvariable=self.print_copies, width=7).grid(
            row=2, column=3, sticky="w", padx=8)
        ttk.Label(printing, text="Fit keeps the whole page. Fill crops edges to fill the printable area. Zero margin uses the printer's minimum.",
                  style="Muted.TLabel", wraplength=660).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))
        if sys.platform.startswith("win"):
            ttk.Checkbutton(printing, text="Review native page setup and print preview before printing",
                            variable=self.print_review).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
            hint = "The Windows dialog provides printer properties (quality, color and trays). Native settings take precedence. Print from the preview toolbar."
        else:
            hint = "Enter a CUPS queue name or leave blank for the default. Use Add devices for driver options."
        ttk.Label(printing, text=hint, style="Muted.TLabel", wraplength=660).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        button = ttk.Button(printing, text="Print with settings…", style="Primary.TButton", command=self._on_print)
        button.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._busy_buttons.append(button)

        scanning = ttk.LabelFrame(root, text="  Scan a page  ", padding=12)
        scanning.grid(row=2, column=0, sticky="ew", pady=16)
        scanning.columnconfigure(1, weight=1)
        ttk.Label(scanning, text="Scanner").grid(row=0, column=0, sticky="w")
        self.scanner_combo = ttk.Combobox(scanning, textvariable=self.scanner_name)
        self.scanner_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        self._build_device_indicator(scanning, "scanner", 1)
        actions = ttk.Frame(scanning)
        actions.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        for text, action in (("Check connection", self._check_scanner), ("Scanner settings…", self._scanner_settings)):
            button = ttk.Button(actions, text=text, command=action)
            button.pack(side="left", padx=(0, 8)); self._busy_buttons.append(button)
        options = ttk.Frame(scanning)
        options.grid(row=3, column=0, columnspan=3, sticky="ew")
        options.columnconfigure(1, weight=1); options.columnconfigure(3, weight=1)
        choice(options, 0, 0, "Resolution (DPI)", self.scan_dpi, (75, 150, 200, 300, 600, 1200))
        choice(options, 0, 2, "Color", self.scan_color, ("Color", "Grayscale", "Black & white"))
        choice(options, 1, 0, "Paper source", self.scan_source, ("Driver default", "Flatbed", "Feeder"))
        if sys.platform.startswith("win"):
            ttk.Checkbutton(scanning, text="Show driver acquisition settings (overrides DPI and source above)",
                            variable=self.scan_driver_settings).grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
            hint = "Enter an installed scanner name or WIA DeviceID. A WIA driver is required. Driver dialogs may appear behind this window; check the taskbar."
        else:
            hint = "Enter a SANE device ID. Scanner settings lists supported options; source and color names depend on the driver."
        ttk.Label(scanning, text=hint, style="Muted.TLabel", wraplength=660).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        button = ttk.Button(scanning, text="Scan and save…", style="Primary.TButton", command=self._on_scan)
        button.grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        self._busy_buttons.append(button)
        ttk.Label(root, text="Device fields accept manually entered installed names/IDs. Add devices installs USB/network devices through the OS. Refresh after setup.",
                  style="Muted.TLabel", wraplength=680).grid(row=3, column=0, sticky="w")
        self.scanner_name.trace_add("write", lambda *_: self._device_status("scanner", "unknown", "Selection changed — check connection"))
        self.printer_name.trace_add("write", lambda *_: self._device_status("printer", "unknown", "Selected — driver availability checked before printing"))

    def _build_device_indicator(self, parent, kind, row):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
        lamp = tk.Canvas(frame, width=18, height=18, highlightthickness=0)
        lamp.pack(side="left", padx=(0, 6))
        label = ttk.Label(frame, text="Not checked", wraplength=620)
        label.pack(side="left")
        setattr(self, kind + "_indicator", (lamp, label))
        self._device_status(kind, "unknown", "Not checked — refresh devices")

    def _device_status(self, kind, state, text):
        self._device_states[kind] = (state, text)
        lamp, label = getattr(self, kind + "_indicator")
        lamp.configure(background=self.colors['background'])
        lamp.delete("all")
        color = self.colors['error'] if state == "error" else self.colors['accent'] if state in {"ready", "busy"} else self.colors['muted']
        lamp.create_oval(4, 4, 14, 14, fill=color if state != "unknown" else "", outline=color, width=2)
        if state == "busy":
            lamp.create_line(9, 6, 9, 9, 12, 9, fill=self.colors['surface'], width=1)
        label.configure(text=text)

    def _device_setup(self):
        try:
            devices.open_device_setup()
            self.status.set("Add your device in system settings, then refresh devices here.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _refresh_devices(self):
        if self._busy:
            return
        for kind in ("printer", "scanner"):
            self._device_status(kind, "busy", "Checking installed devices…")
        def work(prog):
            for kind, discover in (("printer", devices.printers), ("scanner", devices.scanners)):
                try:
                    found = discover()
                    self._msgq.put(("device_list", (kind, found, "")))
                except Exception as exc:
                    self._msgq.put(("device_list", (kind, [], str(exc))))
            return ("Devices", "Device refresh complete. Check scanner connection before scanning.", [], "")
        self._start_job("Finding installed devices…", work)

    def _check_scanner(self):
        if self._busy:
            return
        device = self.scanner_name.get().strip()
        self._device_status("scanner", "busy", "Checking scanner connection…")
        def work(prog):
            try:
                text = devices.scanner_status(device)
                self._msgq.put(("device_status", ("scanner", "ready", text)))
                return ("Scanner", text, [], "")
            except Exception as exc:
                self._msgq.put(("device_status", ("scanner", "error", "Unavailable — " + str(exc))))
                raise
        self._start_job("Connecting to scanner…", work)

    def _scanner_settings(self):
        if self._busy:
            return
        device = self.scanner_name.get().strip()
        def work(prog):
            try:
                text = devices.scanner_settings(device)
                if not sys.platform.startswith("win"):
                    self._msgq.put(("scanner_options", text))
                return ("Scanner settings", "Scanner settings closed." if sys.platform.startswith("win") else "Supported scanner options loaded.", [], "")
            except Exception:
                self._msgq.put(("device_status", ("scanner", "error", "Settings unavailable — check connection and driver")))
                raise
        self._start_job("Opening scanner settings…", work)

    def _on_print(self) -> None:
        if self._busy:
            return
        path = self.print_path.get().strip()
        try:
            copies = int(self.print_copies.get())
            margin = float(self.print_margin.get())
            if not 1 <= copies <= 999 or not 0 <= margin <= 50 or not Path(path).is_file() or not tools.kind_of(path):
                raise ValueError("Choose an existing PDF or picture, 1–999 copies and 0–50 mm margins.")
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        printer = self.printer_name.get().strip()
        settings = dict(paper=self.print_paper.get(), orientation=self.print_orientation.get(),
                        scaling=self.print_scaling.get(), margin_mm=margin,
                        duplex=self.print_duplex.get(), review=self.print_review.get())
        if not sys.platform.startswith("win") and not messagebox.askokcancel(APP_TITLE,
                f"Print {Path(path).name}\n\nPrinter: {printer or 'System default'}\n"
                f"{copies} copy/copies · {settings['paper']} · {settings['orientation']}\n"
                f"{settings['scaling']} · {margin:g} mm margins · Two-sided: {settings['duplex']}\n\nSubmit this job?"):
            return
        self._device_status("printer", "busy", "Preparing print settings…")
        def work(prog):
            try:
                result = devices.print_file(path, printer, copies, **settings)
                self._msgq.put(("device_status", ("printer", "unknown", result)))
                return ("Print", result, [], "")
            except Exception:
                self._msgq.put(("device_status", ("printer", "error", "Print failed — check printer settings and connection")))
                raise
        self._start_job("Preparing print job…", work)

    def _on_scan(self) -> None:
        if self._busy:
            return
        device = self.scanner_name.get().strip()
        try:
            dpi = int(self.scan_dpi.get())
            if not device:
                raise ValueError("Refresh devices and select a scanner, or enter its installed name/ID.")
            if not 75 <= dpi <= 1200:
                raise ValueError("Choose a scan resolution from 75 to 1200 DPI.")
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        out = filedialog.asksaveasfilename(title="Save scanned page", defaultextension=".pdf",
                    filetypes=[("PDF", "*.pdf"), ("PNG", "*.png"), ("JPEG", "*.jpg")])
        if not out:
            return
        settings = dict(color=self.scan_color.get(), source=self.scan_source.get(),
                        driver_settings=self.scan_driver_settings.get())
        self._device_status("scanner", "busy", "Scan requested — connecting, waiting for driver or acquiring page…")
        def work(prog):
            try:
                if not devices.scan(out, device, dpi, **settings):
                    self._msgq.put(("device_status", ("scanner", "unknown", "Scan cancelled — no output replaced")))
                    return ("Scan", "Scanning cancelled.", [], "")
                self._msgq.put(("device_status", ("scanner", "ready", "Scan complete — " + Path(out).name)))
                self._msgq.put(("scanned", out))
                return ("Scan", "Scanned page saved.", [out], out)
            except Exception:
                self._msgq.put(("device_status", ("scanner", "error", "Scan failed — check driver, connection and paper source")))
                raise
        self._start_job("Scanning a page — review the driver dialog…", work)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _build_convert_tab(self, root: ttk.Frame) -> None:
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        f1 = ttk.LabelFrame(root, text="  PDF → pictures  ", padding=10)
        f1.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(1, weight=1)

        ttk.Label(f1, text="PDF").grid(row=0, column=0, sticky="w")
        ttk.Entry(f1, textvariable=self.p2i_pdf).grid(row=0, column=1,
                                                      sticky="ew", padx=6)
        ttk.Button(f1, text="Choose…", command=self._p2i_pick).grid(
            row=0, column=2, sticky="w")

        ttk.Label(f1, text="Save into").grid(row=1, column=0, sticky="w",
                                             pady=(6, 0))
        ttk.Entry(f1, textvariable=self.p2i_dir).grid(row=1, column=1,
                                                      sticky="ew", padx=6,
                                                      pady=(6, 0))
        ttk.Button(f1, text="Choose folder…",
                   command=lambda: self._pick_dir(self.p2i_dir)).grid(
                       row=1, column=2, sticky="w", pady=(6, 0))

        opts = ttk.Frame(f1)
        opts.grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(opts, text="Format").pack(side="left")
        ttk.Combobox(opts, textvariable=self.p2i_fmt, width=6,
                     state="readonly", values=["png", "jpeg"]).pack(
                         side="left", padx=(4, 12))
        ttk.Label(opts, text="Quality (DPI)").pack(side="left")
        ttk.Combobox(opts, textvariable=self.p2i_dpi, width=6,
                     state="readonly", values=[96, 150, 200, 300, 400]).pack(
                         side="left", padx=(4, 12))
        ttk.Label(opts, text="Pages").pack(side="left")
        ttk.Entry(opts, textvariable=self.p2i_pages, width=12).pack(
            side="left", padx=4)
        ttk.Label(opts, text="blank = all", foreground="#666").pack(side="left")

        self.btn_p2i = ttk.Button(f1, text="Export pages as pictures", style="Primary.TButton",
                                  command=self._on_pdf_to_images)
        self.btn_p2i.grid(row=3, column=0, columnspan=3, sticky="ew",
                          pady=(10, 0))
        self._busy_buttons.append(self.btn_p2i)

        f2 = ttk.LabelFrame(root, text="  Pictures → PDF  ", padding=10)
        f2.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        f2.columnconfigure(0, weight=1)
        f2.rowconfigure(0, weight=1)

        self.i2p_list = tk.Listbox(f2, selectmode="extended", height=4,
                                   activestyle="none")
        self.i2p_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f2, orient="vertical", command=self.i2p_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.i2p_list.configure(yscrollcommand=sb.set)

        row = ttk.Frame(f2)
        row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(row, text="Add pictures…",
                   command=self._i2p_add).pack(side="left")
        ttk.Button(row, text="Remove",
                   command=lambda: self._list_remove(self.i2p_list)).pack(
                       side="left", padx=6)
        # Page order is the list order, so it has to be adjustable — file
        # dialogs hand back names alphabetically, which is rarely the order
        # someone photographed the pages in.
        ttk.Button(row, text="Move up", width=8,
                   command=lambda: self._list_move(self.i2p_list, -1)).pack(
                       side="left")
        ttk.Button(row, text="Move down", width=10,
                   command=lambda: self._list_move(self.i2p_list, 1)).pack(
                       side="left", padx=(2, 6))
        ttk.Button(row, text="Sort by name",
                   command=self._i2p_sort).pack(side="left")
        ttk.Label(row, text="the list order is the page order",
                  foreground="#666").pack(side="left", padx=10)

        opts = ttk.Frame(f2)
        opts.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(opts, text="Page size").pack(side="left")
        ttk.Combobox(opts, textvariable=self.i2p_size, width=8,
                     state="readonly", values=["image", "a4", "letter"]).pack(
                         side="left", padx=(4, 12))
        ttk.Label(opts, text="Assume DPI").pack(side="left")
        ttk.Combobox(opts, textvariable=self.i2p_dpi, width=6,
                     state="readonly", values=[96, 150, 200, 300]).pack(
                         side="left", padx=(4, 12))
        ttk.Label(opts, text="JPEG quality").pack(side="left")
        ttk.Spinbox(opts, from_=40, to=100, width=5,
                    textvariable=self.i2p_quality).pack(side="left", padx=4)

        out = ttk.Frame(f2)
        out.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Save as").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.i2p_out).grid(row=0, column=1,
                                                       sticky="ew", padx=6)
        ttk.Button(out, text="Choose…", command=self._i2p_pick_out).grid(
            row=0, column=2, sticky="w")

        self.btn_i2p = ttk.Button(f2, text="Combine into one PDF", style="Primary.TButton",
                                  command=self._on_images_to_pdf)
        self.btn_i2p.grid(row=4, column=0, columnspan=2, sticky="ew",
                          pady=(10, 0))
        self._busy_buttons.append(self.btn_i2p)

    @staticmethod
    def _list_remove(box: tk.Listbox) -> None:
        for i in reversed(box.curselection()):
            box.delete(i)

    @staticmethod
    def _list_move(box: tk.Listbox, delta: int) -> None:
        idx = list(box.curselection())
        if not idx:
            return
        # Walk from the edge the items are moving toward, so a block of
        # selected rows slides as one instead of piling up on itself.
        for i in (idx if delta < 0 else reversed(idx)):
            j = i + delta
            if j < 0 or j >= box.size():
                return
            text = box.get(i)
            box.delete(i)
            box.insert(j, text)
            box.selection_set(j)

    def _i2p_add(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose pictures",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg")])
        for p in paths:
            self.i2p_list.insert("end", p)
        if paths and not self.i2p_out.get():
            self.i2p_out.set(str(Path(paths[0]).with_suffix(".pdf")))

    def _i2p_sort(self) -> None:
        items = sorted(self.i2p_list.get(0, "end"),
                       key=lambda p: Path(p).name.lower())
        self.i2p_list.delete(0, "end")
        for p in items:
            self.i2p_list.insert("end", p)

    def _i2p_pick_out(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save PDF as", defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")])
        if path:
            self.i2p_out.set(path)

    def _p2i_pick(self) -> None:
        path = filedialog.askopenfilename(
            title="Select PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        self.p2i_pdf.set(path)
        if not self.p2i_dir.get():
            self.p2i_dir.set(str(Path(path).with_suffix("")) + "_pages")

    def _pick_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Choose a folder")
        if path:
            var.set(path)

    def _log(self, msg: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg.rstrip() + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _sync_labels(self) -> None:
        self.thr_lbl.config(text=f"{self.threshold.get():.2f}")
        self.fill_help.config(text=engine.FILL_HELP.get(self.fill.get(), ""))
        self.page_lbl.config(text=f"of {self._total_pages}")

    def _sync_mode(self) -> None:
        smart = self.mode.get() == "smart"
        for child in self.smart_box.winfo_children():
            child.configure(state="normal" if smart else "disabled")
        for child in self.raster_box.winfo_children():
            if isinstance(child, ttk.Combobox):
                child.configure(state="disabled" if smart else "readonly")
            else:
                try:
                    child.configure(state="disabled" if smart else "normal")
                except tk.TclError:
                    pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._busy_buttons:
            button.config(state=state)
        for name in ("printer_combo", "scanner_combo"):
            if hasattr(self, name):
                getattr(self, name).configure(state=state)

    def _parse_pages(self, total: int) -> tuple[int, ...] | None:
        return self._parse_page_text(self.page_range.get(), total)

    def _settings(self, total: int) -> Settings:
        angles = ((-45.0, -30.0, 0.0, 30.0, 45.0)
                  if self.scan_rotations.get() else (0.0,))
        targets = []
        if self.t_images.get():
            targets.append("images")
        if self.t_graphics.get():
            targets.append("graphics")
        if self.t_text.get():
            targets.append("text")
        return Settings(
            mode=self.mode.get(),
            dpi=int(self.dpi.get()),
            threshold=float(self.threshold.get()),
            angles=angles,
            fill=self.fill.get(),
            protect_dark=bool(self.protect_dark.get()),
            smart_targets=tuple(targets) or ("images",),
            pages=self._parse_pages(total),
        )

    def _page_count(self) -> int:
        return engine.page_count(self.pdf_path.get())

    def _validate(self) -> bool:
        if not self.pdf_path.get() or not Path(self.pdf_path.get()).is_file():
            messagebox.showwarning(APP_TITLE,
                                   "Choose a PDF or a picture first.")
            return False
        if not self.wm_path.get() or not Path(self.wm_path.get()).is_file():
            messagebox.showwarning(
                APP_TITLE, "Choose an image of the watermark to remove.")
            return False
        return True

    def _pick_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="Select PDF or picture",
            filetypes=[("PDF and pictures", "*.pdf *.png *.jpg *.jpeg"),
                       ("PDF files", "*.pdf"),
                       ("Pictures", "*.png *.jpg *.jpeg")])
        if not path:
            return
        self.pdf_path.set(path)
        self.view_before.clear()
        self.view_after.clear()
        try:
            n = self._page_count()
            self._total_pages = n
            self.preview_page.set(1)
            self.pdf_info.config(
                text=f"{n} page(s)" if engine.is_pdf(path) else "picture")
            self._sync_labels()
            self._log(f"Loaded: {Path(path).name}"
                      + (f" ({n} pages)" if engine.is_pdf(path) else ""))
        except Exception as exc:
            self.pdf_info.config(text="unreadable")
            self._log("FAILED to open " + Path(path).name + ": "
                      + self._explain(exc), "fail")
            messagebox.showerror(APP_TITLE, self._explain(exc))
            return
        # A cleaned picture stays a picture; a cleaned PDF stays a PDF.
        suffix = ".pdf" if engine.is_pdf(path) else Path(path).suffix
        self.out_path.set(f"{Path(path).with_suffix('')}_clean{suffix}")

    def _pick_wm(self) -> None:
        path = filedialog.askopenfilename(
            title="Select watermark image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp")])
        if path:
            self._set_watermark(path)
            self._save_prefs()

    def _set_watermark(self, path: str, quiet: bool = False) -> None:
        self.wm_path.set(path)
        try:
            tmpl = engine.load_template(path)
        except Exception as exc:
            if not quiet:
                messagebox.showerror(APP_TITLE, f"Could not read image:\n{exc}")
            return
        self._log(f"Watermark sample: {Path(path).name} "
                  f"({tmpl.shape[1]}×{tmpl.shape[0]} px after trim)")
        self._show_on_canvas(self.wm_canvas,
                             cv2.cvtColor(tmpl, cv2.COLOR_GRAY2RGB), "wm")

    def _clear_watermark(self) -> None:
        self.wm_path.set("")
        self._thumbs.pop("wm", None)
        self.wm_canvas.delete("all")

    def _snip_from_pdf(self) -> None:
        """Let the user drag a rectangle over a rendered page to grab the
        watermark, so no external screenshot tool is needed."""
        if not self.pdf_path.get():
            messagebox.showwarning(APP_TITLE,
                                   "Choose a PDF or a picture first.")
            return
        try:
            if engine.is_pdf(self.pdf_path.get()):
                with fitz.open(self.pdf_path.get()) as doc:
                    idx = max(0, min(self.preview_page.get() - 1,
                                     doc.page_count - 1))
                    page_rgb = engine.render_page(doc[idx], int(self.dpi.get()))
            else:
                page_rgb = engine.read_image(self.pdf_path.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        SnipWindow(self, page_rgb, self._on_snipped)

    def _on_snipped(self, crop_rgb: "np.ndarray") -> None:
        # Kept with the app rather than beside the document: the document may
        # live on a read-only share, and the snippet is worth keeping anyway —
        # the next run picks it up automatically.
        out = config_dir() / "watermark.png"
        ok, buf = cv2.imencode(".png", cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR))
        if not ok:
            messagebox.showerror(APP_TITLE, "Could not encode the snipped area.")
            return
        try:
            out.write_bytes(buf.tobytes())
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the snippet:\n{exc}")
            return
        self._set_watermark(str(out))
        self._save_prefs()

    @staticmethod
    def _explain(exc: BaseException) -> str:
        """Say *why* a job failed, in words that suggest what to do about it.

        A traceback in the log answers "what broke" for whoever wrote the
        program and nothing at all for the person whose document did not clean.
        The failures that actually happen are few and each has an obvious next
        step, so they are named here; anything unrecognised still falls through
        to the exception's own message, and the traceback follows either way.
        """
        text = str(exc)
        low = text.lower()
        if isinstance(exc, FileNotFoundError):
            return (f"The file could not be found any more: {text}. It may "
                    "have been moved, renamed, or sat on a drive or share "
                    "that is no longer connected.")
        if isinstance(exc, PermissionError):
            return (f"Windows or Linux would not let the program write there: "
                    f"{text}. The file is usually still open in a PDF reader, "
                    "or the folder is read-only.")
        if isinstance(exc, MemoryError):
            return ("The machine ran out of memory. Lower Quality (DPI) in "
                    "Advanced options, or clean the pages in smaller batches.")
        if isinstance(exc, InterruptedError):
            return "The job was stopped before it finished."
        if "password" in low or "encrypted" in low:
            return ("The PDF is password-protected, so its pages cannot be "
                    "read. Open it in a PDF reader, save an unprotected copy, "
                    "and clean that.")
        if "cannot open" in low or "no objects found" in low or \
                "format error" in low or "damaged" in low:
            return (f"The PDF could not be read — it looks damaged or is not "
                    f"really a PDF ({text}).")
        if "could not read image" in low:
            return (f"That picture could not be decoded ({text}). Save it as "
                    "a plain PNG or JPEG and try again.")
        if "no space left" in low:
            return "The disk is full, so the cleaned file could not be written."
        if isinstance(exc, cv2.error):
            return (f"The image processing step failed: "
                    f"{text.strip().splitlines()[-1] if text else exc!r}. This "
                    "usually means the watermark crop is larger than the page "
                    "it is being looked for on — re-snip it more tightly.")
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

    def _report_failure(self, reason: str, detail: str) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._set_busy(False)
        self.status.set("Failed. See the activity details.")
        if not self.activity_shown:
            self._toggle_activity()
        self._log("FAILED: " + reason, "fail")
        self._log("Technical detail:", "note")
        for line in detail.strip().splitlines()[-12:]:
            self._log("   " + line, "note")
        messagebox.showerror(APP_TITLE, reason)

    def _post_error(self, exc: BaseException) -> None:
        """Called on the worker thread — never touches a widget."""
        self._msgq.put(("error", (self._explain(exc), traceback.format_exc())))

    def _on_preview(self) -> None:
        if self._busy or not self._validate():
            return
        try:
            total = self._page_count()
            self._total_pages = total
            s = self._settings(total)
            page = max(0, min(self.preview_page.get() - 1, total - 1))
            self.preview_page.set(page + 1)
            self._sync_labels()
        except Exception as exc:
            self._log("FAILED: " + self._explain(exc), "fail")
            messagebox.showerror(APP_TITLE, self._explain(exc))
            return

        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.status.set("Scanning page…")
        # Read Tk variables on the main thread before starting the worker.
        src, wm = self.pdf_path.get(), self.wm_path.get()

        def work() -> None:
            try:
                ann, clean, matches = engine.preview(src, wm, s, page)
                self._msgq.put(("preview", (ann, clean, matches, page + 1)))
            except Exception as exc:
                self._post_error(exc)

        threading.Thread(target=work, daemon=True).start()

    def _on_run(self) -> None:
        if self._busy or not self._validate():
            return
        try:
            total = self._page_count()
            self._total_pages = total
            s = self._settings(total)
        except Exception as exc:
            self._log("FAILED: " + self._explain(exc), "fail")
            messagebox.showerror(APP_TITLE, self._explain(exc))
            return

        default = self.out_path.get() or "clean.pdf"
        suffix = Path(default).suffix or ".pdf"
        types = ([("PDF files", "*.pdf")] if suffix == ".pdf" else
                 [("Pictures", "*.png *.jpg *.jpeg"), ("PDF files", "*.pdf")])
        out = filedialog.asksaveasfilename(
            title="Save cleaned file as", defaultextension=suffix,
            initialfile=Path(default).name, filetypes=types)
        if not out:
            return
        if os.path.abspath(out) == os.path.abspath(self.pdf_path.get()):
            messagebox.showerror(APP_TITLE, "Choose a different output file — "
                                            "keep your original intact.")
            return
        self.out_path.set(out)

        self._set_busy(True)
        self.progress.config(mode="determinate", value=0)
        self.status.set("Working…")
        self._log(f"--- {engine.FILL_LABELS.get(s.fill, s.fill)}, {s.mode} "
                  f"mode, {s.dpi} dpi, threshold {s.threshold:.2f} ---")
        src, wm = self.pdf_path.get(), self.wm_path.get()

        def work() -> None:
            try:
                def prog(frac: float, msg: str) -> None:
                    self._msgq.put(("progress", (frac, msg)))
                rep = engine.process(src, wm, out, s, prog)
                self._msgq.put(("done", (rep, out)))
            except Exception as exc:
                self._post_error(exc)

        threading.Thread(target=work, daemon=True).start()

    def _start_job(self, label: str, work) -> None:
        """Run `work(progress)` on a worker thread, with the UI locked.

        Every long job goes through here, so the buttons, the progress bar and
        the error path behave identically whichever tab started it — and the
        window keeps repainting while a hundred-page PDF is being squeezed.
        """
        self._set_busy(True)
        self.progress.config(mode="determinate", value=0)
        self.status.set(label)
        self._log(f"--- {label} ---")

        def runner() -> None:
            try:
                def prog(frac: float, msg: str) -> None:
                    self._msgq.put(("progress", (frac, msg)))
                result = work(prog)
                self._msgq.put(("finished", result))
            except Exception as exc:
                self._post_error(exc)

        threading.Thread(target=runner, daemon=True).start()

    def _on_compress(self) -> None:
        if self._busy:
            return
        paths = [p for p, use in self.cmp_files.values() if use]
        if not paths:
            messagebox.showwarning(APP_TITLE, "Add files and check at least one row to process.")
            return
        try:
            target = int(self.target_kb.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning(APP_TITLE, "Target size must be a number "
                                              "of kilobytes.")
            return
        if target < 1 or target > 1024000:
            messagebox.showwarning(APP_TITLE, "Choose a target between 1 and 1,024,000 KB.")
            return

        outdir = self.cmp_outdir.get().strip()
        if outdir:
            try:
                Path(outdir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"Cannot use that folder:\n{exc}")
                return

        exact = self.size_mode.get() == "Set exact file size"
        keep_text = bool(self.cmp_keep_text.get())
        allow_jpeg = bool(self.cmp_allow_jpeg.get())

        def work(prog):
            lines, results = [], []
            for i, src in enumerate(paths):
                name = Path(src)
                suffix = "sized" if exact else "small"
                dest = (Path(outdir) / f"{name.stem}_{suffix}{name.suffix}"
                        if outdir else
                        name.with_name(f"{name.stem}_{suffix}{name.suffix}"))
                base = dest
                serial = 1
                while dest.exists() or (allow_jpeg and name.suffix.lower() == ".png"
                                        and dest.with_suffix(".jpg").exists()):
                    dest = base.with_name(f"{base.stem}_{serial}{base.suffix}")
                    serial += 1

                span = 1.0 / len(paths)
                operation = tools.resize_file if exact else tools.compress
                res = operation(
                    str(src), str(dest), target, keep_text, allow_jpeg,
                    lambda f, m, i=i: prog(
                        (i + f) * span, f"{name.name}: {m}"))
                results.append(res)
                lines.append(f"{name.name}  —  {res.summary()}")
                prog((i + 1) * span, f"{name.name} done")

            missed = [r for r in results if not r.reached]
            head = (f"Processed {len(results)} file(s) to "
                    f"{target} KB" + (" exactly." if exact else " or less."))
            if missed:
                head = (f"Compressed {len(results)} file(s); "
                        f"{len(missed)} could not reach {target} KB.")
            return ("File size adjustment finished", head, lines,
                    results[0].path if results else "")

        self._start_job(f"Adjusting {len(paths)} file(s) to {target} KB…",
                        work)

    def _on_pdf_to_images(self) -> None:
        if self._busy:
            return
        pdf = self.p2i_pdf.get().strip()
        if not pdf or not Path(pdf).is_file():
            messagebox.showwarning(APP_TITLE, "Choose a PDF to export.")
            return
        outdir = self.p2i_dir.get().strip() or \
            str(Path(pdf).with_suffix("")) + "_pages"
        try:
            total = engine.page_count(pdf)
            pages = self._parse_page_text(self.p2i_pages.get(), total)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        fmt, dpi = self.p2i_fmt.get(), int(self.p2i_dpi.get())

        def work(prog):
            written = tools.pdf_to_images(pdf, outdir, dpi, fmt, pages,
                                          progress=prog)
            return ("Export finished",
                    f"Wrote {len(written)} picture(s) into {outdir}",
                    [Path(p).name for p in written[:12]]
                    + (["…"] if len(written) > 12 else []),
                    written[0] if written else outdir)

        self._start_job(f"Exporting pages as {fmt.upper()} at {dpi} dpi…", work)

    def _on_images_to_pdf(self) -> None:
        if self._busy:
            return
        paths = [p for p in self.i2p_list.get(0, "end") if Path(p).is_file()]
        if not paths:
            messagebox.showwarning(APP_TITLE, "Add some pictures first.")
            return
        out = self.i2p_out.get().strip()
        if not out:
            out = str(Path(paths[0]).with_suffix(".pdf"))
            self.i2p_out.set(out)
        if Path(out).exists() and not messagebox.askyesno(
                APP_TITLE, f"{Path(out).name} already exists. Replace it?"):
            return

        size, dpi = self.i2p_size.get(), int(self.i2p_dpi.get())
        quality = max(40, min(100, int(self.i2p_quality.get())))

        def work(prog):
            tools.images_to_pdf(paths, out, size, dpi, quality=quality,
                                progress=prog)
            return ("PDF created",
                    f"{len(paths)} picture(s) combined into {Path(out).name}",
                    [f"{tools.human_size(Path(out).stat().st_size)} on disk"],
                    out)

        self._start_job(f"Combining {len(paths)} picture(s) into a PDF…", work)

    @staticmethod
    def _parse_page_text(text: str, total: int) -> tuple[int, ...] | None:
        """Turn "1-3,7" into zero-based page numbers; blank means every page."""
        text = text.strip()
        if not text:
            return None
        out: set[int] = set()
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    out.update(range(int(a) - 1, int(b)))
                except ValueError:
                    raise ValueError(f"Bad page range: {part!r}")
            else:
                try:
                    out.add(int(part) - 1)
                except ValueError:
                    raise ValueError(f"Bad page number: {part!r}")
        pages = tuple(sorted(p for p in out if 0 <= p < total))
        if not pages:
            raise ValueError("That page range does not match any page.")
        return pages

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msgq.get_nowait()
                if kind == "progress":
                    frac, msg = payload
                    self.progress.config(value=frac * 100)
                    self.status.set(msg)
                    self._log(msg)
                elif kind == "preview":
                    ann, clean, matches, pageno = payload
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_busy(False)
                    self.view_before.show(ann)
                    self.view_after.show(clean)
                    self._sync_labels()
                    self.status.set(
                        f"Page {pageno} of {self._total_pages}: "
                        f"{len(matches)} match(es).")
                    self._log(f"Preview page {pageno}: {len(matches)} match(es)"
                              + (f", best score "
                                 f"{max(m.score for m in matches):.2f}"
                                 if matches else
                                 " — lower the sensitivity value or re-crop "
                                 "the watermark image."))
                elif kind == "done":
                    rep, out = payload
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=100)
                    self._set_busy(False)
                    self.status.set(f"Saved: {out}")
                    self._log(f"Done. {rep['hits']} watermark instance(s) "
                              f"removed across {rep['processed']} page(s).")
                    self._log(f"Saved to {out}")
                    # Clear the watermark before the next job.
                    if not self.remember_wm.get():
                        self._clear_watermark()
                        self._log("Cleared the watermark picture — tick “Keep "
                                  "this watermark for next time” to hold on "
                                  "to it.", "note")
                    if messagebox.askyesno(
                            APP_TITLE,
                            f"Cleaned file saved:\n{out}\n\n"
                            f"{rep['hits']} watermark instance(s) removed.\n\n"
                            "Open the containing folder?"):
                        self._open_folder(out)
                elif kind == "device_status":
                    self._device_status(*payload)
                elif kind == "device_list":
                    device_kind, found, error = payload
                    combo = getattr(self, device_kind + "_combo")
                    var = getattr(self, device_kind + "_name")
                    combo.configure(values=found)
                    if not var.get() and found:
                        var.set(found[0])
                    if error:
                        self._device_status(device_kind, "error", "Discovery failed — " + error)
                        self._log(error, "fail")
                    elif found:
                        self._device_status(device_kind, "unknown", f"{len(found)} installed device(s) detected — connection not yet checked")
                    else:
                        self._device_status(device_kind, "unknown", "No devices detected — use Add devices or enter an installed name/ID")
                elif kind == "scanner_options":
                    dialog = tk.Toplevel(self)
                    dialog.title("Supported scanner options")
                    dialog.geometry("720x480")
                    text = tk.Text(dialog, wrap="word", background=self.colors['surface'], foreground=self.colors['text'])
                    text.pack(fill="both", expand=True)
                    text.insert("1.0", payload); text.configure(state="disabled")
                    ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=8)
                elif kind == "scanned":
                    self._cmp_add_paths([payload])
                    self.print_path.set(payload)
                elif kind == "finished":
                    title, head, lines, open_path = payload
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=100)
                    self._set_busy(False)
                    self.status.set(head)
                    self._log(head)
                    for line in lines:
                        self._log("   " + line)
                    body = head + ("\n\n" + "\n".join(lines[:8]) if lines else "")
                    if open_path and messagebox.askyesno(
                            APP_TITLE, f"{body}\n\nOpen the containing folder?"):
                        self._open_folder(open_path)
                elif kind == "error":
                    reason, detail = payload
                    self._report_failure(reason, detail)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _show_on_canvas(self, canvas: tk.Canvas, rgb: "np.ndarray",
                        key: str) -> None:
        """Fit one small picture — the watermark thumbnail — into a canvas."""
        canvas.update_idletasks()
        cw = max(canvas.winfo_width(), 120)
        ch = max(canvas.winfo_height(), 80)
        h, w = rgb.shape[:2]
        scale = min(cw / w, 88 / h)
        nw, nh = max(int(w * scale), 1), max(int(h * scale), 1)
        img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        photo = ImageTk.PhotoImage(Image.fromarray(img))
        self._thumbs[key] = photo  # keep a reference alive
        canvas.delete("all")
        canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")

    @staticmethod
    def _open_folder(path: str) -> None:
        folder = str(Path(path).parent)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:
            pass


class ImageView(ttk.Frame):
    """A preview pane that scrolls and zooms.

    The previews are whole document pages, so at "fit the page into the panel"
    they come out too small to judge whether the watermark really went — which
    is the only question the preview exists to answer. So the page is shown at
    the width of the panel by default and scrolls vertically, like every PDF
    reader does, with the zoom available for a closer look at the spot the mark
    used to be.
    """

    FITS = ("Fit width", "Fit page")
    ZOOMS = FITS + ("50%", "75%", "100%", "150%", "200%", "400%")
    # A 300 dpi A4 page at 400% would be a 200-megapixel bitmap; Tk will happily
    # try, run the machine out of memory and take the job with it.
    MAX_PIXELS = 40_000_000

    def __init__(self, parent: tk.Misc, placeholder: str = "") -> None:
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, bg="#e9e9e9", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        hbar = ttk.Scrollbar(self, orient="horizontal",
                             command=self.canvas.xview)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)

        self._rgb: "np.ndarray | None" = None
        self._photo: ImageTk.PhotoImage | None = None
        self._zoom = "Fit width"
        self._placeholder = placeholder
        self.empty_ink = "#566158"
        self._last_size = (0, 0)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<ButtonPress-1>", self._grab)
        self.canvas.bind("<B1-Motion>", self._drag)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(event, self._wheel)
        self._draw()

    # -- public ------------------------------------------------------------
    def show(self, rgb: "np.ndarray") -> None:
        self._rgb = rgb
        self._draw()

    def clear(self) -> None:
        self._rgb = None
        self._draw()

    def set_zoom(self, zoom: str) -> None:
        self._zoom = zoom
        self._draw()

    # -- internals ---------------------------------------------------------
    def _wheel(self, e: tk.Event) -> None:
        step = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        axis = ("x" if getattr(e, "state", 0) & 0x0001  # Shift
                else "y")
        (self.canvas.yview_scroll if axis == "y"
         else self.canvas.xview_scroll)(step * 3, "units")

    def _grab(self, e: tk.Event) -> None:
        self.canvas.scan_mark(e.x, e.y)

    def _drag(self, e: tk.Event) -> None:
        self.canvas.scan_dragto(e.x, e.y, gain=1)

    def _on_configure(self, _e: object = None) -> None:
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size != self._last_size:
            self._last_size = size
            self._draw()

    def _scale_for(self, w: int, h: int) -> float:
        cw = max(self.canvas.winfo_width(), 60)
        ch = max(self.canvas.winfo_height(), 60)
        if self._zoom == "Fit width":
            return cw / w
        if self._zoom == "Fit page":
            return min(cw / w, ch / h)
        return int(self._zoom.rstrip("%")) / 100.0

    def _draw(self) -> None:
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width(), 60)
        ch = max(self.canvas.winfo_height(), 60)
        if self._rgb is None:
            self._photo = None
            self.canvas.configure(scrollregion=(0, 0, cw, ch))
            self.canvas.create_text(cw // 2, ch // 2, text=self._placeholder,
                                    fill=self.empty_ink, font="TkDefaultFont", width=cw - 40,
                                    justify="center")
            return

        h, w = self._rgb.shape[:2]
        scale = self._scale_for(w, h)
        if w * h * scale * scale > self.MAX_PIXELS:
            scale = (self.MAX_PIXELS / (w * h)) ** 0.5
        nw, nh = max(int(w * scale), 1), max(int(h * scale), 1)
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        img = cv2.resize(self._rgb, (nw, nh), interpolation=interp)
        self._photo = ImageTk.PhotoImage(Image.fromarray(img))
        # Centre a page narrower than the panel; otherwise it sits at the origin
        # and the scrollbars do the work.
        x = max((cw - nw) // 2, 0)
        y = max((ch - nh) // 2, 0)
        self.canvas.create_image(x, y, image=self._photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, max(nw, cw), max(nh, ch)))


class SnipWindow(tk.Toplevel):
    """Drag a rectangle over a rendered page to crop the watermark."""

    def __init__(self, master: tk.Misc, page_rgb: "np.ndarray",
                 on_done) -> None:
        super().__init__(master)
        self.title("Drag a box around the watermark, then release")
        self.page = page_rgb
        self.on_done = on_done
        h, w = page_rgb.shape[:2]
        maxw, maxh = 900, 720
        self.scale = min(maxw / w, maxh / h, 1.0)
        disp = cv2.resize(page_rgb,
                          (int(w * self.scale), int(h * self.scale)),
                          interpolation=cv2.INTER_AREA)
        self.photo = ImageTk.PhotoImage(Image.fromarray(disp))
        self.canvas = tk.Canvas(self, width=disp.shape[1],
                                height=disp.shape[0], cursor="cross")
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.rect = None
        self.x0 = self.y0 = 0
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.transient(master)
        self.grab_set()

    def _down(self, e: tk.Event) -> None:
        self.x0, self.y0 = e.x, e.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline="#e11", width=2)

    def _move(self, e: tk.Event) -> None:
        if self.rect:
            self.canvas.coords(self.rect, self.x0, self.y0, e.x, e.y)

    def _up(self, e: tk.Event) -> None:
        x0, x1 = sorted((self.x0, e.x))
        y0, y1 = sorted((self.y0, e.y))
        if (x1 - x0) < 6 or (y1 - y0) < 6:
            return
        s = self.scale
        crop = self.page[int(y0 / s):int(y1 / s), int(x0 / s):int(x1 / s)]
        self.grab_release()
        self.destroy()
        if crop.size:
            self.on_done(crop)


def _attach_console() -> None:
    """Give the batch mode somewhere to print on Windows.

    The executable is built as a windowed program so that double-clicking it
    does not flash up a console. That also means it starts with no console at
    all, and progress written to stdout goes nowhere — so when it has been
    launched from an existing terminal, borrow that one. Everywhere else this
    is unnecessary and does nothing.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):  # parent's console
            return
        # errors="replace" for the same reason the CLI reconfigures its own
        # streams: a character the console cannot encode must not end the job.
        sys.stdout = open("CONOUT$", "w", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", buffering=1, errors="replace")
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) > 1:
        # Route command-line arguments to batch processing.
        _attach_console()
        from . import cli
        raise SystemExit(cli.main(sys.argv[1:]))

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
