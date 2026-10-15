"""
PDF Studio - a modern desktop app to merge and split PDF files.

Install:
    pip install -U pypdf
    (Tkinter comes with Python on Windows and macOS.
     On Ubuntu/Debian run:  sudo apt install python3-tk)

Run:
    python pdf_studio.py

Features:
    * Merge many PDFs, reorder them by dragging or with buttons
    * Each merged file gets a bookmark for every source file
    * Split by custom page ranges, every N pages, or single pages
    * Live preview of the files that will be created before you click
    * Duplicate, password-protected and damaged files are detected
    * Existing files are never overwritten without asking
    * Work runs in the background, so the window never freezes
    * Ctrl+O opens files in the current tab
"""

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    sys.exit("pypdf is not installed. Run:  pip install -U pypdf")


# ----------------------------------------------------------------------------
# Theme colors
# ----------------------------------------------------------------------------
BG = "#1e1e2e"
PANEL = "#27273a"
PANEL_2 = "#313149"
TEXT = "#e6e6f0"
MUTED = "#9aa0b4"
ACCENT = "#7aa2f7"
SUCCESS = "#9ece6a"
DANGER = "#f7768e"
DARK_TEXT = "#11111b"

# ----------------------------------------------------------------------------
# Split modes
# ----------------------------------------------------------------------------
MODE_CHOICES = [
    ("ranges_combined", "Extract the pages I list into one PDF"),
    ("ranges_separate", "Save each range I list as its own PDF"),
    ("every_n", "Split every N pages"),
    ("singles", "Save every page as its own PDF"),
]
MODE_DEFAULTS = {
    "ranges_combined": "1-3, 5",
    "ranges_separate": "1-3, 4-6",
    "every_n": "5",
    "singles": "",
}
MODE_HINTS = {
    "ranges_combined": "Example: 1-3, 5, 8-10 becomes one PDF. Pages keep the order you type.",
    "ranges_separate": "Example: 1-3, 4-6 becomes two PDFs. Reversed ranges like 6-4 are fixed automatically.",
    "every_n": "Enter how many pages each new PDF should contain.",
    "singles": "One PDF per page. Nothing to enter.",
}

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


# ============================================================================
# Core PDF logic (no GUI code here, so it is easy to test and reuse)
# ============================================================================
class PDFError(Exception):
    """An error with a message that can be shown directly to the user."""


def open_pdf(path):
    """Open a PDF. Files with an empty user password are unlocked automatically."""
    name = Path(path).name
    if not Path(path).is_file():
        raise PDFError(f"'{name}' was not found. It may have been moved or deleted.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        _ = len(reader.pages)  # raises if the file is still locked or damaged
        return reader
    except Exception as exc:
        raise PDFError(
            f"Could not open '{name}'. It may be password protected or damaged."
        ) from exc


def unique_path(path):
    """Return path, or path_1, path_2, ... if that file already exists."""
    path = Path(path)
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def default_output_dir():
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def parse_ranges(text, total):
    """Turn '1-3, 5, 8-10' into [(1, 3), (5, 5), (8, 10)] (1-based, inclusive)."""
    ranges = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", part)
        if not match:
            raise ValueError(f"'{part}' is not a valid range. Use forms like 4 or 2-6.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            start, end = end, start  # smart: accept reversed ranges
        if start < 1 or end > total:
            raise ValueError(f"'{part}' is outside the document, which has {total} pages.")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("Enter at least one page or range, e.g. 1-3, 5, 8-10.")
    return ranges


def parse_every_n(text):
    try:
        size = int(text.strip())
    except ValueError:
        raise ValueError("Enter a whole number, for example 5.") from None
    if size < 1:
        raise ValueError("The number of pages must be at least 1.")
    return size


def build_split_plan(total, mode, value_text):
    """Return a list of (label, [0-based page indexes]) - one entry per output file."""
    if mode == "singles":
        return [(f"page_{i}", [i - 1]) for i in range(1, total + 1)]

    if mode == "every_n":
        size = parse_every_n(value_text)
        plan = []
        for start in range(0, total, size):
            end = min(start + size, total)
            plan.append((f"part_{start + 1}-{end}", list(range(start, end))))
        return plan

    ranges = parse_ranges(value_text, total)

    if mode == "ranges_separate":
        return [
            (f"pages_{a}-{b}" if a != b else f"page_{a}", list(range(a - 1, b)))
            for a, b in ranges
        ]

    if mode == "ranges_combined":
        # dict.fromkeys removes duplicates but keeps the order the user typed
        pages = list(dict.fromkeys(p - 1 for a, b in ranges for p in range(a, b + 1)))
        return [("extracted", pages)]

    raise ValueError(f"Unknown mode: {mode}")


def merge_pdfs(sources, output, progress=None):
    """Merge PDFs in order. Each source file becomes a bookmark in the result."""
    writer = PdfWriter()
    for done, source in enumerate(sources, start=1):
        writer.append(open_pdf(source), outline_item=Path(source).stem)
        if progress:
            progress(done, len(sources))
    output = Path(output)
    with output.open("wb") as handle:
        writer.write(handle)
    return output


def split_pdf(source, plan, output_dir, progress=None):
    """Write one PDF per plan entry. Returns the list of created files."""
    reader = open_pdf(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for done, (label, pages) in enumerate(plan, start=1):
        writer = PdfWriter()
        for index in pages:
            writer.add_page(reader.pages[index])
        target = unique_path(output_dir / f"{Path(source).stem}_{label}.pdf")
        with target.open("wb") as handle:
            writer.write(handle)
        created.append(target)
        if progress:
            progress(done, len(plan))
    return created


def open_folder(folder):
    """Open a folder in the system file manager."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)  # Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError:
        messagebox.showinfo("Saved", f"Files saved in:\n{folder}")


# ============================================================================
# GUI
# ============================================================================
class PDFStudio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Studio")
        self.geometry("980x660")
        self.minsize(880, 600)
        self.configure(bg=BG)

        self.events = queue.Queue()
        self.busy = False
        self.action_buttons = []
        self.last_dir = str(Path.home())
        self.default_out = default_output_dir()

        # Merge state
        self.merge_files = []  # list of (Path, page_count)
        self._drag_index = None
        self._auto_name = True

        # Split state
        self.split_source = None
        self.split_total = 0
        self.split_mode = tk.StringVar(value="ranges_combined")
        self.split_value = tk.StringVar(value=MODE_DEFAULTS["ranges_combined"])
        self.hint_var = tk.StringVar()
        self.preview_var = tk.StringVar()

        self._setup_style()
        self._build_header()
        self._build_status_bar()
        self._build_tabs()
        self._bind_shortcuts()

        # Live updates are switched on only after every widget exists
        self.split_mode.trace_add("write", self._on_mode_change)
        self.split_value.trace_add("write", self._update_split_preview)
        self._on_mode_change()

        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ style
    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)

        style.configure("TButton", background=PANEL_2, foreground=TEXT,
                        borderwidth=0, focusthickness=0, padding=(12, 8))
        style.map("TButton", background=[("active", "#3b3b58")])
        style.configure("Accent.TButton", background=ACCENT, foreground=DARK_TEXT,
                        font=("Segoe UI", 10, "bold"), padding=(16, 9))
        style.map("Accent.TButton",
                  background=[("active", "#8fb4ff"), ("disabled", "#44475a")],
                  foreground=[("disabled", MUTED)])

        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        padding=(18, 8), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", DARK_TEXT)])

        style.configure("TRadiobutton", background=PANEL, foreground=TEXT)
        style.map("TRadiobutton", background=[("active", PANEL)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT,
                        insertcolor=TEXT, bordercolor=PANEL_2, padding=6)
        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=PANEL, borderwidth=0)

    # ---------------------------------------------------------------- layout
    def _build_header(self):
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=24, pady=(18, 10))
        ttk.Label(header, text="PDF Studio", font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(header, text="Merge and split your PDFs in seconds",
                  foreground=MUTED).pack(side="left", padx=14, pady=(8, 0))

    def _build_status_bar(self):
        bar = ttk.Frame(self, style="Card.TFrame", padding=(16, 8))
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var, style="Card.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bar, length=280, maximum=100,
                                        style="Horizontal.TProgressbar")
        self.progress.pack(side="right")

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=24, pady=(4, 16))

        self.merge_tab = ttk.Frame(self.notebook, padding=16)
        self.split_tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.merge_tab, text="  Merge PDFs  ")
        self.notebook.add(self.split_tab, text="  Split PDF  ")

        self._build_merge_tab()
        self._build_split_tab()

    def _build_merge_tab(self):
        tab = self.merge_tab

        # Footer is packed first so it stays visible when the window shrinks
        footer = ttk.Frame(tab, style="Card.TFrame", padding=14)
        footer.pack(side="bottom", fill="x", pady=(14, 0))
        footer.columnconfigure(1, weight=1)
        footer.columnconfigure(3, weight=1)

        self.merge_name = tk.StringVar(value="merged.pdf")
        ttk.Label(footer, text="File name", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        name_entry = ttk.Entry(footer, textvariable=self.merge_name, width=30)
        name_entry.grid(row=0, column=1, sticky="ew")
        # Once the user types a name, stop auto-naming it
        name_entry.bind("<Key>", lambda e: setattr(self, "_auto_name", False))

        ttk.Label(footer, text="Save in", style="Card.TLabel").grid(
            row=0, column=2, sticky="w", padx=(18, 10))
        self.merge_folder = tk.StringVar(value=str(self.default_out))
        ttk.Entry(footer, textvariable=self.merge_folder, width=34).grid(
            row=0, column=3, sticky="ew")
        ttk.Button(footer, text="Browse", command=self.choose_merge_folder).grid(
            row=0, column=4, padx=(8, 0))

        self.merge_button = ttk.Button(footer, text="Merge PDFs", style="Accent.TButton",
                                       command=self.start_merge)
        self.merge_button.grid(row=0, column=5, padx=(18, 0))
        self.action_buttons.append(self.merge_button)

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=14)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Merge order  (drag rows, or use the buttons)",
                  style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        list_wrap = ttk.Frame(left, style="Card.TFrame")
        list_wrap.pack(fill="both", expand=True, pady=(10, 10))
        scroll = ttk.Scrollbar(list_wrap)
        scroll.pack(side="right", fill="y")
        self.file_listbox = tk.Listbox(
            list_wrap,
            bg=PANEL_2, fg=TEXT,
            selectbackground=ACCENT, selectforeground=DARK_TEXT,
            highlightthickness=0, borderwidth=0, activestyle="none",
            font=("Segoe UI", 10), selectmode="browse",
            yscrollcommand=scroll.set,
        )
        self.file_listbox.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.file_listbox.yview)
        self.file_listbox.bind("<ButtonPress-1>", self._drag_start)
        self.file_listbox.bind("<B1-Motion>", self._drag_motion)
        self.file_listbox.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_index", None))
        self.file_listbox.bind("<Delete>", lambda e: self.remove_selected())

        self.merge_summary = ttk.Label(left, text="", style="Muted.TLabel")
        self.merge_summary.pack(anchor="w")

        right = ttk.Frame(body, style="Card.TFrame", padding=14)
        right.pack(side="right", fill="y", padx=(14, 0))
        actions = [
            ("+  Add PDFs", self.add_merge_files),
            ("Remove", self.remove_selected),
            ("▲  Move up", lambda: self.move_selected(-1)),
            ("▼  Move down", lambda: self.move_selected(1)),
            ("Clear all", self.clear_merge_files),
        ]
        for text, command in actions:
            button = ttk.Button(right, text=text, command=command, width=16)
            button.pack(fill="x", pady=4)
            self.action_buttons.append(button)

        self.refresh_merge_list()

    def _build_split_tab(self):
        tab = self.split_tab

        footer = ttk.Frame(tab, style="Card.TFrame", padding=14)
        footer.pack(side="bottom", fill="x", pady=(14, 0))
        footer.columnconfigure(1, weight=1)
        ttk.Label(footer, text="Save in", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        self.split_folder = tk.StringVar(value=str(self.default_out))
        ttk.Entry(footer, textvariable=self.split_folder).grid(row=0, column=1, sticky="ew")
        ttk.Button(footer, text="Browse", command=self.choose_split_folder).grid(
            row=0, column=2, padx=(8, 0))
        self.split_button = ttk.Button(footer, text="Split PDF", style="Accent.TButton",
                                       command=self.start_split)
        self.split_button.grid(row=0, column=3, padx=(18, 0))
        self.action_buttons.append(self.split_button)

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body, style="Card.TFrame", padding=16)
        top.pack(fill="x")
        choose = ttk.Button(top, text="Choose PDF", command=self.choose_split_file)
        choose.pack(side="left")
        self.action_buttons.append(choose)

        info = ttk.Frame(top, style="Card.TFrame")
        info.pack(side="left", padx=18)
        self.split_file_label = ttk.Label(info, text="No file selected",
                                          style="Card.TLabel", font=("Segoe UI", 11, "bold"))
        self.split_file_label.pack(anchor="w")
        self.split_info_label = ttk.Label(info, text="Pick a PDF to see its page count",
                                          style="Muted.TLabel")
        self.split_info_label.pack(anchor="w")

        middle = ttk.Frame(body, style="Card.TFrame", padding=16)
        middle.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(middle, text="How do you want to split it?", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        for key, text in MODE_CHOICES:
            ttk.Radiobutton(middle, text=text, value=key,
                            variable=self.split_mode).pack(anchor="w", pady=3)

        ttk.Label(middle, text="Pages / number", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(16, 4))
        self.value_entry = ttk.Entry(middle, textvariable=self.split_value, width=42)
        self.value_entry.pack(anchor="w")
        ttk.Label(middle, textvariable=self.hint_var, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 0))

        self.preview_label = ttk.Label(middle, textvariable=self.preview_var,
                                       style="Card.TLabel", wraplength=720, justify="left")
        self.preview_label.pack(anchor="w", pady=(18, 0))

    def _bind_shortcuts(self):
        self.bind("<Control-o>", self._open_shortcut)

    def _open_shortcut(self, _event=None):
        if self.busy:
            return
        if self.notebook.index(self.notebook.select()) == 0:
            self.add_merge_files()
        else:
            self.choose_split_file()

    # --------------------------------------------------------- background job
    def run_async(self, func, args, on_success, status_text):
        """Run a PDF job in a thread so the window stays responsive."""
        if self.busy:
            return
        self._set_busy(True, status_text)

        def progress(done, total):
            self.events.put(("progress", done, total))

        def worker():
            try:
                result = func(*args, progress=progress)
                self.events.put(("done", on_success, result))
            except Exception as exc:  # shown to the user below
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, done, total = event
                    self.progress.configure(maximum=max(total, 1), value=done)
                elif kind == "done":
                    _, callback, result = event
                    self._set_busy(False, "Done")
                    callback(result)
                elif kind == "error":
                    self._set_busy(False, "Something went wrong")
                    messagebox.showerror("Could not complete the task", event[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _set_busy(self, busy, text=""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.action_buttons:
            button.configure(state=state)
        if busy:
            self.progress.configure(value=0)
        else:
            self.progress.configure(value=100)
        self.status_var.set(text)

    # ---------------------------------------------------------------- merge
    def add_merge_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            title="Choose PDF files",
            initialdir=self.last_dir,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not paths:
            return
        self.last_dir = str(Path(paths[0]).parent)

        known = {path.resolve() for path, _ in self.merge_files}
        problems = []
        for raw in paths:
            path = Path(raw)
            if path.resolve() in known:
                problems.append(f"{path.name}: already in the list")
                continue
            try:
                pages = len(open_pdf(path).pages)
            except PDFError as exc:
                problems.append(str(exc))
                continue
            self.merge_files.append((path, pages))
            known.add(path.resolve())

        # Smart naming: suggest "<first file>_merged.pdf" until the user types a name
        if self._auto_name and self.merge_files:
            self.merge_name.set(f"{self.merge_files[0][0].stem}_merged.pdf")

        self.refresh_merge_list()
        if problems:
            messagebox.showwarning("Some files were skipped", "\n".join(problems))

    def refresh_merge_list(self):
        self.file_listbox.delete(0, "end")
        for i, (path, pages) in enumerate(self.merge_files, start=1):
            unit = "page" if pages == 1 else "pages"
            self.file_listbox.insert("end", f"  {i:>2}.   {path.name}      {pages} {unit}")

        if self.merge_files:
            total = sum(pages for _, pages in self.merge_files)
            self.merge_summary.configure(
                text=f"{len(self.merge_files)} file(s)  |  {total} total pages  |  "
                     "each file gets a bookmark in the result")
        else:
            self.merge_summary.configure(text='No files yet. Click "+ Add PDFs" to begin.')

    def _select_merge_row(self, index):
        self.file_listbox.selection_clear(0, "end")
        self.file_listbox.selection_set(index)
        self.file_listbox.activate(index)
        self.file_listbox.see(index)

    def remove_selected(self):
        selection = self.file_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        del self.merge_files[index]
        self.refresh_merge_list()
        if self.merge_files:
            self._select_merge_row(min(index, len(self.merge_files) - 1))

    def move_selected(self, direction):
        selection = self.file_listbox.curselection()
        if not selection:
            self.status_var.set("Select a file first, then move it.")
            return
        i = selection[0]
        j = i + direction
        if not 0 <= j < len(self.merge_files):
            return
        self.merge_files[i], self.merge_files[j] = self.merge_files[j], self.merge_files[i]
        self.refresh_merge_list()
        self._select_merge_row(j)

    def clear_merge_files(self):
        if not self.merge_files:
            return
        if messagebox.askyesno("Clear list", "Remove all files from the merge list?"):
            self.merge_files.clear()
            self._auto_name = True
            self.merge_name.set("merged.pdf")
            self.refresh_merge_list()

    def _drag_start(self, event):
        if not self.merge_files:
            self._drag_index = None
            return
        self._drag_index = self.file_listbox.nearest(event.y)

    def _drag_motion(self, event):
        if self._drag_index is None or not self.merge_files:
            return
        target = self.file_listbox.nearest(event.y)
        if target == self._drag_index or not 0 <= target < len(self.merge_files):
            return
        item = self.merge_files.pop(self._drag_index)
        self.merge_files.insert(target, item)
        self._drag_index = target
        self.refresh_merge_list()
        self._select_merge_row(target)

    def choose_merge_folder(self):
        folder = filedialog.askdirectory(initialdir=self.merge_folder.get() or str(self.default_out))
        if folder:
            self.merge_folder.set(folder)

    def start_merge(self):
        if len(self.merge_files) < 2:
            messagebox.showinfo("Add more files", "Add at least two PDF files to merge.")
            return

        folder = Path(self.merge_folder.get().strip() or self.default_out).expanduser()
        name = INVALID_FILENAME_CHARS.sub("_", self.merge_name.get().strip()) or "merged"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        output = folder / name

        sources = [path for path, _ in self.merge_files]
        if output.resolve() in {p.resolve() for p in sources}:
            messagebox.showwarning(
                "Name clash",
                "The merged file cannot have the same name as one of the source files.")
            return

        if output.exists():
            overwrite = messagebox.askyesno(
                "File exists",
                f"'{output.name}' already exists.\n\nOverwrite it?\n\n"
                "(No will save the result as a new copy.)")
            if not overwrite:
                output = unique_path(output)

        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Folder error", f"Could not create the folder:\n{exc}")
            return

        count = len(sources)
        self.run_async(
            merge_pdfs, (sources, output),
            lambda result: self.on_merge_done(result, count),
            f"Merging {count} files...",
        )

    def on_merge_done(self, output, count):
        self.status_var.set(f"Merged {count} files into {output.name}")
        if messagebox.askyesno("Merge complete",
                               f"Saved as:\n{output}\n\nOpen the folder?"):
            open_folder(output.parent)

    # ---------------------------------------------------------------- split
    def choose_split_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Choose a PDF",
            initialdir=self.last_dir,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not path:
            return
        try:
            total = len(open_pdf(path).pages)
        except PDFError as exc:
            messagebox.showerror("Cannot open PDF", str(exc))
            return

        source = Path(path)
        self.last_dir = str(source.parent)
        self.split_source = source
        self.split_total = total
        self.split_file_label.configure(text=source.name)
        self.split_info_label.configure(
            text=f"{total} page{'s' if total != 1 else ''}  |  "
                 f"{format_size(source.stat().st_size)}")
        # Smart default: save results in a folder next to the original file
        self.split_folder.set(str(source.parent / f"{source.stem}_split"))
        self._update_split_preview()

    def choose_split_folder(self):
        folder = filedialog.askdirectory(initialdir=self.split_folder.get() or str(self.default_out))
        if folder:
            self.split_folder.set(folder)

    def _on_mode_change(self, *_):
        mode = self.split_mode.get()
        self.split_value.set(MODE_DEFAULTS[mode])
        self.hint_var.set(MODE_HINTS[mode])
        self.value_entry.configure(state="disabled" if mode == "singles" else "normal")
        self._update_split_preview()

    def _update_split_preview(self, *_):
        """Show what will be created while the user types, with errors in red."""
        if self.split_source is None:
            self.preview_var.set("Choose a PDF to see what will be created.")
            self.preview_label.configure(foreground=MUTED)
            return
        try:
            plan = build_split_plan(self.split_total, self.split_mode.get(), self.split_value.get())
        except ValueError as exc:
            self.preview_var.set(f"⚠  {exc}")
            self.preview_label.configure(foreground=DANGER)
            return

        names = [f"{self.split_source.stem}_{label}.pdf" for label, _ in plan]
        shown = ", ".join(names[:3])
        if len(names) > 3:
            shown += f"  ... and {len(names) - 3} more"
        self.preview_var.set(f"✓  Will create {len(plan)} file(s):  {shown}")
        self.preview_label.configure(foreground=SUCCESS)

    def start_split(self):
        if self.split_source is None:
            messagebox.showinfo("No PDF", "Choose a PDF file first.")
            return
        try:
            plan = build_split_plan(self.split_total, self.split_mode.get(), self.split_value.get())
        except ValueError as exc:
            messagebox.showwarning("Check your pages", str(exc))
            return

        out_dir = Path(self.split_folder.get().strip() or self.default_out).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Folder error", f"Could not create the folder:\n{exc}")
            return

        self.run_async(
            split_pdf, (self.split_source, plan, out_dir),
            lambda files: self.on_split_done(files, out_dir),
            f"Splitting into {len(plan)} file(s)...",
        )

    def on_split_done(self, files, out_dir):
        self.status_var.set(f"Created {len(files)} file(s)")
        if messagebox.askyesno("Split complete",
                               f"Created {len(files)} file(s).\n\nOpen the output folder?"):
            open_folder(out_dir)


if __name__ == "__main__":
    app = PDFStudio()
    app.mainloop()
