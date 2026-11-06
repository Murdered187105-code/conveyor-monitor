# -*- coding: utf-8 -*-
r"""
КОНТРОЛЬ ДЕТАЛЕЙ НА КОНВЕЙЕРЕ — версия 6: самообучающийся ИИ-автопилот.

Вкладки:
  * «Ручной режим»  — всё из v5: зона, 12 линий, шкала, очерёдность (config.json).
  * «ИИ-Автопилот»  — сам находит столб деталей, следит за деталью, кликает и учится
                      по очкам (ai_config.json, база ai_database\).
  * «Предпросмотр»  — что видит ИИ: столб, зона, деталь, прогноз, маски.
  * «Логи»          — общий журнал обоих режимов.

Горячие клавиши (из любого окна): F8 — отправка в ручном режиме, F9 — старт/стоп ИИ.

Запуск: python conveyor_monitor_v6.py  (недостающие библиотеки установятся сами).
"""
# =============================================================================
# 1. Автоустановка библиотек (до всех остальных импортов)
# =============================================================================
import importlib.util
import subprocess
import sys

CORE_PACKAGES = [("cv2", "opencv-python"), ("numpy", "numpy"), ("mss", "mss"),
                 ("PIL", "pillow")]
# Необязательные: ставятся в фоне после запуска окна (easyocr большой — PyTorch ~1–2 ГБ)
OPTIONAL_PACKAGES = [("dxcam", "dxcam"), ("easyocr", "easyocr")]


def missing_packages(pkgs):
    return [pip for mod, pip in pkgs if importlib.util.find_spec(mod) is None]


def pip_install(pkgs):
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pkgs]
    try:
        return subprocess.call(cmd)
    except Exception as exc:
        print("pip не запустился:", exc)
        return 1


_core_missing = missing_packages(CORE_PACKAGES)
if _core_missing:
    print("Устанавливаю недостающие библиотеки:", ", ".join(_core_missing))
    if pip_install(_core_missing) != 0:
        print("Не удалось установить библиотеки. Выполните вручную:\n"
              f"  {sys.executable} -m pip install {' '.join(_core_missing)}")
        input("Нажмите Enter для выхода…")
        sys.exit(1)
    importlib.invalidate_caches()

# =============================================================================
# 2. Импорты
# =============================================================================
import ctypes          # noqa: E402
import os              # noqa: E402
import threading       # noqa: E402
import time            # noqa: E402
import traceback       # noqa: E402
from pathlib import Path  # noqa: E402

import cv2             # noqa: E402
import numpy as np     # noqa: E402
import tkinter as tk   # noqa: E402
from tkinter import ttk, messagebox, filedialog  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

import conveyor_monitor_v5 as cm  # noqa: E402  (ручной режим, захват, Interception)
import ai_autopilot as ai          # noqa: E402

APP_TITLE = "Конвейер · ИИ-Автопилот"
VERSION = "6.0"

# =============================================================================
# 3. Тёмная тема
# =============================================================================
P = {
    "bg": "#0d1117", "panel": "#161b22", "card": "#1c2330", "card2": "#222b38",
    "border": "#30363d", "text": "#e6edf3", "muted": "#8b949e", "accent": "#2f81f7",
    "accent_h": "#4c95f9", "ok": "#2ea043", "ok_h": "#3fb950", "warn": "#d29922",
    "err": "#da3633", "err_h": "#f85149", "violet": "#a371f7",
}
FONT = "Segoe UI"


def apply_dark_theme(root):
    root.configure(bg=P["bg"])
    for opt, val in (("*Background", P["bg"]), ("*Foreground", P["text"]),
                     ("*selectBackground", P["accent"]), ("*selectForeground", "white"),
                     ("*insertBackground", P["text"]), ("*highlightThickness", 0),
                     ("*Listbox.background", P["card"]), ("*Text.background", P["card"]),
                     ("*TCombobox*Listbox.background", P["card"]),
                     ("*TCombobox*Listbox.foreground", P["text"]),
                     ("*TCombobox*Listbox.selectBackground", P["accent"]),
                     ("*TCombobox*Listbox.font", (FONT, 10))):
        root.option_add(opt, val)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=P["bg"], foreground=P["text"], fieldbackground=P["card"],
                bordercolor=P["border"], darkcolor=P["panel"], lightcolor=P["panel"],
                troughcolor=P["card"], selectbackground=P["accent"],
                selectforeground="white", insertcolor=P["text"], font=(FONT, 10),
                focuscolor=P["accent"])
    s.configure("TFrame", background=P["bg"])
    s.configure("Card.TFrame", background=P["panel"])
    s.configure("Header.TFrame", background=P["panel"])
    s.configure("TLabel", background=P["bg"], foreground=P["text"])
    s.configure("Card.TLabel", background=P["panel"], foreground=P["text"])
    s.configure("Title.TLabel", font=(FONT, 16, "bold"))
    s.configure("HTitle.TLabel", background=P["panel"], font=(FONT, 17, "bold"))
    s.configure("HSub.TLabel", background=P["panel"], foreground=P["muted"], font=(FONT, 9))
    s.configure("Bold.TLabel", font=(FONT, 10, "bold"))
    s.configure("CardBold.TLabel", background=P["panel"], font=(FONT, 10, "bold"))
    s.configure("Hint.TLabel", foreground=P["muted"], font=(FONT, 9))
    s.configure("CardHint.TLabel", background=P["panel"], foreground=P["muted"],
                font=(FONT, 9))
    s.configure("Score.TLabel", background=P["panel"], foreground=P["text"],
                font=(FONT, 40, "bold"))
    s.configure("Stat.TLabel", background=P["panel"], font=(FONT, 15, "bold"))
    s.configure("TLabelframe", background=P["bg"], bordercolor=P["border"], relief="solid")
    s.configure("TLabelframe.Label", background=P["bg"], foreground=P["muted"],
                font=(FONT, 9, "bold"))
    s.configure("Card.TLabelframe", background=P["panel"], bordercolor=P["border"])
    s.configure("Card.TLabelframe.Label", background=P["panel"], foreground=P["muted"],
                font=(FONT, 10, "bold"))
    # Кнопки
    s.configure("TButton", background=P["card2"], foreground=P["text"],
                bordercolor=P["border"], padding=(10, 5), relief="flat", focusthickness=0)
    s.map("TButton", background=[("pressed", P["accent"]), ("active", P["border"]),
                                 ("disabled", P["card"])],
          foreground=[("disabled", P["muted"])])
    for name, base, hover in (("Accent", P["accent"], P["accent_h"]),
                              ("Go", P["ok"], P["ok_h"]), ("Danger", P["err"], P["err_h"])):
        s.configure(f"{name}.TButton", background=base, foreground="white",
                    bordercolor=base, font=(FONT, 12, "bold"), padding=(14, 10))
        s.map(f"{name}.TButton", background=[("pressed", base), ("active", hover),
                                             ("disabled", P["card"])],
              foreground=[("disabled", P["muted"])])
    s.configure("Big.TButton", font=(FONT, 11, "bold"), padding=8)
    # Флажки и переключатели
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=P["bg"], foreground=P["text"],
                    indicatorbackground=P["card"], indicatorforeground="white",
                    indicatorcolor=P["card"], upperbordercolor=P["border"],
                    lowerbordercolor=P["border"])
        s.map(w, background=[("active", P["bg"])],
              indicatorcolor=[("selected", P["accent"]), ("pressed", P["accent"])])
        s.configure(f"Card.{w}", background=P["panel"])
        s.map(f"Card.{w}", background=[("active", P["panel"])],
              indicatorcolor=[("selected", P["accent"])])
    s.configure("Bold.TCheckbutton", font=(FONT, 10, "bold"))
    # Вкладки
    s.configure("TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
    s.configure("TNotebook.Tab", background=P["panel"], foreground=P["muted"],
                padding=(16, 8), font=(FONT, 10, "bold"), borderwidth=0)
    s.map("TNotebook.Tab", background=[("selected", P["card2"]), ("active", P["card"])],
          foreground=[("selected", P["text"]), ("active", P["text"])])
    s.configure("Main.TNotebook.Tab", padding=(22, 10), font=(FONT, 11, "bold"))
    # Поля ввода
    for w in ("TEntry", "TSpinbox", "TCombobox"):
        s.configure(w, fieldbackground=P["card"], foreground=P["text"],
                    background=P["card2"], arrowcolor=P["text"], bordercolor=P["border"],
                    insertcolor=P["text"], padding=3)
        s.map(w, fieldbackground=[("readonly", P["card"]), ("disabled", P["panel"])],
              foreground=[("disabled", P["muted"])])
    s.configure("TScale", background=P["accent"], troughcolor=P["card"],
                bordercolor=P["border"], lightcolor=P["accent"], darkcolor=P["accent"],
                sliderthickness=14, sliderlength=18)
    s.map("TScale", background=[("active", P["accent_h"])])
    s.configure("TScrollbar", background=P["card2"], troughcolor=P["bg"],
                bordercolor=P["bg"], arrowcolor=P["muted"])
    s.configure("Horizontal.TProgressbar", background=P["ok"], troughcolor=P["card"],
                bordercolor=P["border"], thickness=8)
    s.configure("Perfect.Horizontal.TProgressbar", background=P["violet"])
    s.configure("Normal.Horizontal.TProgressbar", background=P["accent"])
    s.configure("Miss.Horizontal.TProgressbar", background=P["err"])
    s.configure("TSeparator", background=P["border"])


cm.THEME.update({"dark": True, "text": P["text"], "muted": P["muted"], "accent": P["accent"],
                 "list_bg": P["card"], "hl_bg": "#1f6f3a", "hl_fg": "white",
                 "bar_bg": P["card"], "bar_border": P["border"], "log_bg": P["card"]})
cm.STYLE_HOOK = apply_dark_theme


# =============================================================================
# 4. Вспомогательные виджеты
# =============================================================================
class ScrollFrame(ttk.Frame):
    """Прокручиваемая колонка (колесо мыши работает, когда курсор над ней)."""

    def __init__(self, parent, width=420, style="TFrame"):
        super().__init__(parent, style=style)
        bg = P["panel"] if style == "Card.TFrame" else P["bg"]
        self.canvas = tk.Canvas(self, width=width, bg=bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style=style)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))


def card(parent, title, **grid):
    f = ttk.LabelFrame(parent, text="  " + title + "  ", style="Card.TLabelframe",
                       padding=(14, 10))
    if grid:
        f.grid(**grid)
    return f


class RegionPicker:
    """Выделение прямоугольника на экране мышью (затемнение всего экрана)."""

    def __init__(self, root, title, callback):
        self.cb = callback
        u = cm.user32
        self.vx, self.vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.35)
        self.win.geometry(f"{vw}x{vh}{self.vx:+d}{self.vy:+d}")
        self.cv = tk.Canvas(self.win, bg="black", cursor="crosshair", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.create_text(vw // 2, 60, fill="white", font=(FONT, 20, "bold"),
                            text=f"{title}\nЗажмите левую кнопку и обведите область. Esc — отмена.",
                            justify="center")
        self.start = None
        self.rect = None
        self.cv.bind("<ButtonPress-1>", self._down)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._up)
        self.win.bind("<Escape>", lambda e: self._close(None))
        self.win.focus_force()
        self.win.grab_set()

    def _down(self, e):
        self.start = (e.x_root, e.y_root)
        self.rect = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#2f81f7", width=3)

    def _move(self, e):
        if self.start:
            sx, sy = self.start[0] - self.vx, self.start[1] - self.vy
            self.cv.coords(self.rect, sx, sy, e.x, e.y)

    def _up(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x_root, e.y_root
        x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        self._close((x, y, w, h) if w >= 10 and h >= 10 else None)

    def _close(self, result):
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass
        self.cb(result)


# =============================================================================
# 5. Главное окно
# =============================================================================
AI_SLIDERS = [
    # ключ, подпись, мин, макс, единицы, подсказка
    ("latency_ms", "Задержка системы", 0, 200, "мс",
     "Время от кадра до нажатия в игре. ИИ дальше подстраивает тайминг сам."),
    ("lead_ms", "Ручное смещение тайминга", -300, 300, "мс",
     "+ кликать раньше, − позже. Добавляется к выученному смещению."),
    ("motion_thr", "Порог движения", 2, 60, "×0.1 пикс",
     "Меньше — замечает медленные детали, больше — меньше ложных срабатываний."),
    ("bg_thr", "Отделение от фона", 2, 60, "%",
     "Насколько пиксель должен отличаться от восстановленной карты фона, чтобы "
     "считаться деталью."),
    ("tex_thr", "Чувствительность к узорам", 4, 60, "",
     "Меньше — сильнее ловит детали по текстуре, больше — игнорирует мелкий шум фона."),
    ("min_block", "Минимальный размер детали", 1, 50, "‰", "Доля площади кадра (промилле)."),
    ("zone_h", "Высота зоны над столбом", 5, 80, "%", "Для наглядности и поиска детали."),
    ("cooldown_ms", "Пауза между кликами", 100, 3000, "мс", ""),
    ("settle_ms", "Ожидание приземления", 200, 3000, "мс",
     "Сколько ждать, пока деталь ляжет на столб, перед оценкой."),
    ("perfect_pct", "«Идеал» по геометрии", 1, 25, "%",
     "Ошибка меньше N% ширины детали = +2 (если очки не читаются)."),
    ("explore_pct", "Исследование (пробы)", 0, 50, "%",
     "Как часто ИИ пробует другое смещение, чтобы найти лучшее. Со временем уменьшается."),
    ("learn_rate", "Скорость обучения", 5, 100, "%", ""),
    ("match_pct", "Порог «та же деталь»", 40, 95, "%",
     "Сходство, при котором деталь (в том числе обрезанная) считается известным типом."),
    ("work_width", "Ширина анализа", 160, 720, "пикс",
     "Больше — точнее, но медленнее. 360 — хороший баланс."),
]
AI_FLAGS = [
    ("ocr_on", "Считывать очки на экране (easyocr): +2 / +1 / 0"),
    ("learn_on", "Обучаться (тайминг и виды деталей)"),
    ("save_db", "Сохранять снимки в базу данных"),
    ("dry_run", "Пробный прогон — без кликов (только прогноз)"),
    ("show_preview", "Обновлять предпросмотр"),
]


class MainWindow:
    PREV_W, PREV_H = 420, 330

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE}  v{VERSION}")
        self.root.geometry("1300x1000")
        self.root.minsize(1150, 820)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass
        apply_dark_theme(self.root)
        self._closing = False
        self._photos = {}
        self._f9_prev = False
        self._picker = None
        self._countdown_left = 0
        self._roi_confirmed = False

        self._build_header()
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(6, 10))

        # --- Ручной режим (v5 целиком) ---
        self.manual_tab = ttk.Frame(self.nb)
        self.nb.add(self.manual_tab, text="  Ручной режим  ")
        self.manual = cm.App(root=self.root, container=self.manual_tab, show_title=False)
        self.manual.is_visible = lambda: self.nb.select() == str(self.manual_tab)
        self.sender = self.manual.sender
        self.shared = self.manual.shared
        orig_append = self.manual._append_log

        def append_both(line):
            orig_append(line)
            self._log_append(line)
        self.manual._append_log = append_both

        # --- ИИ ---
        self.pilot = ai.Autopilot(self.sender, self.shared.log)
        self.pilot.start()
        self.ai_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.ai_tab, text="  ИИ-Автопилот  ")
        self.prev_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.prev_tab, text="  Предпросмотр  ")
        self.log_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.log_tab, text="  Логи  ")
        self._build_ai_tab()
        self._build_preview_tab()
        self._build_log_tab()
        self.nb.select(self.ai_tab)
        self.roi_overlay = cm.RoiOverlay(self.root)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._tick)
        self.root.after(cm.AUTOSAVE_MS, self._autosave)
        self.shared.log(f"{APP_TITLE} v{VERSION} запущен. F9 — старт/стоп ИИ, F8 — отправка "
                        f"в ручном режиме.")
        threading.Thread(target=self._install_optional, daemon=True).start()

    # ----------------------------------------------------------------- шапка
    def _build_header(self):
        hdr = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 12))
        hdr.pack(fill="x")
        left = ttk.Frame(hdr, style="Header.TFrame")
        left.pack(side="left")
        ttk.Label(left, text="◆ " + APP_TITLE, style="HTitle.TLabel").pack(anchor="w")
        ttk.Label(left, text="Самообучающийся автопилот: поиск столба, оптический поток, "
                             "шаблоны деталей, очки и обучение с подкреплением",
                  style="HSub.TLabel").pack(anchor="w")
        right = ttk.Frame(hdr, style="Header.TFrame")
        right.pack(side="right")
        self.pill_ai = tk.Label(right, text="", font=(FONT, 10, "bold"), fg="white",
                                bg=P["card2"], padx=14, pady=6)
        self.pill_ai.pack(side="right", padx=(8, 0))
        self.pill_manual = tk.Label(right, text="", font=(FONT, 10, "bold"), fg="white",
                                    bg=P["card2"], padx=14, pady=6)
        self.pill_manual.pack(side="right", padx=(8, 0))
        self.pill_drv = tk.Label(right, text="", font=(FONT, 10, "bold"), fg="white",
                                 bg=P["card2"], padx=14, pady=6)
        self.pill_drv.pack(side="right")

    # ------------------------------------------------------------- вкладка ИИ
    def _build_ai_tab(self):
        cfg = self.pilot.cfg
        t = self.ai_tab
        t.columnconfigure(0, weight=3, uniform="c")
        t.columnconfigure(1, weight=2, uniform="c")
        t.rowconfigure(0, weight=1)
        left = ttk.Frame(t)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=1)

        # --- Управление ---
        ctl = card(left, "Управление", row=0, column=0, columnspan=2, sticky="ew")
        for c in range(4):
            ctl.columnconfigure(c, weight=1)
        self.btn_start = ttk.Button(ctl, text="▶  СТАРТ", style="Go.TButton",
                                    command=self.start_ai)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_stop = ttk.Button(ctl, text="■  СТОП", style="Danger.TButton",
                                   command=self.stop_ai)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(ctl, text="⚡  Тест клика (3 с)", style="Accent.TButton",
                   command=self.test_click).grid(row=0, column=2, sticky="ew", padx=6)
        ttk.Button(ctl, text="📂  Открыть папку БД", style="Accent.TButton",
                   command=self.open_db).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        self.ctl_hint = tk.StringVar(value="F9 — старт/стоп из любого окна. Перед стартом "
                                           "выберите область игры справа.")
        ttk.Label(ctl, textvariable=self.ctl_hint, style="CardHint.TLabel",
                  wraplength=760).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        # --- Счёт ---
        sc = card(left, "Счёт и оценка", row=1, column=0, sticky="nsew", pady=(10, 0),
                  padx=(0, 5))
        sc.columnconfigure(1, weight=1)
        self.v_score = tk.StringVar(value="—")
        ttk.Label(sc, textvariable=self.v_score, style="Score.TLabel").grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 18))
        self.v_last = tk.StringVar(value="Последняя деталь: —")
        ttk.Label(sc, textvariable=self.v_last, style="CardBold.TLabel").grid(
            row=0, column=1, sticky="w")
        self.v_acc = tk.StringVar(value="Точность: —")
        ttk.Label(sc, textvariable=self.v_acc, style="CardHint.TLabel").grid(
            row=1, column=1, sticky="w")
        self.bars = {}
        for r, (key, name, style) in enumerate((("perfect", "+2 идеал", "Perfect"),
                                                ("normal", "+1 обычно", "Normal"),
                                                ("miss", "0 промах", "Miss")), start=2):
            ttk.Label(sc, text=name, style="Card.TLabel").grid(row=r, column=0, sticky="w",
                                                               pady=(8, 0))
            bar = ttk.Progressbar(sc, style=f"{style}.Horizontal.TProgressbar", maximum=100)
            bar.grid(row=r, column=1, sticky="ew", pady=(8, 0))
            var = tk.StringVar(value="0")
            ttk.Label(sc, textvariable=var, style="CardBold.TLabel", width=6,
                      anchor="e").grid(row=r, column=2, sticky="e", pady=(8, 0))
            self.bars[key] = (bar, var)

        # --- Состояние ИИ ---
        stt = card(left, "Мозг ИИ", row=1, column=1, sticky="nsew", pady=(10, 0),
                   padx=(5, 0))
        stt.columnconfigure(1, weight=1)
        self.v_state = tk.StringVar(value="Остановлен")
        tk.Label(stt, textvariable=self.v_state, font=(FONT, 12, "bold"), bg=P["panel"],
                 fg=P["accent_h"], anchor="w", wraplength=330, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.state_vars = {}
        rows = [("fps", "Кадров/с"), ("tower", "Столб"), ("speed", "Скорость детали"),
                ("offset", "Смещение тайминга"), ("fall", "Время падения"),
                ("drift", "Снос при падении"), ("types", "Видов деталей в базе"),
                ("learn", "Обучающих попыток"), ("explore", "Исследование"),
                ("ocr", "Чтение очков")]
        for r, (key, name) in enumerate(rows, start=1):
            ttk.Label(stt, text=name, style="CardHint.TLabel").grid(row=r, column=0,
                                                                    sticky="w", pady=1)
            v = tk.StringVar(value="—")
            ttk.Label(stt, textvariable=v, style="Card.TLabel", wraplength=230).grid(
                row=r, column=1, sticky="e", pady=1)
            self.state_vars[key] = v

        # --- Мини-предпросмотр ---
        mp = card(left, "Что видит ИИ (кратко)", row=2, column=0, columnspan=2,
                  sticky="nsew", pady=(10, 0))
        left.rowconfigure(2, weight=1)
        self.mini_lbl = tk.Label(mp, text="Нажмите «СТАРТ» — здесь появится картинка",
                                 bg="#05080c", fg=P["muted"], width=80, height=14)
        self.mini_lbl.pack(fill="both", expand=True)

        # --- Правая колонка: настройки ---
        sf = ScrollFrame(t, width=440)
        sf.grid(row=0, column=1, sticky="nsew")
        col = sf.inner
        col.columnconfigure(0, weight=1)
        self.ai_vars = {}

        area = card(col, "Область игры и счёта", row=0, column=0, sticky="ew")
        area.columnconfigure(0, weight=1)
        area.columnconfigure(1, weight=1)
        self.v_roi = tk.StringVar()
        ttk.Label(area, textvariable=self.v_roi, style="Card.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(area, text="Выбрать область игры мышью",
                   command=lambda: self.pick_region("roi")).grid(
            row=1, column=0, sticky="ew", pady=(6, 0), padx=(0, 4))
        ttk.Button(area, text="Весь основной экран", command=self.full_screen_roi).grid(
            row=1, column=1, sticky="ew", pady=(6, 0), padx=(4, 0))
        self.v_score_roi = tk.StringVar()
        ttk.Label(area, textvariable=self.v_score_roi, style="Card.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(area, text="Выбрать область счёта мышью",
                   command=lambda: self.pick_region("score")).grid(
            row=3, column=0, sticky="ew", pady=(6, 0), padx=(0, 4))
        ttk.Button(area, text="Счёт: авто (верх по центру)", command=self.auto_score_roi).grid(
            row=3, column=1, sticky="ew", pady=(6, 0), padx=(4, 0))
        ttk.Button(area, text="Показать области на экране (3 с)",
                   command=self.flash_regions).grid(row=4, column=0, columnspan=2,
                                                    sticky="ew", pady=(6, 0))
        self._refresh_roi_text()

        clk = card(col, "Клик и захват", row=1, column=0, sticky="ew", pady=(10, 0))
        clk.columnconfigure(1, weight=1)
        ttk.Label(clk, text="Клавиша / кнопка", style="Card.TLabel").grid(row=0, column=0,
                                                                         sticky="w")
        self.ai_vars["key"] = tk.StringVar(value=cfg["key"])
        ttk.Combobox(clk, textvariable=self.ai_vars["key"], values=cm.KEYS, state="readonly",
                     width=14).grid(row=0, column=1, sticky="e")
        ttk.Label(clk, text="Удержание, мс", style="Card.TLabel").grid(row=1, column=0,
                                                                      sticky="w", pady=(6, 0))
        self.ai_vars["hold_ms"] = tk.IntVar(value=cfg["hold_ms"])
        ttk.Spinbox(clk, from_=0, to=300, textvariable=self.ai_vars["hold_ms"],
                    width=8).grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Label(clk, text="Захват экрана", style="Card.TLabel").grid(row=2, column=0,
                                                                      sticky="w", pady=(6, 0))
        self.ai_vars["capture"] = tk.StringVar(value=cfg["capture"])
        ttk.Combobox(clk, textvariable=self.ai_vars["capture"],
                     values=["mss", "dxcam"] if cm.dxcam is not None else ["mss"],
                     state="readonly", width=14).grid(row=2, column=1, sticky="e", pady=(6, 0))
        ttk.Label(clk, text="mouse_left — клик мышью в текущей точке курсора. Нажатие идёт "
                            "через драйвер Interception, как в ручном режиме.",
                  style="CardHint.TLabel", wraplength=380).grid(row=3, column=0, columnspan=2,
                                                                sticky="w", pady=(6, 0))

        flg = card(col, "Режимы", row=2, column=0, sticky="ew", pady=(10, 0))
        for r, (key, text) in enumerate(AI_FLAGS):
            self.ai_vars[key] = tk.BooleanVar(value=bool(cfg[key]))
            ttk.Checkbutton(flg, text=text, variable=self.ai_vars[key],
                            style="Card.TCheckbutton").grid(row=r, column=0, sticky="w", pady=1)

        tune = card(col, "Тонкая настройка", row=3, column=0, sticky="ew", pady=(10, 0))
        tune.columnconfigure(0, weight=1)
        r = 0
        for key, label, lo, hi, unit, hint in AI_SLIDERS:
            var = tk.IntVar(value=int(cfg[key]))
            self.ai_vars[key] = var
            top = ttk.Frame(tune, style="Card.TFrame")
            top.grid(row=r, column=0, sticky="ew", pady=(6, 0))
            ttk.Label(top, text=label, style="CardBold.TLabel").pack(side="left")
            val = tk.StringVar()
            ttk.Label(top, textvariable=val, style="Card.TLabel").pack(side="right")

            def upd(*_, var=var, val=val, unit=unit):
                try:
                    val.set(f"{int(var.get())} {unit}")
                except (tk.TclError, ValueError):
                    pass
            var.trace_add("write", upd)
            upd()
            ttk.Scale(tune, from_=lo, to=hi, variable=var, orient="horizontal",
                      command=lambda v, var=var: var.set(int(round(float(v))))).grid(
                row=r + 1, column=0, sticky="ew")
            r += 2
            if hint:
                ttk.Label(tune, text=hint, style="CardHint.TLabel", wraplength=380,
                          justify="left").grid(row=r, column=0, sticky="w")
                r += 1

        misc = card(col, "Обучение и база", row=4, column=0, sticky="ew", pady=(10, 0))
        misc.columnconfigure(0, weight=1)
        misc.columnconfigure(1, weight=1)
        ttk.Button(misc, text="Сохранить обучение", command=self.pilot.persist).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(misc, text="Сбросить тайминг", command=self.reset_learning).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(misc, text=f"База: {ai.DB_DIR}\nНастройки: {ai.AI_CONFIG_FILE.name} "
                             "(автосохранение раз в минуту и при выходе).",
                  style="CardHint.TLabel", wraplength=380, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        for key, var in self.ai_vars.items():
            var.trace_add("write", lambda *_: self._push_ai())
        self._push_ai()

    # ------------------------------------------------------ вкладка предпросмотра
    def _build_preview_tab(self):
        t = self.prev_tab
        self.prev_lbls = []
        titles = ["Что видит ИИ: столб (зелёный), зона и цель (голубой), деталь (жёлтый), "
                  "скорость (оранжевая стрелка), прогноз (фиолетовый)",
                  "Отделение от фона (карта фона + гистограмма + текстура): белое — объект",
                  "Оптический поток Farneback: яркое — движение"]
        for i, title in enumerate(titles):
            box = card(t, title, row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            lbl = tk.Label(box, bg="#05080c", fg=P["muted"], text="Нет изображения",
                           width=70, height=18)
            lbl.pack(fill="both", expand=True)
            self.prev_lbls.append(lbl)
        t.columnconfigure(0, weight=1)
        t.columnconfigure(1, weight=1)
        t.rowconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)
        legend = card(t, "Как читать", row=1, column=1, sticky="nsew", padx=6, pady=6)
        ttk.Label(legend, style="Card.TLabel", wraplength=480, justify="left", text=(
            "• Столб — неподвижный объект внизу области (может стоять на платформе). "
            "Его верх — цель.\n"
            "• Фон восстанавливается картой по пикселям без объектов и уточняется "
            "гистограммой цвета вокруг столба, поэтому деталь отделяется даже на градиенте.\n"
            "• Скорость детали — по оптическому потоку и истории положений (учитывается "
            "ускорение, подходит для «маятника» и случайной скорости).\n"
            "• Клик — когда прогноз места падения (фиолетовая линия) совпадёт с центром "
            "столба с учётом задержки и выученного смещения.\n"
            "• После каждой детали ИИ читает очки (+2/+1/0) и поправляет тайминг.\n"
            "• Серия снимков за пролёт превращается в шаблон детали. Обрезанные после промаха "
            "детали узнаются как искажения известного типа.")).pack(anchor="w")

    # ------------------------------------------------------------ вкладка логов
    def _build_log_tab(self):
        t = self.log_tab
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Очистить", command=self._log_clear).pack(side="left")
        ttk.Button(bar, text="Сохранить в файл…", command=self._log_save).pack(side="left",
                                                                                padx=8)
        self.log_only_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Только сообщения ИИ", variable=self.log_only_ai).pack(
            side="left", padx=8)
        frame = ttk.Frame(t)
        frame.pack(fill="both", expand=True)
        self.log_txt = tk.Text(frame, wrap="word", font=("Consolas", 10), bg=P["card"],
                               fg=P["text"], relief="flat", padx=10, pady=8,
                               state="disabled")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_txt.tag_configure("ai", foreground="#79c0ff")
        self.log_txt.tag_configure("ok", foreground=P["ok_h"])
        self.log_txt.tag_configure("perfect", foreground="#d2a8ff")
        self.log_txt.tag_configure("err", foreground=P["err_h"])
        self.log_txt.tag_configure("time", foreground=P["muted"])

    def _log_append(self, line):
        if self.log_only_ai.get() and "[ИИ]" not in line:
            return
        low = line.lower()
        tag = ("err" if ("ошибка" in low or "промах" in low or "не удалось" in low)
               else "perfect" if "идеал" in low
               else "ok" if ("сигнал" in low or "+1" in line)
               else "ai" if "[ИИ]" in line else "")
        t = self.log_txt
        t.configure(state="normal")
        if line.startswith("[") and "] " in line:
            stamp, rest = line.split("] ", 1)
            t.insert("end", stamp + "] ", "time")
            t.insert("end", rest + "\n", tag)
        else:
            t.insert("end", line + "\n", tag)
        if int(t.index("end-1c").split(".")[0]) > 3000:
            t.delete("1.0", "500.0")
        t.see("end")
        t.configure(state="disabled")

    def _log_clear(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    def _log_save(self):
        path = filedialog.asksaveasfilename(title="Сохранить журнал", defaultextension=".txt",
                                            initialfile=f"журнал_{time.strftime('%Y%m%d_%H%M')}.txt",
                                            filetypes=[("Текст", "*.txt")])
        if path:
            Path(path).write_text(self.log_txt.get("1.0", "end"), encoding="utf-8")

    # ------------------------------------------------------------ настройки ИИ
    def _collect_ai(self):
        cfg = dict(self.pilot.cfg)
        for key, var in self.ai_vars.items():
            try:
                cfg[key] = var.get()
            except (tk.TclError, ValueError):
                pass
        return cfg

    def _push_ai(self):
        self.pilot.set_config(self._collect_ai())

    def _save_ai(self):
        try:
            ai.save_config(self.pilot.cfg)
        except Exception as exc:
            self.shared.log(f"[ИИ] Не удалось сохранить {ai.AI_CONFIG_FILE.name}: {exc}")

    def _autosave(self):
        if self._closing:
            return
        self._save_ai()
        self.root.after(cm.AUTOSAVE_MS, self._autosave)

    def _refresh_roi_text(self):
        c = self.pilot.cfg
        x, y, w, h = ai.Autopilot._roi(c)
        self.v_roi.set(f"Область игры: {w}×{h} пикс, от ({x}, {y})")
        if c["score_w"] >= 8:
            self.v_score_roi.set(f"Область счёта: {c['score_w']}×{c['score_h']} пикс, "
                                 f"от ({c['score_x']}, {c['score_y']})")
        else:
            self.v_score_roi.set("Область счёта: авто (верхняя часть области игры)")

    def _set_cfg(self, **kw):
        cfg = self._collect_ai()
        cfg.update(kw)
        self.pilot.set_config(cfg)
        self._refresh_roi_text()
        self._save_ai()

    def pick_region(self, which):
        if self._picker is not None:
            return
        title = "Область ИГРЫ: игровое поле со столбом и деталью (без кнопок и таблиц вокруг)" \
            if which == "roi" \
            else "Область СЧЁТА (только цифры очков)"

        def done(r):
            self._picker = None
            self.root.deiconify()
            if r is None:
                return
            x, y, w, h = r
            if which == "roi":
                self._set_cfg(roi_x=x, roi_y=y, roi_w=w, roi_h=h)
                self.shared.log(f"[ИИ] Область игры: {w}×{h} от ({x}, {y}).")
            else:
                self._set_cfg(score_x=x, score_y=y, score_w=w, score_h=h)
                self.shared.log(f"[ИИ] Область счёта: {w}×{h} от ({x}, {y}).")
            self.flash_regions()
        self.root.iconify()                 # окно программы не должно закрывать игру
        self.root.after(250, lambda: setattr(self, "_picker",
                                             RegionPicker(self.root, title, done)))
        self._picker = "opening"

    def full_screen_roi(self):
        x, y, w, h = ai._screen_rect()
        self._set_cfg(roi_x=x, roi_y=y, roi_w=w, roi_h=h)

    def auto_score_roi(self):
        self._set_cfg(score_x=0, score_y=0, score_w=0, score_h=0)

    def flash_regions(self):
        x, y, w, h = ai.Autopilot._roi(self.pilot.cfg)
        sw, sh = cm.user32.GetSystemMetrics(0), cm.user32.GetSystemMetrics(1)
        if w < sw or h < sh:
            self.roi_overlay.show(x, y, w, h)
            self.root.after(3000, self.roi_overlay.hide)
        sx, sy, sw_, sh_ = self.pilot._score_rect(self.pilot.cfg)
        self.shared.log(f"[ИИ] Область игры {w}×{h} от ({x}, {y}); счёт {sw_}×{sh_} "
                        f"от ({sx}, {sy}).")

    # ------------------------------------------------------------ действия
    def start_ai(self):
        if self.pilot.running.is_set():
            return
        self._push_ai()
        x, y, w, h = ai.Autopilot._roi(self.pilot.cfg)
        sw, sh = cm.user32.GetSystemMetrics(0), cm.user32.GetSystemMetrics(1)
        if (w, h) == (sw, sh) and not self._roi_confirmed:
            ans = messagebox.askyesnocancel(
                APP_TITLE, "Область игры — весь экран. ИИ может принять за «столб» "
                           "элементы сайта или окна.\n\nДа — выбрать игровое поле мышью "
                           "(рекомендуется)\nНет — запустить на весь экран\nОтмена — не "
                           "запускать")
            if ans is None:
                return
            if ans:
                self.pick_region("roi")
                return
            self._roi_confirmed = True
        if self.shared.armed:
            self.shared.set_armed(False, "запущен ИИ-автопилот")
        self.shared.paused.set()            # ручной режим не захватывает экран
        if not self.sender.available and not self.pilot.cfg["dry_run"]:
            self.shared.log("[ИИ] Внимание: Interception недоступен — клики не пройдут. "
                            "Включите «Пробный прогон» или установите драйвер.")
        self.pilot.start_pilot()

    def stop_ai(self):
        self.pilot.stop_pilot()
        self.shared.paused.clear()

    def test_click(self):
        if self._countdown_left > 0:
            return
        self._countdown_left = 3
        self._countdown()

    def _countdown(self):
        if self._countdown_left > 0:
            self.ctl_hint.set(f"Тест клика через {self._countdown_left} с — переключитесь "
                              f"на игру…")
            self._countdown_left -= 1
            self.root.after(1000, self._countdown)
            return
        cfg = self.pilot.cfg
        ok = self.sender.request(cfg["key"], int(cfg["hold_ms"]), time.perf_counter(), 0.0,
                                 "тест клика ИИ-автопилота")
        self.ctl_hint.set("Тест: нажатие отправлено." if ok else
                          "Тест не прошёл: " + (self.sender.error or "драйвер занят"))

    def open_db(self):
        ai.DB_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(ai.DB_DIR))

    def reset_learning(self):
        if messagebox.askyesno(APP_TITLE, "Сбросить выученный тайминг и статистику очков?\n"
                                          "Снимки и виды деталей в базе останутся."):
            self.pilot.reset_learning()

    # ------------------------------------------------------- фоновая установка
    def _install_optional(self):
        missing = missing_packages(OPTIONAL_PACKAGES)
        if not missing:
            return
        self.shared.log(f"[ИИ] Устанавливаю в фоне: {', '.join(missing)}. Для easyocr это "
                        f"может занять 5–15 минут (скачивается PyTorch).")
        for pkg in missing:
            rc = pip_install([pkg])
            self.shared.log(f"[ИИ] {pkg}: " + ("установлен." if rc == 0 else
                                                f"ошибка установки (код {rc})."))
        importlib.invalidate_caches()
        if "easyocr" in missing and self.pilot.cfg["ocr_on"] and self.pilot.running.is_set():
            self.pilot.ocr.load_async()
        if "dxcam" in missing:
            self.shared.log("[ИИ] dxcam станет доступен после перезапуска программы.")

    # ------------------------------------------------------------ обновление
    def _tick(self):
        try:
            self._update()
        except Exception:
            traceback.print_exc()
        if not self._closing:
            self.root.after(60, self._tick)

    def _update(self):
        f9 = cm.user32.GetAsyncKeyState(0x78) < 0
        if f9 and not self._f9_prev:
            self.stop_ai() if self.pilot.running.is_set() else self.start_ai()
        self._f9_prev = f9
        running = self.pilot.running.is_set()
        if running and self.shared.armed:   # F8 во время работы ИИ — не даём кликать вдвоём
            self.shared.set_armed(False, "работает ИИ-автопилот — сначала остановите его")

        st = self.pilot.get_stats()
        # Шапка
        if self.sender.available:
            self.pill_drv.configure(text="● Interception", bg=P["ok"])
        else:
            self.pill_drv.configure(text="● Interception нет", bg=P["err"])
        self.pill_manual.configure(text="Ручной: " + ("ПАУЗА" if running else
                                                      ("ВКЛ" if self.shared.armed else "выкл")),
                                   bg=P["accent"] if self.shared.armed else P["card2"])
        if running:
            dry = self.pilot.cfg["dry_run"]
            self.pill_ai.configure(text="ИИ: " + ("ПРОБНЫЙ ПРОГОН" if dry else "РАБОТАЕТ"),
                                   bg=P["warn"] if dry else P["ok"])
        else:
            self.pill_ai.configure(text="ИИ: остановлен", bg=P["card2"])
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if running else ["disabled"])

        if st:
            self.v_state.set(("⚠ " + st["error"]) if st.get("error") else st["state_text"])
            self.v_score.set(str(st["score"]) if st["score"] is not None else "—")
            self.v_last.set(f"Последняя деталь: {st['last_result'] or '—'}")
            self.v_acc.set(f"Точность: {st['accuracy']:.0f}%   Идеал: {st['perfect_rate']:.0f}%"
                           f"   Всего: {st['total']}")
            total = max(1, st["total"])
            for key in ("perfect", "normal", "miss"):
                bar, var = self.bars[key]
                bar["value"] = st[key] * 100.0 / total
                var.set(str(st[key]))
            sv = self.state_vars
            sv["fps"].set(f"{st['fps']:.0f}")
            sv["tower"].set("найден" if st["tower"] else "не найден")
            sv["speed"].set(f"{st['speed']:.2f} шир/с")
            sv["offset"].set(f"{st['offset']:+.0f} мс")
            sv["fall"].set(f"{st['fall_ms']:.0f} мс" if st["fall_ms"] > 0 else "учится…")
            sv["drift"].set(f"{st['drift_k'] * 100:.0f}%")
            sv["types"].set(str(st["types"]))
            sv["learn"].set(str(st["learn_total"]))
            sv["explore"].set(f"{st['explore']:.1f}%")
            sv["ocr"].set(st["ocr"] if self.pilot.cfg["ocr_on"] else "выключено")

        item = self.pilot.take_preview()
        if item is not None:
            self._render_preview(item)

    def _fit(self, bgr, lbl, max_w, max_h):
        """Вписать картинку в рамку-карточку (размер рамки задаёт сетка, а не картинка)."""
        h, w = bgr.shape[:2]
        box = lbl.master
        bw, bh = box.winfo_width() - 32, box.winfo_height() - 44
        aw = bw if bw > 100 else max_w
        ah = bh if bh > 80 else max_h
        k = min(aw / w, ah / h)
        if abs(k - 1) > 0.02:
            bgr = cv2.resize(bgr, (max(1, int(w * k)), max(1, int(h * k))),
                             interpolation=cv2.INTER_NEAREST if k > 1 else cv2.INTER_AREA)
        return bgr

    def _show(self, key, lbl, bgr, max_w, max_h):
        img = self._fit(bgr, lbl, max_w, max_h)
        photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        self._photos[key] = photo
        lbl.configure(image=photo, text="", width=0, height=0)

    def _render_preview(self, item):
        vis, fg, mag, _moving = item
        tab = self.nb.select()
        if tab == str(self.ai_tab):
            self._show("mini", self.mini_lbl, vis, 760, 300)
        elif tab == str(self.prev_tab):
            self._show("p0", self.prev_lbls[0], vis, 620, 380)
            self._show("p1", self.prev_lbls[1], cv2.cvtColor(fg, cv2.COLOR_GRAY2BGR), 620, 380)
            heat = cv2.applyColorMap(np.clip(mag * 40, 0, 255).astype(np.uint8),
                                     cv2.COLORMAP_INFERNO)
            self._show("p2", self.prev_lbls[2], heat, 620, 380)

    # ------------------------------------------------------------ выход
    def on_close(self):
        self._closing = True
        try:
            self._push_ai()
            self._save_ai()
            self.pilot.shutdown()
        except Exception:
            traceback.print_exc()
        try:
            self.roi_overlay.destroy()
        except Exception:
            pass
        self.manual.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# =============================================================================
# 6. Запуск
# =============================================================================
def relaunch_as_admin():
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        script = str(Path(__file__).resolve())
        params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
        rc = shell32.ShellExecuteW(None, "runas", sys.executable, params,
                                   str(Path(script).parent), 1)
        return bool(rc and rc > 32)
    except Exception:
        return False


def main():
    if not cm.is_admin() and "--no-admin" not in sys.argv:
        tmp = tk.Tk()
        tmp.withdraw()
        answer = messagebox.askyesnocancel(
            APP_TITLE,
            "Программа запущена БЕЗ прав администратора.\n\n"
            "Драйверу Interception для нажатий обычно нужны права администратора.\n\n"
            "Да — перезапустить от имени администратора (рекомендуется)\n"
            "Нет — продолжить без прав\nОтмена — выйти", icon="warning", parent=tmp)
        tmp.destroy()
        if answer is None:
            return
        if answer and relaunch_as_admin():
            return
    try:
        MainWindow().run()
    except Exception as exc:
        traceback.print_exc()
        try:
            messagebox.showerror(APP_TITLE, f"Критическая ошибка:\n{exc}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
