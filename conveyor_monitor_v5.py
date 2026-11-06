# -*- coding: utf-8 -*-
r"""
КОНТРОЛЬ ДЕТАЛЕЙ НА КОНВЕЙЕРЕ  (версия 5: 12 линий, шкала сдвига, автосохранение)

Программа следит за областью экрана (или окна) и, когда цветная деталь заполняет
зону срабатывания на заданный процент, нажимает клавишу (по умолчанию Пробел).

Два режима работы (переключаются вкладками / переключателем справа):
  * «Зона»            — рамка / круг / ромб / полоса, процент заполнения цветом (HSV).
  * «Цикличные линии» — до 12 линий под любым углом, у каждой флажок «Активна». Цикл идёт
                        по ВКЛЮЧЁННЫМ линиям по порядку: изменение пикселей на текущей линии ->
                        клик -> сразу следующая включённая линия (выключенные пропускаются) ->
                        ... -> после последней снова первая.
  * Вкладка «Шкала»  — один слайдер сдвигает ВСЕ линии по оси Y (подстройка под подъём камеры).

Все настройки сохраняются в config.json каждую минуту и при выходе, а при запуске
загружаются автоматически.

Нажатие отправляется через драйвер Interception по скан-коду (Пробел = 0x39), то есть
на уровне драйвера клавиатуры. Для приложений, читающих Raw Input, такое нажатие
выглядит как нажатие на физической клавиатуре (в отличие от SendInput/keyboard).

Архитектура:
  * Главный поток  — интерфейс tkinter (настройки, предпросмотр, журнал).
  * Рабочий поток  — захват экрана -> HSV -> маска -> % заполнения -> нажатие
                     клавиши. Работает без пауз, на максимальной частоте кадров.

Способы захвата (выбираются в интерфейсе):
  1) mss    — весь экран, быстрый и универсальный.
  2) dxcam  — DirectX Desktop Duplication, максимальный FPS (основной монитор).
  3) окно   — WinAPI PrintWindow/BitBlt конкретного окна: работает, даже если
              окно перекрыто другими окнами или не в фокусе.

Установка библиотек:
    pip install opencv-python numpy mss pillow
    pip install dxcam            (необязательно, для режима DirectX)

Драйвер Interception (один раз):
    1) Скачайте релиз: https://github.com/oblitum/Interception
    2) В папке «command line installer» запустите от администратора:
           install-interception.exe /install
    3) Перезагрузите компьютер.
    4) Файл library\x64\interception.dll (для 64-битного Python) положите рядом
       с этим скриптом. Если установлен pip-пакет interception-python, программа
       попробует найти DLL и в нём.

Запуск (от имени администратора):
    python conveyor_monitor_v5.py
"""

import collections
import ctypes
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":
    print("Эта программа работает только в Windows.")
    sys.exit(1)

try:
    import cv2
    import numpy as np
    import mss
    from PIL import Image, ImageTk
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError as exc:
    print(f"Не хватает библиотеки: {exc}\n"
          "Установите: pip install opencv-python numpy mss pillow")
    sys.exit(1)

try:  # dxcam необязателен
    import dxcam
except Exception:
    dxcam = None


# =============================================================================
# DPI и WinAPI
# =============================================================================
def enable_dpi_awareness():
    """Чтобы координаты экрана совпадали с физическими пикселями."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# Типы аргументов обязательны: иначе 64-битные дескрипторы обрезаются до 32 бит.
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
try:  # SetWindowDisplayAffinity: исключение своих окон из захвата (Windows 10 2004+)
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    HAS_AFFINITY = True
except AttributeError:
    HAS_AFFINITY = False
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LINE_COORD_RE = re.compile(r"^l\d{1,2}[xy][12]$")   # ключи координат линий
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def list_windows():
    """Список видимых окон: [(hwnd, заголовок), ...]."""
    found = []

    def callback(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    found.append((int(hwnd), buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


# =============================================================================
# Настройки по умолчанию и константы
# =============================================================================
APP_TITLE = "Контроль деталей на конвейере"
SETTINGS_FILE = Path(__file__).resolve().with_name("config.json")
LEGACY_SETTINGS_FILE = Path(__file__).resolve().with_name("conveyor_settings.json")  # старое имя
AUTOSAVE_MS = 60_000          # автосохранение раз в минуту
SHIFT_LIMIT = 600             # предел шкалы сдвига, пикс
LINE_COUNT = 12               # число линий

# Цвета интерфейса. Главное окно v6 подменяет их на тёмную тему (THEME.update(...))
# и задаёт STYLE_HOOK — функцию, которая настраивает стили ttk.
THEME = {
    "dark": False,
    "text": "black", "muted": "#9ca3af", "accent": "#2563eb",
    "list_bg": "white", "hl_bg": "#bbf7d0", "hl_fg": "black",
    "bar_bg": "#f1f5f9", "bar_border": "#cbd5e1", "log_bg": "#f8fafc",
}
STYLE_HOOK = None

SHAPES = ["Прямоугольник", "Круг (эллипс)", "Ромб", "Вертикальная полоса"]
SHAPE_HINTS = [
    "вся выделенная область",
    "эллипс, вписанный в область",
    "ромб, вписанный в область",
    "узкая полоса по центру области (ширина — ползунком ниже)",
]
# Скан-коды (Set 1). Стрелки — "расширенные" клавиши (флаг E0).
KEY_SCANCODES = {
    "space": (0x39, False), "enter": (0x1C, False), "tab": (0x0F, False),
    "esc": (0x01, False),
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
    "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False), "f4": (0x3E, False),
    "f5": (0x3F, False), "f6": (0x40, False), "f7": (0x41, False), "f8": (0x42, False),
    "f9": (0x43, False), "f10": (0x44, False), "f11": (0x57, False), "f12": (0x58, False),
}
# Кнопки мыши (устройства Interception 11..20): (флаг нажатия, флаг отпускания)
MOUSE_BUTTONS = {"mouse_left": (0x001, 0x002), "mouse_right": (0x004, 0x008)}
KEYS = list(KEY_SCANCODES.keys()) + list(MOUSE_BUTTONS.keys())
VALID_KEYS = set(KEYS)


def make_num_specs():
    """Числовые настройки: ключ -> (минимум, максимум, значение по умолчанию)."""
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78) or sw
    vh = user32.GetSystemMetrics(79) or sh
    specs = {
        "x": (vx, vx + vw, max(vx, sw // 2 - 150)),
        "y": (vy, vy + vh, max(vy, sh // 2 - 100)),
        "w": (10, vw, 300),
        "h": (10, vh, 200),
        "step": (1, 50, 5),
        "strip": (5, 100, 30),
        "thr": (1, 100, 90),
        "hmin": (0, 179, 0),
        "hmax": (0, 179, 179),
        "smin": (0, 255, 80),
        "vmin": (0, 255, 50),
        "vmax": (0, 255, 255),
        "noise": (0, 5, 1),
        "hold": (0, 300, 40),
        "cooldown": (0, 5000, 300),
        "release": (10, 95, 60),
        # --- режим «Цикличные линии» ---
        "line_pct": (1, 100, 10),      # % изменившихся пикселей для срабатывания линии
        "line_delta": (1, 255, 25),    # минимальная разница яркости/насыщенности пикселя
        "line_thick": (1, 7, 1),       # толщина линии, пикс
        "line_ref": (0, 500, 0),       # сравнивать с кадром N мс назад (0 = предыдущий)
        "line_lock": (0, 2000, 100),   # пауза после срабатывания, мс
        "line_start": (1, 12, 1),      # с какой линии начинать цикл
    }
    # Координаты 12 линий: l<N>x1, l<N>y1 (начало), l<N>x2, l<N>y2 (конец).
    # Диапазоны с запасом, чтобы шкала сдвига была полностью обратимой.
    margin = 3000
    for i in range(1, 13):
        y = sh // 2 - 180 + 30 * (i - 1)
        specs[f"l{i}x1"] = (vx - margin, vx + vw + margin, max(vx, sw // 2 - 150))
        specs[f"l{i}y1"] = (vy - margin, vy + vh + margin, y)
        specs[f"l{i}x2"] = (vx - margin, vx + vw + margin, min(vx + vw, sw // 2 + 150))
        specs[f"l{i}y2"] = (vy - margin, vy + vh + margin, y)
    return specs


# =============================================================================
# Способы захвата экрана
# =============================================================================
class BackendError(Exception):
    """Понятная пользователю ошибка захвата."""


class MssBackend:
    """Захват экрана через mss (GDI BitBlt). Работает с любым монитором."""

    def __init__(self):
        self.sct = mss.mss()  # создаётся и используется в рабочем потоке

    def grab(self, x, y, w, h):
        shot = self.sct.grab({"left": int(x), "top": int(y),
                              "width": int(w), "height": int(h)})
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


_dx_camera = None  # одна камера dxcam на всё время работы программы


class DxcamBackend:
    """Захват через DirectX Desktop Duplication (dxcam). Основной монитор."""

    def __init__(self):
        global _dx_camera
        if dxcam is None:
            raise BackendError("Библиотека dxcam не установлена (pip install dxcam)")
        if _dx_camera is None:
            _dx_camera = dxcam.create(output_color="BGR")
        if _dx_camera is None:
            raise BackendError("Не удалось инициализировать dxcam")
        self.cam = _dx_camera
        self.sw = user32.GetSystemMetrics(0)
        self.sh = user32.GetSystemMetrics(1)

    def grab(self, x, y, w, h):
        left, top = max(0, x), max(0, y)
        right, bottom = min(self.sw, x + w), min(self.sh, y + h)
        if right - left < 2 or bottom - top < 2:
            raise BackendError("Область вне основного монитора "
                               "(dxcam работает только с основным монитором)")
        # None — нового кадра пока нет (экран не менялся)
        return self.cam.grab(region=(left, top, right, bottom))

    def close(self):
        pass


class WindowBackend:
    """Захват конкретного окна через PrintWindow/BitBlt (даже если оно перекрыто)."""

    def __init__(self, hwnd):
        if not hwnd or not user32.IsWindow(hwnd):
            raise BackendError("Окно не выбрано или закрыто (вкладка «Захват»)")
        self.hwnd = hwnd
        self.win_dc = user32.GetWindowDC(hwnd)
        if not self.win_dc:
            raise BackendError("Не удалось получить контекст окна")
        self.mem_dc = gdi32.CreateCompatibleDC(self.win_dc)
        self.bmp = None
        self.size = (0, 0)
        self.buf = None

    def _realloc(self, ww, wh):
        if self.bmp:
            gdi32.DeleteObject(self.bmp)
        self.bmp = gdi32.CreateCompatibleBitmap(self.win_dc, ww, wh)
        if not self.bmp:
            raise BackendError("Не удалось создать буфер изображения")
        self.buf = (ctypes.c_ubyte * (ww * wh * 4))()
        self.size = (ww, wh)

    def grab(self, x, y, w, h):
        hwnd = self.hwnd
        if not user32.IsWindow(hwnd):
            raise BackendError("Окно закрыто или недоступно")
        if user32.IsIconic(hwnd):
            raise BackendError("Окно свёрнуто — разверните его (свёрнутое окно не отрисовывается)")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        ww, wh = rect.right - rect.left, rect.bottom - rect.top
        if ww <= 0 or wh <= 0:
            raise BackendError("У окна нулевой размер")
        if (ww, wh) != self.size:
            self._realloc(ww, wh)

        old = gdi32.SelectObject(self.mem_dc, self.bmp)
        try:
            if not user32.PrintWindow(hwnd, self.mem_dc, PW_RENDERFULLCONTENT):
                # Запасной вариант: копирование из контекста окна
                gdi32.BitBlt(self.mem_dc, 0, 0, ww, wh, self.win_dc, 0, 0,
                             SRCCOPY | CAPTUREBLT)
        finally:
            gdi32.SelectObject(self.mem_dc, old)  # bitmap нельзя читать, пока он выбран

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = ww
        bmi.bmiHeader.biHeight = -wh  # отрицательное значение = строки сверху вниз
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        lines = gdi32.GetDIBits(self.mem_dc, self.bmp, 0, wh, self.buf,
                                ctypes.byref(bmi), 0)
        if lines == 0:
            raise BackendError("Не удалось прочитать изображение окна")

        full = np.frombuffer(self.buf, dtype=np.uint8).reshape(wh, ww, 4)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(ww, x + w), min(wh, y + h)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise BackendError("Область находится за пределами окна")
        return cv2.cvtColor(np.ascontiguousarray(full[y0:y1, x0:x1]), cv2.COLOR_BGRA2BGR)

    def close(self):
        try:
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            if self.mem_dc:
                gdi32.DeleteDC(self.mem_dc)
            if self.win_dc:
                user32.ReleaseDC(self.hwnd, self.win_dc)
        except Exception:
            pass


def make_backend(p):
    mode = p["mode"]
    if mode == "dxcam":
        return DxcamBackend()
    if mode == "window":
        return WindowBackend(p["hwnd"])
    return MssBackend()


# =============================================================================
# Обработка изображения
# =============================================================================
_mask_cache = {}
_kernel_cache = {}


def get_shape_mask(shape, w, h, strip_pct):
    """Маска формы зоны (255 внутри зоны). Кэшируется."""
    key = (shape, w, h, strip_pct)
    mask = _mask_cache.get(key)
    if mask is None:
        mask = np.zeros((h, w), np.uint8)
        if shape == 0:    # прямоугольник
            mask[:] = 255
        elif shape == 1:  # круг / эллипс
            cv2.ellipse(mask, (w // 2, h // 2), (max(w // 2, 1), max(h // 2, 1)),
                        0, 0, 360, 255, -1)
        elif shape == 2:  # ромб
            pts = np.array([[w // 2, 0], [w - 1, h // 2],
                            [w // 2, h - 1], [0, h // 2]], np.int32)
            cv2.fillPoly(mask, [pts], 255)
        else:             # вертикальная полоса по центру
            sw_ = max(1, int(w * strip_pct / 100))
            x0 = (w - sw_) // 2
            mask[:, x0:x0 + sw_] = 255
        if len(_mask_cache) > 32:
            _mask_cache.clear()
        _mask_cache[key] = mask
    return mask


def analyze(frame, shape_mask, p):
    """HSV -> inRange -> очистка шума -> процент заполнения зоны."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h_lo, h_hi = sorted((p["hmin"], p["hmax"]))
    v_lo, v_hi = sorted((p["vmin"], p["vmax"]))
    obj = cv2.inRange(hsv, np.array([h_lo, p["smin"], v_lo], np.uint8),
                      np.array([h_hi, 255, v_hi], np.uint8))
    n = p["noise"]
    if n > 0:
        k = 2 * n + 1
        kernel = _kernel_cache.get(k)
        if kernel is None:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            _kernel_cache[k] = kernel
        obj = cv2.morphologyEx(obj, cv2.MORPH_OPEN, kernel)
    in_zone = cv2.bitwise_and(obj, shape_mask)
    zone_px = cv2.countNonZero(shape_mask)
    fill = cv2.countNonZero(in_zone) * 100.0 / zone_px if zone_px else 0.0
    return fill, in_zone


# =============================================================================
# Ввод через драйвер Interception (эмуляция физической клавиатуры)
# =============================================================================
INTERCEPTION_KEY_DOWN = 0x00
INTERCEPTION_KEY_UP = 0x01
INTERCEPTION_KEY_E0 = 0x02
INTERCEPTION_FILTER_KEY_ALL = 0xFFFF
MAX_KEYBOARDS = 10   # устройства 1..10 — клавиатуры, 11..20 — мыши

MAX_MICE = 10        # устройства 11..20

_PREDICATE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)


class IMouseStroke(ctypes.Structure):
    # InterceptionMouseStroke: state, flags, rolling, x, y, information (20 байт)
    _fields_ = [("state", ctypes.c_ushort), ("flags", ctypes.c_ushort),
                ("rolling", ctypes.c_short), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("information", ctypes.c_uint)]


class InterceptionError(Exception):
    """Понятная пользователю ошибка драйвера Interception."""


class IStroke(ctypes.Structure):
    # Буфер размером с InterceptionStroke (20 байт). Для клавиатуры нужны первые 8 байт:
    # code (скан-код), state (0 = нажатие, 1 = отпускание, |2 = расширенная клавиша).
    _fields_ = [("code", ctypes.c_ushort), ("state", ctypes.c_ushort),
                ("information", ctypes.c_uint), ("_reserved", ctypes.c_ubyte * 12)]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Перезапускает скрипт с правами администратора (появится окно UAC)."""
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        script = str(Path(__file__).resolve())
        params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
        rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return bool(rc and rc > 32)
    except Exception:
        return False


def load_interception_dll():
    """Ищет и загружает interception.dll. Возвращает (библиотека, путь)."""
    arch = "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86"
    base = Path(__file__).resolve().parent
    candidates = []
    env = os.environ.get("INTERCEPTION_DLL")
    if env:
        candidates.append(env)
    for rel in ("interception.dll", f"library/{arch}/interception.dll",
                f"{arch}/interception.dll", f"Interception/library/{arch}/interception.dll"):
        path = base / rel
        if path.is_file():
            candidates.append(str(path))
    try:  # DLL внутри установленного pip-пакета interception-python
        spec = importlib.util.find_spec("interception")
        if spec is not None:
            for loc in (spec.submodule_search_locations or []):
                found = sorted(Path(loc).rglob("interception*.dll"),
                               key=lambda q: (arch not in str(q).lower(), str(q)))
                candidates.extend(str(q) for q in found)
    except Exception:
        pass
    candidates.append("interception.dll")  # системные пути / PATH

    for cand in candidates:
        try:
            return ctypes.CDLL(cand), cand
        except OSError:
            continue
    raise InterceptionError(
        "Не найден interception.dll. Скачайте Interception (github.com/oblitum/Interception, "
        f"раздел Releases) и положите library\\{arch}\\interception.dll рядом со скриптом.")


class InterceptionKeySender:
    """Нажатие клавиш через драйвер Interception.

    Работает в отдельном потоке: рабочий поток только ставит запрос в очередь, а
    удержание клавиши (пауза между нажатием и отпусканием) не тормозит захват кадров.
    """

    def __init__(self, shared):
        self.shared = shared
        self.lib = None
        self.ctx = None
        self.dll_path = ""
        self.available = False
        self.can_detect = False
        self.error = ""
        self.devices = []        # [(номер устройства 1..10, hardware id)]
        self.selected = 0        # 0 = автоопределение
        self.active_dev = 0      # последняя клавиатура, на которой нажатие прошло
        self.active_mouse = 0    # последняя мышь, на которой клик прошёл
        self._mice = None
        self.sent = 0
        self._io_lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._pending = None
        self._evt = threading.Event()
        self._stop = threading.Event()
        self._detect_thread = None
        self._detect_result = None
        self._open()
        if self.available:
            self.refresh_devices()
            self._thread = threading.Thread(target=self._loop, name="KeySender", daemon=True)
            self._thread.start()

    # ---------------------------------------------------------------- драйвер
    def _open(self):
        try:
            lib, path = load_interception_dll()
            c = ctypes
            lib.interception_create_context.argtypes = []
            lib.interception_create_context.restype = c.c_void_p
            lib.interception_destroy_context.argtypes = [c.c_void_p]
            lib.interception_destroy_context.restype = None
            lib.interception_set_filter.argtypes = [c.c_void_p, _PREDICATE, c.c_ushort]
            lib.interception_set_filter.restype = None
            lib.interception_send.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_uint]
            lib.interception_send.restype = c.c_int
            lib.interception_receive.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_uint]
            lib.interception_receive.restype = c.c_int
            lib.interception_get_hardware_id.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_uint]
            lib.interception_get_hardware_id.restype = c.c_uint
            if hasattr(lib, "interception_wait_with_timeout"):
                lib.interception_wait_with_timeout.argtypes = [c.c_void_p, c.c_ulong]
                lib.interception_wait_with_timeout.restype = c.c_int
                self.can_detect = True
            ctx = lib.interception_create_context()
            if not ctx:
                raise InterceptionError(
                    "Не удалось открыть драйвер Interception. Установите драйвер: запустите "
                    "install-interception.exe /install от администратора и перезагрузите "
                    "компьютер. Если драйвер уже установлен — запустите программу от "
                    "имени администратора.")
            self.lib, self.ctx, self.dll_path = lib, ctx, path
            self.available = True
        except InterceptionError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"Ошибка инициализации Interception: {exc}"

    def refresh_devices(self):
        """Определяет подключённые клавиатуры по их hardware id."""
        found = []
        if self.available and self.ctx:
            buf = ctypes.create_string_buffer(512)
            for dev in range(1, MAX_KEYBOARDS + 1):
                try:
                    n = int(self.lib.interception_get_hardware_id(self.ctx, dev, buf, 512))
                except Exception:
                    n = 0
                if 0 < n <= 512:
                    hw = buf.raw[:n].decode("utf-16-le", errors="ignore").split("\x00")[0].strip()
                    found.append((dev, hw))
        self.devices = found
        return found

    def status_text(self):
        admin = "да" if is_admin() else "НЕТ"
        if not self.available:
            return f"Interception недоступен: {self.error}"
        if self.devices:
            names = ", ".join(str(d) for d, _ in self.devices)
            return (f"Драйвер Interception работает. Найдено клавиатур: {len(self.devices)} "
                    f"(устройства: {names}). Права администратора: {admin}.")
        return ("Драйвер работает, но клавиатуры не определились автоматически. Нажмите "
                f"«Определить нажатием…». Права администратора: {admin}.")

    def _candidates(self):
        """Порядок попыток: выбранная -> последняя рабочая -> остальные найденные."""
        ids = [d for d, _ in self.devices]
        order = []
        if self.selected in ids:
            order.append(self.selected)
        elif self.active_dev in ids:
            order.append(self.active_dev)
        order += [d for d in ids if d not in order]
        return order or list(range(1, MAX_KEYBOARDS + 1))

    def _send(self, dev, code, state):
        if not self.ctx:
            return False
        stroke = IStroke()
        stroke.code, stroke.state, stroke.information = code, state, 0
        try:
            with self._io_lock:
                return self.lib.interception_send(self.ctx, dev, ctypes.byref(stroke), 1) > 0
        except Exception:
            return False

    # ---------------------------------------------------------------- нажатие
    def _mouse_ids(self):
        """Номера устройств-мышей (11..20), у которых есть hardware id."""
        if self._mice is None:
            found = []
            buf = ctypes.create_string_buffer(512)
            for dev in range(MAX_KEYBOARDS + 1, MAX_KEYBOARDS + MAX_MICE + 1):
                try:
                    n = int(self.lib.interception_get_hardware_id(self.ctx, dev, buf, 512))
                except Exception:
                    n = 0
                if 0 < n <= 512:
                    found.append(dev)
            self._mice = found or list(range(MAX_KEYBOARDS + 1, MAX_KEYBOARDS + MAX_MICE + 1))
        return self._mice

    def _send_mouse(self, dev, state):
        stroke = IMouseStroke()
        stroke.state, stroke.flags, stroke.rolling = state, 0, 0   # flags 0: без движения
        stroke.x = stroke.y = 0
        stroke.information = 0
        try:
            with self._io_lock:
                return self.lib.interception_send(self.ctx, dev, ctypes.byref(stroke), 1) > 0
        except Exception:
            return False

    def click_sync(self, button, hold_ms, t_origin=None):
        """Клик кнопкой мыши через Interception (в текущей точке курсора)."""
        down, up = MOUSE_BUTTONS[button]
        order = ([self.active_mouse] if self.active_mouse else []) + \
            [d for d in self._mouse_ids() if d != self.active_mouse]
        for dev in order:
            if not self._send_mouse(dev, down):
                continue
            latency = (time.perf_counter() - t_origin) * 1000.0 if t_origin else 0.0
            try:
                if hold_ms > 0:
                    time.sleep(hold_ms / 1000.0)
            finally:
                for _ in range(3):
                    if self._send_mouse(dev, up):
                        break
            self.active_mouse = dev
            return True, dev, latency, ""
        return False, 0, 0.0, "драйвер не принял клик ни на одной мыши"

    def press_sync(self, key_name, hold_ms, t_origin=None):
        """Нажать и отпустить клавишу. Возвращает (успех, устройство, задержка_мс, ошибка)."""
        if not self.available:
            return False, 0, 0.0, self.error
        if key_name in MOUSE_BUTTONS:
            return self.click_sync(key_name, hold_ms, t_origin)
        code, extended = KEY_SCANCODES.get(key_name, KEY_SCANCODES["space"])
        flag = INTERCEPTION_KEY_E0 if extended else 0
        down, up = INTERCEPTION_KEY_DOWN | flag, INTERCEPTION_KEY_UP | flag
        for dev in self._candidates():
            if not self._send(dev, code, down):
                continue
            latency = (time.perf_counter() - t_origin) * 1000.0 if t_origin else 0.0
            try:
                if hold_ms > 0:
                    time.sleep(hold_ms / 1000.0)
            finally:
                for _ in range(3):          # отпускание обязательно, иначе клавиша "залипнет"
                    if self._send(dev, code, up):
                        break
            self.active_dev = dev
            return True, dev, latency, ""
        return False, 0, 0.0, ("драйвер не принял нажатие ни на одной клавиатуре "
                               "(нажмите «Определить нажатием…» или запустите от администратора)")

    def request(self, key_name, hold_ms, t_origin, fill, note=None):
        """Неблокирующий запрос нажатия (вызывается из рабочего потока)."""
        if not self.available:
            return False
        with self._req_lock:
            if self._pending is not None:      # предыдущее нажатие ещё выполняется
                return False
            self._pending = (key_name, hold_ms, t_origin, fill, note)
        self._evt.set()
        return True

    def _loop(self):
        while not self._stop.is_set():
            self._evt.wait()
            self._evt.clear()
            if self._stop.is_set():
                break
            with self._req_lock:
                req = self._pending
            if req is None:
                continue
            key_name, hold_ms, t_origin, fill, note = req
            try:
                ok, dev, lat, err = self.press_sync(key_name, hold_ms, t_origin)
            except Exception as exc:
                ok, dev, lat, err = False, 0, 0.0, str(exc)
            if ok:
                self.sent += 1
                self.shared.set_stats(sent=self.sent, latency=lat)
                what = note or f"заполнение {fill:.1f}%"
                kind = "мышь" if key_name in MOUSE_BUTTONS else "клавиатура"
                self.shared.log(f"СИГНАЛ «{key_name}» отправлен через Interception "
                                f"({kind} {dev}): {what}, "
                                f"задержка {lat:.1f} мс (№{self.sent})")
            else:
                self.shared.log(f"Ошибка отправки «{key_name}»: {err}")
            with self._req_lock:
                self._pending = None

    # --------------------------------------------- определение клавиатуры
    def start_detect(self, timeout_ms=6000):
        """Ждёт реального нажатия на клавиатуре и запоминает, с какого устройства оно пришло."""
        if not (self.available and self.can_detect):
            return False
        if self._detect_thread is not None and self._detect_thread.is_alive():
            return False
        self._detect_result = None
        self._detect_thread = threading.Thread(target=self._detect_worker,
                                               args=(timeout_ms,), daemon=True)
        self._detect_thread.start()
        return True

    def pop_detect(self):
        """None — результата ещё нет; 0 — клавиша не нажата; >0 — номер устройства."""
        result = self._detect_result
        self._detect_result = None
        return result

    def _detect_worker(self, timeout_ms):
        lib = self.lib
        ctx = None
        found = 0
        try:
            ctx = lib.interception_create_context()
            if not ctx:
                raise InterceptionError("не удалось создать контекст")
            is_kbd = _PREDICATE(lambda d: 1 if 1 <= d <= MAX_KEYBOARDS else 0)
            lib.interception_set_filter(ctx, is_kbd, INTERCEPTION_FILTER_KEY_ALL)
            stroke = IStroke()
            dev = lib.interception_wait_with_timeout(ctx, timeout_ms)
            if 1 <= dev <= MAX_KEYBOARDS:
                if lib.interception_receive(ctx, dev, ctypes.byref(stroke), 1) > 0:
                    found = dev
                    # Перехваченную клавишу сразу возвращаем системе, иначе она "пропадёт"
                    lib.interception_send(ctx, dev, ctypes.byref(stroke), 1)
            lib.interception_set_filter(ctx, is_kbd, 0)   # больше ничего не перехватываем
            # Возвращаем системе всё, что успело перехватиться (например, отпускание клавиши)
            for _ in range(20):
                d = lib.interception_wait_with_timeout(ctx, 150)
                if not (1 <= d <= MAX_KEYBOARDS):
                    break
                if lib.interception_receive(ctx, d, ctypes.byref(stroke), 1) > 0:
                    lib.interception_send(ctx, d, ctypes.byref(stroke), 1)
        except Exception as exc:
            self.shared.log(f"Ошибка определения клавиатуры: {exc}")
            found = 0
        finally:
            if ctx:
                try:
                    lib.interception_destroy_context(ctx)
                except Exception:
                    pass
        self._detect_result = found

    def close(self):
        self._stop.set()
        self._evt.set()
        with self._io_lock:
            if self.ctx:
                try:
                    self.lib.interception_destroy_context(self.ctx)
                except Exception:
                    pass
                self.ctx = None
            self.available = False


# =============================================================================
# Общее состояние между потоками
# =============================================================================
class Shared:
    """Потокобезопасный обмен данными между интерфейсом и рабочим потоком."""

    def __init__(self, params):
        self._lock = threading.Lock()
        self._params = dict(params)
        self.armed = False
        self.sender = None   # InterceptionKeySender (задаётся при запуске)
        self.stop = threading.Event()
        self.log_q = queue.Queue()
        self.reset_cycle = threading.Event()   # запрос "начать цикл линий заново"
        self.paused = threading.Event()        # ручной режим приостановлен (работает ИИ)
        self._stats = {"fill": 0.0, "fps": 0.0, "triggered": False, "sent": 0,
                       "error": "", "black": False, "latency": 0.0,
                       "work": "zone", "active": 0, "pcts": [0.0] * 12, "cycles": 0,
                       "fired_at": 0.0, "fired_line": -1, "step": -1, "seq_len": 0}
        self._preview = None
        self._preview_seq = 0
        self._preview_taken = 0

    # параметры: словарь только заменяется целиком и никогда не меняется на месте
    def set_params(self, p):
        with self._lock:
            self._params = dict(p)

    def get_params(self):
        with self._lock:
            return self._params

    def set_stats(self, **kw):
        with self._lock:
            self._stats.update(kw)

    def get_stats(self):
        with self._lock:
            return dict(self._stats)

    def set_preview(self, item):
        with self._lock:
            self._preview = item
            self._preview_seq += 1

    def take_preview(self):
        with self._lock:
            if self._preview_seq != self._preview_taken:
                self._preview_taken = self._preview_seq
                return self._preview
        return None

    def log(self, msg):
        self.log_q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def set_armed(self, value, source=""):
        self.armed = bool(value)
        self.log(f"Отправка сигнала {'ВКЛЮЧЕНА' if self.armed else 'ВЫКЛЮЧЕНА'}"
                 f"{' (' + source + ')' if source else ''}")

    def toggle_armed(self, source=""):
        self.set_armed(not self.armed, source)


# =============================================================================
# Рабочий поток: захват -> анализ -> нажатие
# =============================================================================
# ---------------------------------------------------------------------------
# Режим «Цикличные линии»: пиксели вдоль линий и поиск изменений
# ---------------------------------------------------------------------------
LINE_COLORS_HEX = ["#ff3b30", "#34c759", "#0a84ff", "#ffcc00", "#bf5af2", "#ff9f0a",
                   "#32d4de", "#ff6482", "#a2845e", "#5e5ce6", "#8e8e93", "#d1f20b"]
LINE_COLORS_BGR = [tuple(int(h[k:k + 2], 16) for k in (5, 3, 1)) for h in LINE_COLORS_HEX]
_line_cache = {}


SEQ_MAX_STEPS = 200    # максимум шагов в своей очереди


def seq_mode(p):
    """Работаем ли по своей очереди (вкладка «Очерёдность»)."""
    return bool(p.get("seq_on")) and bool(p.get("seq"))


def parse_sequence(text):
    """«1-2-3, 8 6» -> [0, 1, 2, 7, 5] (номера линий с нуля). Числа вне 1..12 отбрасываются."""
    out = [int(n) - 1 for n in re.findall(r"\d+", str(text)) if 1 <= int(n) <= LINE_COUNT]
    return out[:SEQ_MAX_STEPS]


def format_sequence(seq):
    return "-".join(str(i + 1) for i in seq)


def seq_loop_index(p):
    """С какого шага (с нуля) продолжать после последнего."""
    n = len(p["seq"])
    return min(max(int(p.get("seq_loop", 1)) - 1, 0), n - 1)


def seq_next(seq, act, pos, loop0):
    """Следующий шаг очереди после pos, линия которого включена.
    После последнего шага — переход к шагу loop0. Возвращает (шаг, был_ли_переход_в_начало)."""
    wrapped = False
    for _ in range(len(seq) + 1):
        pos += 1
        if pos >= len(seq):
            pos, wrapped = loop0, True
        if seq[pos] in act:
            return pos, wrapped
    # В повторяемой части нет ни одной включённой линии — первый подходящий шаг с начала
    for k, line in enumerate(seq):
        if line in act:
            return k, True
    return 0, True


def seq_preview(seq, loop0, enabled, count=24):
    """Как пойдёт цикл: первые count шагов (выключенные линии пропускаются)."""
    act = [i for i in set(seq) if enabled[i]]
    if not seq or not act:
        return []
    pos = 0 if seq[0] in act else seq_next(seq, act, 0, loop0)[0]
    out = []
    for _ in range(count):
        out.append(seq[pos])
        pos = seq_next(seq, act, pos, loop0)[0]
    return out


def active_lines(p):
    """Номера (0..11) линий, которые участвуют в цикле: включённые, а в режиме своей
    очереди — включённые и встречающиеся в очереди."""
    on = [i for i in range(LINE_COUNT) if p["line_on"][i]]
    if seq_mode(p):
        used = set(p["seq"])
        return [i for i in on if i in used]
    return on


def line_geometry(p):
    """Охватывающий прямоугольник включённых линий (его захватываем) и координаты линий
    внутри него: {номер_линии: (x1, y1, x2, y2)}."""
    act = active_lines(p)
    if not act:
        if seq_mode(p):
            raise BackendError("В очереди (вкладка «Очерёдность») нет ни одной включённой "
                               "линии — отметьте «Активна» у линий из очереди")
        raise BackendError("Нет включённых линий — отметьте «Активна» хотя бы у одной линии "
                           "(вкладка «Линии»)")
    pad = p["line_thick"] + 2
    pts = {i: (p[f"l{i + 1}x1"], p[f"l{i + 1}y1"], p[f"l{i + 1}x2"], p[f"l{i + 1}y2"])
           for i in act}
    xs = [v for (x1, _y1, x2, _y2) in pts.values() for v in (x1, x2)]
    ys = [v for (_x1, y1, _x2, y2) in pts.values() for v in (y1, y2)]
    bx, by = min(xs) - pad, min(ys) - pad
    bw, bh = max(xs) - min(xs) + 1 + 2 * pad, max(ys) - min(ys) + 1 + 2 * pad
    local = {i: (x1 - bx, y1 - by, x2 - bx, y2 - by) for i, (x1, y1, x2, y2) in pts.items()}
    return (bx, by, bw, bh), local


def line_pixels(line, thick, w, h):
    """Индексы пикселей строго вдоль линии (cv2.line = алгоритм Брезенхэма),
    упорядоченные от начала линии к концу."""
    key = (line, thick, w, h)
    got = _line_cache.get(key)
    if got is None:
        x1, y1, x2, y2 = line
        mask = np.zeros((h, w), np.uint8)
        cv2.line(mask, (x1, y1), (x2, y2), 255, thick, cv2.LINE_8)
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            raise BackendError("Линия выходит за пределы кадра")
        order = np.argsort((xs - x1) * (x2 - x1) + (ys - y1) * (y2 - y1), kind="stable")
        got = (ys[order], xs[order])
        if len(_line_cache) > 128:
            _line_cache.clear()
        _line_cache[key] = got
    return got


def changed_pixels(cur, ref, delta, use_hue):
    """Какие пиксели изменились: |ΔS| или |ΔV| >= delta (и, по желанию, смена оттенка).
    cur, ref — массивы HSV формы (N, 3)."""
    d = np.abs(cur.astype(np.int16) - ref.astype(np.int16))
    ch = (d[:, 1] >= delta) | (d[:, 2] >= delta)
    if use_hue:
        dh = np.minimum(d[:, 0], 180 - d[:, 0])
        valid = (cur[:, 1] > 40) & (cur[:, 2] > 40) & (ref[:, 1] > 40) & (ref[:, 2] > 40)
        ch = ch | (valid & (dh >= max(4, delta // 2)))
    return ch


def diff_map(cur, prev):
    """Карта изменений между кадрами для предпросмотра (ярче = сильнее изменение)."""
    if prev is None or prev.shape != cur.shape:
        return np.zeros(cur.shape[:2], np.uint8)
    d = cv2.absdiff(cv2.cvtColor(cur, cv2.COLOR_BGR2HSV), cv2.cvtColor(prev, cv2.COLOR_BGR2HSV))
    return cv2.convertScaleAbs(np.maximum(d[:, :, 1], d[:, :, 2]), alpha=4.0)


def next_active(act, cur):
    """Следующая ВКЛЮЧЁННАЯ линия после cur (по кругу). Выключенные пропускаются."""
    for step in range(1, LINE_COUNT + 1):
        j = (cur + step) % LINE_COUNT
        if j in act:
            return j
    return cur


def first_active_from(act, cur):
    """cur, если она включена; иначе ближайшая включённая после неё."""
    for step in range(LINE_COUNT):
        j = (cur + step) % LINE_COUNT
        if j in act:
            return j
    return cur


class LineEngine:
    """Строгий цикл по включённым линиям: активна одна линия; сработала -> клик ->
    активной становится следующая включённая."""

    def __init__(self):
        self.key = None
        self.idx = {}
        self.history = collections.deque(maxlen=4000)   # (время, {линия: HSV пикселей})
        self.reset(0)

    def reset(self, start):
        self.active = max(0, min(LINE_COUNT - 1, int(start)))
        self.step = 0          # номер шага своей очереди (если она включена)
        self.cycles = 0
        self.lock_until = 0.0
        self.fired_at = 0.0
        self.fired_line = -1
        self.history.clear()   # после сброса сравниваем только со свежими кадрами

    def _ref(self, now, lag):
        if not self.history:
            return None
        if lag <= 0:
            return self.history[-1][1]          # предыдущий кадр
        for t, hsv in reversed(self.history):
            if now - t >= lag:
                return hsv
        return None                             # нужной глубины истории ещё нет

    def process(self, frame, bbox, local, p, now, armed, send):
        """Обрабатывает кадр. send(индекс_линии, процент) -> True, если клик отправлен."""
        act = sorted(local)
        thick = p["line_thick"]
        bw, bh = bbox[2], bbox[3]
        key = (bbox, tuple(sorted(local.items())), thick)
        if key != self.key:                     # линии изменили/сдвинули — история не годится
            self.key = key
            self.idx = {i: line_pixels(local[i], thick, bw, bh) for i in act}
            self.history.clear()
        seq = p["seq"] if seq_mode(p) else None
        if seq:
            loop0 = seq_loop_index(p)
            if self.step >= len(seq):           # очередь укоротили на ходу
                self.step = 0
            if seq[self.step] not in local:     # линию шага выключили -> следующий шаг
                self.step = seq_next(seq, act, self.step, loop0)[0]
            self.active = seq[self.step]
        elif self.active not in local:          # текущую линию выключили -> следующая включённая
            self.active = first_active_from(act, self.active)

        hsv_map, pix_map = {}, {}
        for i in act:
            ys, xs = self.idx[i]
            pix = frame[ys, xs]                 # пиксели строго вдоль линии
            pix_map[i] = pix
            hsv_map[i] = cv2.cvtColor(pix.reshape(1, -1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)

        ref = self._ref(now, p["line_ref"] / 1000.0)
        pcts = [0.0] * LINE_COUNT
        changed = {}
        if ref is not None:
            for i in act:
                ch = changed_pixels(hsv_map[i], ref[i], p["line_delta"], p["line_hue"])
                changed[i] = ch
                pcts[i] = float(ch.mean()) * 100.0
        self.history.append((now, hsv_map))

        cur = self.active
        fired = -1
        # Проверяется ТОЛЬКО текущая линия; остальные ждут своей очереди
        if ref is not None and now >= self.lock_until and pcts[cur] >= p["line_pct"]:
            if p["line_dry"] or armed:
                ok = True if p["line_dry"] else send(cur, pcts[cur])
                if ok:
                    fired = cur
                    if seq:                             # своя очередь: следующий шаг
                        self.step, wrapped = seq_next(seq, act, self.step, loop0)
                        if wrapped:
                            self.cycles += 1
                        self.active = seq[self.step]
                    else:
                        nxt = next_active(act, cur)     # СРАЗУ на следующую включённую
                        if nxt <= cur:
                            self.cycles += 1            # прошли круг
                        self.active = nxt
                    self.lock_until = now + p["line_lock"] / 1000.0
                    self.fired_at, self.fired_line = now, cur
        return {"act": act, "lines": local, "active": self.active, "fired": fired,
                "step": self.step if seq else -1, "seq_len": len(seq) if seq else 0,
                "pcts": pcts, "thr": p["line_pct"],
                "profiles": {i: (pix_map[i], changed.get(i)) for i in act}}


# ---------------------------------------------------------------------------
# Рабочий поток
# ---------------------------------------------------------------------------
class Worker(threading.Thread):
    def __init__(self, shared):
        super().__init__(name="Worker", daemon=True)
        self.sh = shared
        self.lines = LineEngine()
        self._last_fail_log = 0.0

    def _send(self, p, t_grab, fill, note=None):
        """Запрос нажатия у потока-отправителя (Interception). True — запрос принят."""
        sh = self.sh
        sender = sh.sender
        if sender is not None and sender.request(p["key"], p["hold"], t_grab, fill, note):
            return True
        now = time.perf_counter()
        if now - self._last_fail_log > 1.0:     # не засорять журнал на каждом кадре
            self._last_fail_log = now
            why = (sender.error if sender is not None else "") or \
                  "предыдущее нажатие ещё выполняется"
            sh.log(f"Сигнал не отправлен: {why}")
        return False

    def run(self):
        sh = self.sh
        backend, backend_key = None, None
        triggered = False
        last_send = 0.0
        frames = 0
        t_fps = time.perf_counter()
        fps = 0.0
        last_preview = 0.0
        last_err = ""
        prev_armed = False
        prev_work = None
        prev_frame = None

        while not sh.stop.is_set():
            if sh.paused.is_set():              # экран сейчас анализирует ИИ-автопилот
                if backend is not None:
                    backend.close()
                    backend, backend_key = None, None
                sh.set_stats(fps=0.0, error="", triggered=False)
                sh.stop.wait(0.05)
                continue
            p = sh.get_params()
            try:
                work = p["work"]
                key = (p["mode"], p["hwnd"] if p["mode"] == "window" else 0)
                if key != backend_key or backend is None:
                    if backend is not None:
                        backend.close()
                        backend = None
                    backend_key = None
                    backend = make_backend(p)
                    backend_key = key
                    sh.log(f"Способ захвата: {p['mode']}")

                # Смена режима, включение отправки или кнопка сброса: цикл начинается заново
                armed = sh.armed
                if work != prev_work or (armed and not prev_armed) or sh.reset_cycle.is_set():
                    sh.reset_cycle.clear()
                    self.lines.reset(p["line_start"] - 1)
                    triggered = False
                    prev_frame = None
                    if work == "lines" and seq_mode(p):
                        sh.log(f"Цикл по своей очереди начат с шага 1: "
                               f"{format_sequence(p['seq'])}, после последнего шага — "
                               f"к шагу {seq_loop_index(p) + 1}.")
                    elif work == "lines":
                        sh.log(f"Цикл линий начат с линии {p['line_start']} "
                               f"(если она выключена — с ближайшей включённой).")
                prev_work, prev_armed = work, armed

                t_grab = time.perf_counter()
                if work == "lines":
                    bbox, local = line_geometry(p)
                    frame = backend.grab(*bbox)
                else:
                    frame = backend.grab(p["x"], p["y"], p["w"], p["h"])
                if frame is None:               # новых кадров пока нет (dxcam)
                    sh.stop.wait(0.001)
                    continue
                if frame.size == 0:
                    raise BackendError("Получен пустой кадр")
                fh, fw = frame.shape[:2]
                black = not frame[::3, ::3].any()
                now = time.perf_counter()

                if work == "lines":
                    # ------------------ режим «Цикличные линии» ------------------
                    if (fw, fh) != (bbox[2], bbox[3]):
                        raise BackendError("Линии выходят за пределы экрана или окна — "
                                           "проверьте координаты")
                    cycles_before = self.lines.cycles
                    info = self.lines.process(
                        frame, bbox, local, p, now, armed,
                        lambda i, pct: self._send(
                            p, t_grab, pct, f"линия {i + 1}: изменилось {pct:.1f}% пикселей"))
                    step_txt = (f" (шаг {info['step'] + 1}/{info['seq_len']})"
                                if info["step"] >= 0 else "")
                    if info["fired"] >= 0:
                        sh.log(f"Линия {info['fired'] + 1} сработала "
                               f"({info['pcts'][info['fired']]:.1f}% пикселей) → клик → "
                               f"ждём линию {info['active'] + 1}{step_txt}"
                               f"{' [пробный прогон]' if p['line_dry'] else ''}")
                        if self.lines.cycles > cycles_before:
                            sh.log(f"Круг №{self.lines.cycles} завершён, снова линия "
                                   f"{info['active'] + 1}{step_txt}.")
                    stats = dict(work="lines", fill=info["pcts"][info["active"]],
                                 step=info["step"], seq_len=info["seq_len"],
                                 triggered=False, active=info["active"], pcts=info["pcts"],
                                 cycles=self.lines.cycles, fired_at=self.lines.fired_at,
                                 fired_line=self.lines.fired_line)
                    make_preview = lambda: ("lines", frame.copy(),
                                            diff_map(frame, prev_frame), info)
                else:
                    # ------------------------ режим «Зона» ------------------------
                    shape_mask = get_shape_mask(p["shape"], fw, fh, p["strip"])
                    fill, in_zone = analyze(frame, shape_mask, p)
                    thr = p["thr"]
                    if triggered:
                        if fill < thr * p["release"] / 100.0:   # деталь ушла
                            triggered = False
                    elif fill >= thr:
                        triggered = True
                        if armed and (now - last_send) * 1000.0 >= p["cooldown"]:
                            if self._send(p, t_grab, fill):
                                last_send = now
                    stats = dict(work="zone", fill=fill, triggered=triggered)
                    make_preview = lambda: ("zone", frame.copy(), in_zone, shape_mask)

                # ---- Статистика и предпросмотр ----
                frames += 1
                if now - t_fps >= 0.5:
                    fps = frames / (now - t_fps)
                    frames = 0
                    t_fps = now
                sh.set_stats(fps=fps, error="", black=black, **stats)
                last_err = ""
                if now - last_preview >= 0.04:          # предпросмотр ~25 раз/с
                    last_preview = now
                    sh.set_preview(make_preview())
                prev_frame = frame if work == "lines" else None

            except Exception as exc:  # любая ошибка не должна останавливать программу
                msg = str(exc) if isinstance(exc, BackendError) else f"{type(exc).__name__}: {exc}"
                if msg != last_err:
                    sh.log(f"Ошибка захвата: {msg}")
                    last_err = msg
                sh.set_stats(error=msg, fps=0.0, triggered=False)
                triggered = False
                prev_frame = None
                self.lines.history.clear()
                if backend is not None:
                    backend.close()
                backend, backend_key = None, None
                sh.stop.wait(0.1)   # пауза только при ошибке, чтобы не забивать журнал

        if backend is not None:
            backend.close()


# =============================================================================
# Красная рамка области на экране (рисуется СНАРУЖИ области, не попадает в захват)
# =============================================================================
class RoiOverlay:
    THICK = 3

    def __init__(self, root):
        self.wins = []
        self.visible = False
        self.last = None
        for _ in range(4):
            w = tk.Toplevel(root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            w.configure(bg="#ff2d2d")
            w.withdraw()
            self.wins.append(w)
        self._no_activate()

    def _no_activate(self):
        """Рамка не должна забирать фокус у целевого приложения."""
        for w in self.wins:
            try:
                w.update_idletasks()
                hwnd = user32.GetAncestor(w.winfo_id(), GA_ROOT) or w.winfo_id()
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                      style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            except Exception:
                pass

    def show(self, x, y, w, h):
        t = self.THICK
        geoms = [(w + 2 * t, t, x - t, y - t), (w + 2 * t, t, x - t, y + h),
                 (t, h, x - t, y), (t, h, x + w, y)]
        if geoms == self.last and self.visible:
            return
        for win, (gw, gh, gx, gy) in zip(self.wins, geoms):
            win.geometry(f"{gw}x{gh}{gx:+d}{gy:+d}")
            win.deiconify()
        self.last, self.visible = geoms, True

    def hide(self):
        if self.visible:
            for win in self.wins:
                win.withdraw()
            self.visible = False
            self.last = None

    def destroy(self):
        for win in self.wins:
            try:
                win.destroy()
            except Exception:
                pass


# =============================================================================
# Выбор точек линий мышью (полноэкранная накладка)
# =============================================================================
class PointPicker:
    """Полупрозрачная накладка на весь экран: два клика мышью = одна линия."""

    def __init__(self, app, indices):
        self.app = app
        self.queue = list(indices)
        self.first = None          # (x_экрана, y_экрана, x_холста, y_холста) первой точки
        self.rubber = None
        self.done = False
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0)
        vh = user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1)
        win = tk.Toplevel(app.root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.5)
        except tk.TclError:
            pass
        win.configure(bg="black")
        win.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        c = tk.Canvas(win, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        self.canvas = c
        self.hint_bg = c.create_rectangle(0, 0, 0, 0, fill="#111111", outline="#ffcc00")
        self.hint = c.create_text(24, 24, anchor="nw", fill="#ffffff",
                                  font=("Segoe UI", 14, "bold"), text="")
        self.pos = c.create_text(0, 0, anchor="nw", fill="#ffff00",
                                 font=("Consolas", 10), text="")
        c.bind("<Button-1>", self._click)
        c.bind("<Motion>", self._motion)
        c.bind("<Button-3>", lambda e: self.finish(True))
        win.bind("<Escape>", lambda e: self.finish(True))
        c.bind("<Escape>", lambda e: self.finish(True))
        win.update_idletasks()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        c.focus_set()
        self._update_hint()

    def _update_hint(self):
        i = self.queue[0]
        which = "НАЧАЛО" if self.first is None else "КОНЕЦ"
        step = 1 if self.first is None else 2
        self.canvas.itemconfigure(
            self.hint, text=f"Линия {i + 1}: кликните {which} линии "
                            f"({step}/2).   Esc или правая кнопка мыши — отмена")
        x0, y0, x1, y1 = self.canvas.bbox(self.hint)
        self.canvas.coords(self.hint_bg, x0 - 10, y0 - 8, x1 + 10, y1 + 8)

    def _motion(self, e):
        self.canvas.coords(self.pos, e.x + 14, e.y + 14)
        self.canvas.itemconfigure(self.pos, text=f"{e.x_root}, {e.y_root}")
        if self.first is not None and self.rubber is not None:
            self.canvas.coords(self.rubber, self.first[2], self.first[3], e.x, e.y)

    def _click(self, e):
        c = self.canvas
        i = self.queue[0]
        color = LINE_COLORS_HEX[i]
        c.create_oval(e.x - 5, e.y - 5, e.x + 5, e.y + 5, outline=color, width=2)
        if self.first is None:
            self.first = (e.x_root, e.y_root, e.x, e.y)
            self.rubber = c.create_line(e.x, e.y, e.x, e.y, fill=color, width=2)
        else:
            x1, y1, cx1, cy1 = self.first
            c.coords(self.rubber, cx1, cy1, e.x, e.y)     # готовая линия остаётся на экране
            self.app.on_line_picked(i, (x1, y1), (e.x_root, e.y_root))
            self.queue.pop(0)
            self.first = None
            self.rubber = None
            if not self.queue:
                self.finish(False)
                return
        self._update_hint()

    def finish(self, cancelled):
        if self.done:
            return
        self.done = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.app.on_pick_finished(cancelled)


# =============================================================================
# Линии поверх экрана (не попадают в захват, пропускают клики)
# =============================================================================
class LineOverlay:
    """12 прозрачных окон с линиями (рисуются только включённые). Окна исключены из захвата
    экрана (SetWindowDisplayAffinity), поэтому не искажают пиксели, которые читает программа.
    Если Windows это не поддерживает, линии на экране отключаются (остаётся предпросмотр)."""

    KEY = "#010203"   # цвет, который Windows делает прозрачным

    def __init__(self, root):
        self.available = HAS_AFFINITY
        self.items = []
        for _ in range(LINE_COUNT):
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=self.KEY)
            try:
                win.attributes("-transparentcolor", self.KEY)
            except tk.TclError:
                self.available = False
            canvas = tk.Canvas(win, bg=self.KEY, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
            win.withdraw()
            self.items.append({"win": win, "canvas": canvas, "state": None, "styled": False})

    @staticmethod
    def _style(win):
        """Не забирать фокус, пропускать клики, не попадать в скриншоты."""
        try:
            hwnd = user32.GetAncestor(win.winfo_id(), GA_ROOT) or win.winfo_id()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE |
                                  WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED)
            return bool(HAS_AFFINITY and user32.SetWindowDisplayAffinity(
                hwnd, WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            return False

    def hide(self):
        for item in self.items:
            if item["state"] is not None:
                item["win"].withdraw()
            item["state"] = None

    def update(self, lines, active, enabled):
        """lines — [(x1, y1, x2, y2)] для 12 линий в экранных координатах;
        active — номер ожидаемой линии (рисуется толще); enabled — флаги «Активна»."""
        if not self.available:
            return
        for i, item in enumerate(self.items):
            win = item["win"]
            if not enabled[i]:
                if item["state"] is not None:
                    win.withdraw()
                    item["state"] = None
                continue
            x1, y1, x2, y2 = lines[i]
            state = (x1, y1, x2, y2, i == active)
            if state == item["state"]:
                continue
            item["state"] = state
            pad = 16
            left, top = min(x1, x2) - pad, min(y1, y2) - pad
            w, h = abs(x2 - x1) + 1 + 2 * pad, abs(y2 - y1) + 1 + 2 * pad
            win.geometry(f"{w}x{h}{left:+d}{top:+d}")
            win.deiconify()
            if not item["styled"]:
                if not self._style(win):      # без исключения из захвата рисовать нельзя
                    self.available = False
                    self.hide()
                    return
                item["styled"] = True
            c = item["canvas"]
            c.delete("all")
            color = LINE_COLORS_HEX[i]
            c.create_line(x1 - left, y1 - top, x2 - left, y2 - top, fill=color,
                          width=4 if i == active else 2)
            c.create_oval(x1 - left - 5, y1 - top - 5, x1 - left + 5, y1 - top + 5,
                          outline=color, width=2)
            c.create_text(x1 - left + 10, y1 - top - 12, text=str(i + 1), fill=color,
                          font=("Segoe UI", 11, "bold"))

    def destroy(self):
        for item in self.items:
            try:
                item["win"].destroy()
            except Exception:
                pass


# =============================================================================
# Интерфейс
# =============================================================================
class App:
    PW, PH = 360, 240   # размер панелей предпросмотра

    MODE_TEXT = {
        "mss": ("Весь экран (mss) — рекомендуется",
                "Быстрый универсальный захват. Видит то, что реально отображается на экране, "
                "поэтому окно с видео должно быть видно и не перекрыто другими окнами."),
        "dxcam": ("DirectX-захват (dxcam) — максимальная частота кадров",
                  "Лучше всего работает с аппаратным видео и играми. Только основной монитор. "
                  "Требует установки: pip install dxcam"),
        "window": ("Конкретное окно (WinAPI) — работает, даже если окно перекрыто",
                   "Захватывает содержимое выбранного окна независимо от фокуса и других окон. "
                   "Координаты области считаются от левого верхнего угла этого окна."),
    }

    def __init__(self, root=None, container=None, show_title=True):
        """root/container — для встраивания во вкладку главного окна v6.
        Без параметров — отдельное окно, как раньше."""
        self.standalone = root is None
        self.root = root or tk.Tk()
        self.container = container or self.root
        self.show_title = show_title
        self.is_visible = lambda: True   # v6 подменяет: видна ли вкладка ручного режима
        if self.standalone:
            self.root.title(APP_TITLE)
            self.root.geometry("1200x940")
            self.root.minsize(1100, 780)
        self._closing = False
        self._bulk = False          # пакетное изменение переменных (без лишних пересчётов)
        self._shift_busy = False    # шкала меняется программно (загрузка настроек)
        self._setup_style()

        self.specs = make_num_specs()
        self._startup_msgs = []
        saved = self._read_settings(SETTINGS_FILE, quiet=True)
        if not saved:   # config.json ещё нет — пробуем файл прежних версий
            saved = self._read_settings(LEGACY_SETTINGS_FILE, quiet=True)
        self._create_vars(saved)

        self.windows = []          # [(hwnd, title)]
        self.win_labels = {}       # подпись в списке -> (hwnd, title)
        self.pipette = False
        self._photos = {}
        self._last_frame = None
        self._last_scale = 1.0

        self._f8_prev = False
        self.kb_labels = {}
        self._picker = None
        self._affinity_warned = False
        self._titles_lines = False
        self.prev_boxes = []

        # Общее состояние, отправитель клавиш (Interception) и рабочий поток
        self.shared = Shared(self.collect())
        self.sender = InterceptionKeySender(self.shared)
        self.sender.selected = self.kb_selected
        self.shared.sender = self.sender
        self.worker = Worker(self.shared)
        self.worker.start()

        self._build_ui()
        self.overlay = RoiOverlay(self.root)
        self.line_overlay = LineOverlay(self.root)
        self._refresh_windows(select_title=self.win_title)
        self._bind_arrows(self.root)

        for var in list(self.num.values()) + [self.shape_var, self.mode_var, self.key_var,
                                              self.work_var, self.line_hue_var,
                                              self.line_dry_var, self.seq_on_var,
                                              self.seq_loop_var] + list(self.line_on_vars):
            var.trace_add("write", self._push)
        for var in [self.seq_loop_var, self.seq_on_var] + list(self.line_on_vars):
            var.trace_add("write", lambda *_: self._seq_refresh(keep_text=True))
        self.shift_var.trace_add("write", self._on_shift)
        self._push()

        for msg in self._startup_msgs:
            self.shared.log(msg)
        self.shared.log(self.sender.status_text())
        self.shared.log("Автосохранение настроек: раз в минуту в config.json.")
        if not is_admin():
            self.shared.log("Внимание: программа запущена без прав администратора — "
                            "драйвер Interception может не работать.")
        self.shared.log("Программа запущена. Отправка сигнала ВЫКЛЮЧЕНА — "
                        "настройте область и включите её кнопкой справа или клавишей F8.")

        if self.standalone:
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self._tick)
        self.root.after(AUTOSAVE_MS, self._autosave)

    # ------------------------------------------------------------------ стиль
    def _setup_style(self):
        if STYLE_HOOK is not None:          # v6: своя (тёмная) тема
            STYLE_HOOK(self.root)
            self._unbind_space()
            return
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            style.theme_use("clam")
        base = ("Segoe UI", 10)
        style.configure(".", font=base)
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Bold.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Bold.TCheckbutton", font=("Segoe UI", 10, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 9), foreground="#6b7280")
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)
        style.configure("TNotebook.Tab", padding=(12, 6), font=("Segoe UI", 10, "bold"))
        self._unbind_space()

    def _unbind_space(self):
        # Пробел во время работы не должен "нажимать" кнопки интерфейса
        for cls in ("TButton", "TCheckbutton", "TRadiobutton"):
            try:
                self.root.unbind_class(cls, "<Key-space>")
            except tk.TclError:
                pass

    # ------------------------------------------------------------- переменные
    def _create_vars(self, saved):
        self.num = {}
        for key, (lo, hi, default) in self.specs.items():
            value = default
            if key in saved:
                try:
                    value = min(max(float(saved[key]), lo), hi)
                except (TypeError, ValueError):
                    pass
            if LINE_COORD_RE.match(key):     # координаты линий — целые числа
                self.num[key] = tk.IntVar(self.root, value=int(round(value)))
            else:
                self.num[key] = tk.DoubleVar(self.root, value=value)

        try:
            shape = min(max(int(saved.get("shape", 0)), 0), len(SHAPES) - 1)
        except (TypeError, ValueError):
            shape = 0
        self.shape_var = tk.IntVar(self.root, value=shape)

        mode = saved.get("mode", "mss")
        if mode not in self.MODE_TEXT or (mode == "dxcam" and dxcam is None):
            mode = "mss"
        self.mode_var = tk.StringVar(self.root, value=mode)
        key = str(saved.get("key", "space"))
        self.key_var = tk.StringVar(self.root, value=key if key in VALID_KEYS else "space")
        try:
            self.kb_selected = max(0, min(MAX_KEYBOARDS, int(saved.get("kb_device", 0))))
        except (TypeError, ValueError):
            self.kb_selected = 0
        self.frame_var = tk.BooleanVar(self.root, value=bool(saved.get("show_frame", True)))
        work = saved.get("work", "zone")
        self.work_var = tk.StringVar(self.root, value=work if work in ("zone", "lines") else "zone")
        self.line_hue_var = tk.BooleanVar(self.root, value=bool(saved.get("line_hue", False)))
        self.line_dry_var = tk.BooleanVar(self.root, value=bool(saved.get("line_dry", False)))
        self.show_lines_var = tk.BooleanVar(self.root, value=bool(saved.get("show_lines", True)))
        on = saved.get("line_on")
        if isinstance(on, list):
            flags = [bool(on[i]) if i < len(on) else False for i in range(LINE_COUNT)]
        else:   # совместимость со старым конфигом (поле line_count)
            try:
                cnt = int(saved.get("line_count", 4))
            except (TypeError, ValueError):
                cnt = 4
            flags = [i < cnt for i in range(LINE_COUNT)]
        self.line_on_vars = [tk.BooleanVar(self.root, value=f) for f in flags]
        # Своя очередь линий (вкладка «Очерёдность»)
        self.seq_on_var = tk.BooleanVar(self.root, value=bool(saved.get("seq_on", False)))
        self.seq_list = self._clean_seq(saved.get("seq", []))
        try:
            loop = int(saved.get("seq_loop", 1))
        except (TypeError, ValueError):
            loop = 1
        self.seq_loop_var = tk.IntVar(self.root, value=max(1, loop))
        try:
            shift = int(round(float(saved.get("shift", 0))))
        except (TypeError, ValueError):
            shift = 0
        shift = min(max(shift, -SHIFT_LIMIT), SHIFT_LIMIT)
        self.shift_var = tk.DoubleVar(self.root, value=shift)   # шкала сдвига по оси Y
        self._shift_applied = shift     # сдвиг, уже "вшитый" в координаты линий
        self.win_title = str(saved.get("window_title", ""))
        self.hwnd = 0
        self.win_var = tk.StringVar(self.root)

    def collect(self):
        """Текущие настройки в виде словаря для рабочего потока."""
        p = {k: int(round(v.get())) for k, v in self.num.items()}
        p["shape"] = int(self.shape_var.get())
        p["mode"] = self.mode_var.get()
        p["key"] = self.key_var.get().strip() or "space"
        p["hwnd"] = self.hwnd
        p["work"] = self.work_var.get()
        p["line_hue"] = int(bool(self.line_hue_var.get()))
        p["line_dry"] = int(bool(self.line_dry_var.get()))
        p["line_on"] = tuple(int(bool(v.get())) for v in self.line_on_vars)
        p["seq_on"] = int(bool(self.seq_on_var.get()))
        p["seq"] = tuple(self.seq_list)
        try:
            p["seq_loop"] = int(self.seq_loop_var.get())
        except (tk.TclError, ValueError):   # в поле ввода сейчас не число
            p["seq_loop"] = 1
        return p

    @staticmethod
    def _clean_seq(raw):
        """Очередь из config.json: номера линий с нуля, только 0..11."""
        if isinstance(raw, str):
            return parse_sequence(raw)
        out = []
        if isinstance(raw, (list, tuple)):
            for v in raw:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= v < LINE_COUNT:
                    out.append(v)
        return out[:SEQ_MAX_STEPS]

    def _push(self, *_):
        if self._bulk:
            return
        try:
            self.shared.set_params(self.collect())
        except tk.TclError:
            pass

    # -------------------------------------------------------------- построение
    def _build_ui(self):
        root = self.container
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- Шапка: состояние, заполнение зоны ----
        hdr = ttk.Frame(root, padding=(14, 10, 14, 0))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(0, weight=1)
        ttk.Label(hdr, text=APP_TITLE if self.show_title else "Ручной режим: зона, 12 линий, "
                  "шкала, очерёдность", style="Title.TLabel" if self.show_title
                  else "Bold.TLabel").grid(row=0, column=0, sticky="w")
        self.badge = tk.Label(hdr, text="", font=("Segoe UI", 11, "bold"), fg="white",
                              bg="#6b7280", padx=14, pady=5)
        self.badge.grid(row=0, column=1, sticky="e")

        bar_row = ttk.Frame(hdr)
        bar_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bar_row.columnconfigure(1, weight=1)
        self.fill_var = tk.StringVar(value="Заполнение зоны: 0.0 %")
        ttk.Label(bar_row, textvariable=self.fill_var, style="Bold.TLabel",
                  width=46).grid(row=0, column=0, sticky="w")
        self.bar = tk.Canvas(bar_row, height=24, highlightthickness=1,
                             highlightbackground=THEME["bar_border"], bg=THEME["bar_bg"])
        self.bar.grid(row=0, column=1, sticky="ew", padx=8)
        self.stat_var = tk.StringVar(value="")
        ttk.Label(bar_row, textvariable=self.stat_var, style="Hint.TLabel",
                  width=48, anchor="e").grid(row=0, column=2, sticky="e")
        self.msg_var = tk.StringVar(value="")
        self.msg_lbl = ttk.Label(hdr, textvariable=self.msg_var, foreground="#dc2626",
                                 wraplength=1120, justify="left")
        self.msg_lbl.grid(row=2, column=0, columnspan=2, sticky="w")

        # ---- Предпросмотр: три панели ----
        prev = ttk.Frame(root, padding=(14, 6))
        prev.grid(row=1, column=0, sticky="ew")
        self.prev_boxes = []
        titles = [("Исходное изображение (зона — зелёный контур)", "lbl_orig"),
                  ("Маска: белое = деталь найдена", "lbl_mask"),
                  ("Что видит программа (цветные объекты)", "lbl_seen")]
        for i, (title, attr) in enumerate(titles):
            box = ttk.LabelFrame(prev, text=title, padding=4)
            self.prev_boxes.append(box)
            box.grid(row=0, column=i, padx=(0 if i == 0 else 8, 0), sticky="nsew")
            holder = tk.Frame(box, width=self.PW, height=self.PH, bg="#111827")
            holder.pack()
            holder.pack_propagate(False)
            lbl = tk.Label(holder, text="Нет изображения", fg="#9ca3af", bg="#111827",
                           bd=0, highlightthickness=0)
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            setattr(self, attr, lbl)
        self.lbl_orig.bind("<Button-1>", self._on_pick)

        # ---- Нижняя часть: вкладки настроек и правая панель ----
        bottom = ttk.Frame(root, padding=(14, 4, 14, 12))
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)

        self.nb = ttk.Notebook(bottom)
        self.nb.grid(row=0, column=0, sticky="nsew")
        # Вкладка «Зона» — прежний режим (внутри три подвкладки)
        self.zone_holder = ttk.Frame(self.nb)
        self.nb.add(self.zone_holder, text="Режим «Зона» (рамки, полосы)")
        self.zone_nb = ttk.Notebook(self.zone_holder)
        self.zone_nb.pack(fill="both", expand=True)
        self._tab_parent = self.zone_nb
        self._tab_area()
        self._tab_zone()
        self._tab_color()
        self._tab_parent = self.nb
        self._tab_lines()          # вкладка «Линии (цикл)»
        self._tab_scale()          # вкладка «Шкала»
        self._tab_sequence()       # вкладка «Очерёдность» (своя очередь линий)
        self._tab_signal()
        self._tab_capture()
        self.nb.select(self.lines_tab if self.work_var.get() == "lines" else self.zone_holder)
        self.nb.bind("<<NotebookTabChanged>>", self._on_main_tab)

        side = ttk.Frame(bottom, padding=(12, 0, 0, 0))
        side.grid(row=0, column=1, sticky="ns")
        self._build_side(side)

    # ---- Вспомогательные виджеты ----
    def _slider(self, parent, row, key, label, hint, unit=""):
        lo, hi, _ = self.specs[key]
        var = self.num[key]
        ttk.Label(parent, text=label, style="Bold.TLabel").grid(row=row, column=0, sticky="w",
                                                                pady=(8, 0))
        ttk.Scale(parent, from_=lo, to=hi, variable=var, orient="horizontal").grid(
            row=row, column=1, sticky="ew", padx=10, pady=(8, 0))
        sv = tk.StringVar()
        ttk.Label(parent, textvariable=sv, width=9, anchor="e").grid(row=row, column=2,
                                                                     pady=(8, 0))

        def refresh(*_):
            sv.set(f"{int(round(var.get()))} {unit}".strip())

        var.trace_add("write", refresh)
        refresh()
        if not hint:
            return row + 1
        ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=640,
                  justify="left").grid(row=row + 1, column=0, columnspan=3, sticky="w")
        return row + 2

    def _tab(self, title):
        parent = getattr(self, "_tab_parent", None) or self.nb
        frame = ttk.Frame(parent, padding=14)
        frame.columnconfigure(1, weight=1)
        parent.add(frame, text=title)
        return frame

    def _tab_area(self):
        t = self._tab("1. Область на экране")
        ttk.Label(t, text="Выделите прямоугольник, в котором проезжает деталь. Красная рамка на "
                          "экране показывает эту область. Двигать область можно стрелками "
                          "клавиатуры (Shift — шаг ×10) или кнопками справа.",
                  wraplength=700, justify="left").grid(row=0, column=0, columnspan=3,
                                                       sticky="w")
        r = 1
        r = self._slider(t, r, "x", "Позиция слева (X)",
                         "Расстояние от левого края экрана (или окна) до области.", "пикс")
        r = self._slider(t, r, "y", "Позиция сверху (Y)",
                         "Расстояние от верхнего края экрана (или окна) до области.", "пикс")
        r = self._slider(t, r, "w", "Ширина области",
                         "Чем меньше область, тем быстрее обработка.", "пикс")
        r = self._slider(t, r, "h", "Высота области", "", "пикс")
        r = self._slider(t, r, "step", "Шаг стрелок",
                         "На сколько пикселей сдвигает область одно нажатие стрелки.", "пикс")
        ttk.Checkbutton(t, text="Показывать красную рамку области на экране",
                        variable=self.frame_var).grid(row=r, column=0, columnspan=3,
                                                      sticky="w", pady=(10, 0))
        pad = ttk.Frame(t)
        pad.grid(row=1, column=3, rowspan=4, padx=(16, 0), sticky="n")
        ttk.Label(pad, text="Сдвиг области", style="Bold.TLabel").grid(row=0, column=0,
                                                                       columnspan=3)
        for text, (dx, dy), (gr, gc) in (("▲", (0, -1), (1, 1)), ("◀", (-1, 0), (2, 0)),
                                         ("▶", (1, 0), (2, 2)), ("▼", (0, 1), (3, 1))):
            ttk.Button(pad, text=text, width=4,
                       command=lambda dx=dx, dy=dy: self._move(dx, dy)).grid(row=gr, column=gc,
                                                                            padx=2, pady=2)

    def _tab_zone(self):
        t = self._tab("2. Зона срабатывания")
        ttk.Label(t, text="Деталь считается «на месте», когда она заполняет зону на заданный "
                          "процент. Выберите форму зоны внутри области:",
                  wraplength=700, justify="left").grid(row=0, column=0, columnspan=3,
                                                       sticky="w")
        for i, (name, hint) in enumerate(zip(SHAPES, SHAPE_HINTS)):
            ttk.Radiobutton(t, text=f"{name} — {hint}", value=i,
                            variable=self.shape_var).grid(row=1 + i, column=0, columnspan=3,
                                                          sticky="w", pady=2)
        r = 5
        r = self._slider(t, r, "strip", "Ширина вертикальной полосы",
                         "Только для формы «Вертикальная полоса»: доля ширины области.", "%")
        r = self._slider(t, r, "thr", "Порог срабатывания",
                         "Сигнал отправляется, когда зона заполнена деталью не меньше, чем на "
                         "этот процент. Для «полного перекрытия» ставьте 90–95 %: ровно 100 % "
                         "из-за бликов и шума почти недостижимо.", "%")

    def _tab_color(self):
        t = self._tab("3. Цвет детали")
        ttk.Label(t, text="Деталью считаются пиксели, чей цвет попадает в диапазон ниже. "
                          "Серый и тёмный фон отсекается порогами насыщенности и яркости.",
                  wraplength=700, justify="left").grid(row=0, column=0, columnspan=3,
                                                       sticky="w")
        self.pipette_btn = ttk.Button(t, text="Пипетка: кликните по детали на левом изображении",
                                      command=self._toggle_pipette)
        self.pipette_btn.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r = 2
        r = self._slider(t, r, "hmin", "Оттенок: от", "0–179. Красный ≈ 0, жёлтый ≈ 30, "
                         "зелёный ≈ 60, голубой ≈ 90, синий ≈ 120.")
        r = self._slider(t, r, "hmax", "Оттенок: до", "Верхняя граница диапазона оттенков.")
        r = self._slider(t, r, "smin", "Насыщенность: минимум",
                         "Чем выше значение, тем строже отсекаются серые/белые (бесцветные) "
                         "пиксели фона.")
        r = self._slider(t, r, "vmin", "Яркость: минимум",
                         "Отсекает тёмные пиксели (тени, чёрный фон).")
        r = self._slider(t, r, "vmax", "Яркость: максимум",
                         "Отсекает слишком яркие пиксели (блики, засветку).")
        r = self._slider(t, r, "noise", "Подавление шума",
                         "0 — выключено. Больше — убирает мелкие одиночные точки в маске.")

    def _tab_lines(self):
        t = self._tab("Линии (цикл)")
        self.lines_tab = t
        t.rowconfigure(1, weight=1)
        ttk.Label(t, text="Цикл идёт по ВКЛЮЧЁННЫМ линиям по порядку: ждём изменения пикселей на "
                          "текущей линии → клик → сразу следующая включённая линия (выключенные "
                          "пропускаются) → … → после последней снова первая. Линия — две точки "
                          "в экранных координатах (в режиме «Конкретное окно» — от угла окна).",
                  wraplength=830, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")

        # --- слева: прокручиваемый список из 12 групп ---
        holder = ttk.Frame(t)
        holder.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        canvas = tk.Canvas(holder, width=500, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        for i in range(LINE_COUNT):
            box = ttk.LabelFrame(inner, text=f"Линия {i + 1}", padding=5)
            box.grid(row=i // 2, column=i % 2, padx=(0, 8), pady=(0, 6), sticky="nw")
            top = ttk.Frame(box)
            top.grid(row=0, column=0, columnspan=4, sticky="ew")
            tk.Label(top, width=2, bg=LINE_COLORS_HEX[i]).pack(side="left", padx=(0, 6))
            ttk.Checkbutton(top, text="Активна", variable=self.line_on_vars[i]).pack(side="left")
            ttk.Button(top, text="Мышью", width=8,
                       command=lambda i=i: self.pick_lines([i])).pack(side="right")
            for r, pair in enumerate((("x1", "y1"), ("x2", "y2")), start=1):
                for c, name in enumerate(pair):
                    key = f"l{i + 1}{name}"
                    lo, hi, _ = self.specs[key]
                    ttk.Label(box, text=name.upper() + ":").grid(
                        row=r, column=c * 2, sticky="e", padx=(0 if c == 0 else 8, 2))
                    ttk.Spinbox(box, from_=lo, to=hi, textvariable=self.num[key],
                                width=6).grid(row=r, column=c * 2 + 1, pady=2)

        # --- справа: параметры обнаружения ---
        right = ttk.Frame(t)
        right.grid(row=1, column=1, sticky="new", padx=(14, 0), pady=(8, 0))
        right.columnconfigure(1, weight=1)
        r = 0
        for key, label, unit in (
                ("line_pct", "Порог: % изменившихся пикселей", "%"),
                ("line_delta", "Чувствительность к цвету", ""),
                ("line_thick", "Толщина линии", "пикс"),
                ("line_ref", "Сравнивать с кадром N мс назад", "мс"),
                ("line_lock", "Пауза после срабатывания", "мс"),
                ("line_start", "Начинать цикл с линии №", "")):
            r = self._slider(right, r, key, label, "", unit)
        for text, var in (("Учитывать смену оттенка (не только яркость и насыщенность)",
                           self.line_hue_var),
                          ("Пробный прогон: переключать линии без нажатия клавиши",
                           self.line_dry_var),
                          ("Показывать линии на экране", self.show_lines_var)):
            ttk.Checkbutton(right, text=text, variable=var).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
            r += 1
        btns = ttk.Frame(right)
        btns.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Все вкл", width=9,
                   command=lambda: self._set_all_lines(True)).pack(side="left")
        ttk.Button(btns, text="Все выкл", width=9,
                   command=lambda: self._set_all_lines(False)).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Сбросить цикл", command=self.reset_cycle).pack(
            side="left", padx=(6, 0))
        ttk.Button(right, text="Захватить мышью все включённые линии подряд",
                   command=lambda: self.pick_lines(
                       [i for i in range(LINE_COUNT) if self.line_on_vars[i].get()])).grid(
            row=r + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(right, text="Чувствительность — минимальная разница яркости или насыщенности "
                              "пикселя (0–255), при которой он считается изменившимся. «0 мс» в "
                              "сравнении с кадром = сравнивать с предыдущим кадром.",
                  style="Hint.TLabel", wraplength=330, justify="left").grid(
            row=r + 2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _tab_scale(self):
        t = self._tab("Шкала (подъём камеры)")
        self.scale_tab = t
        ttk.Label(t, text="Подстройка под подъём камеры одним движением: когда вы двигаете "
                          "слайдер вверх или вниз, ВСЕ 12 линий одновременно сдвигаются по оси Y "
                          "на одно и то же значение (слайдер вверх — линии вверх, вниз — вниз). "
                          "Каждую линию по отдельности трогать не нужно.",
                  wraplength=830, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        left = ttk.Frame(t)
        left.grid(row=1, column=0, sticky="n", padx=(24, 34), pady=(8, 0))
        ttk.Label(left, text="↑ выше", style="Hint.TLabel").pack()
        ttk.Scale(left, from_=-SHIFT_LIMIT, to=SHIFT_LIMIT, orient="vertical",
                  variable=self.shift_var, length=300).pack(pady=4)
        ttk.Label(left, text="↓ ниже", style="Hint.TLabel").pack()

        right = ttk.Frame(t)
        right.grid(row=1, column=1, sticky="nw", pady=(8, 0))
        self.shift_text = tk.StringVar()
        tk.Label(right, textvariable=self.shift_text, font=("Segoe UI", 20, "bold"),
                 fg=THEME["accent"]).pack(anchor="w")
        ttk.Label(right, text="Все 12 линий (включая выключенные) сдвинуты по оси Y "
                              "на это значение.", style="Hint.TLabel").pack(anchor="w",
                                                                           pady=(0, 10))
        row = ttk.Frame(right)
        row.pack(anchor="w")
        ttk.Label(row, text="Точное значение:").pack(side="left")
        ttk.Spinbox(row, from_=-SHIFT_LIMIT, to=SHIFT_LIMIT, width=7, format="%.0f",
                    textvariable=self.shift_var).pack(side="left", padx=6)
        ttk.Label(row, text="пикс").pack(side="left")
        steps = ttk.Frame(right)
        steps.pack(anchor="w", pady=(10, 0))
        for text, d in (("▲ 10", -10), ("▲ 1", -1), ("▼ 1", 1), ("▼ 10", 10)):
            ttk.Button(steps, text=text, width=6,
                       command=lambda d=d: self._nudge_shift(d)).pack(side="left", padx=(0, 6))
        ttk.Button(right, text="Сбросить шкалу в 0", style="Big.TButton",
                   command=lambda: self.shift_var.set(0)).pack(anchor="w", pady=(16, 0))
        ttk.Label(right, text="Стрелки ↑ / ↓ на клавиатуре на этой вкладке тоже двигают шкалу "
                              "(Shift — шаг ×10). Координаты линий на вкладке «Линии» меняются "
                              "сразу, в реальном времени. Положение шкалы сохраняется в "
                              "config.json вместе с остальными настройками.",
                  style="Hint.TLabel", wraplength=470, justify="left").pack(anchor="w",
                                                                          pady=(14, 0))
        self._shift_text()

    # ------------------------------------------------- вкладка «Очерёдность»
    def _tab_sequence(self):
        t = self._tab("Очерёдность")
        self.seq_tab = t
        t.columnconfigure(0, weight=0)
        t.columnconfigure(1, weight=1)
        ttk.Label(t, text="Своя очередь срабатывания линий. Линии идут строго по шагам сверху "
                          "вниз, одну линию можно ставить сколько угодно раз. После последнего "
                          "шага цикл продолжается с выбранного шага. Например, шаги "
                          "1-2-3-4-5-6-7-8 и возврат к шагу 6 дают: 1-2-3-4-5-6-7-8-6-7-8-6-7-8…",
                  wraplength=830, justify="left").grid(row=0, column=0, columnspan=2,
                                                        sticky="w")
        ttk.Checkbutton(t, text="Использовать эту очередь вместо обычного порядка 1 → 12 "
                                "(работает в режиме «Линии»)",
                        variable=self.seq_on_var, style="Bold.TCheckbutton").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # --- слева: список шагов и кнопки правки ---
        left = ttk.Frame(t)
        left.grid(row=2, column=0, sticky="nsw", pady=(8, 0))
        lb_holder = ttk.Frame(left)
        lb_holder.grid(row=0, column=0, sticky="ns")
        self.seq_lb = tk.Listbox(lb_holder, width=30, height=14, font=("Consolas", 11),
                                 activestyle="none", exportselection=False,
                                 selectbackground=THEME["accent"], selectforeground="white",
                                 bg=THEME["list_bg"], fg=THEME["text"], highlightthickness=0)
        sb = ttk.Scrollbar(lb_holder, orient="vertical", command=self.seq_lb.yview)
        self.seq_lb.configure(yscrollcommand=sb.set)
        self.seq_lb.pack(side="left", fill="y")
        sb.pack(side="left", fill="y")
        self.seq_lb.bind("<Delete>", lambda e: self._seq_remove())
        self.seq_lb.bind("<Control-Up>", lambda e: (self._seq_move(-1), "break")[1])
        self.seq_lb.bind("<Control-Down>", lambda e: (self._seq_move(1), "break")[1])

        btns = ttk.Frame(left)
        btns.grid(row=0, column=1, sticky="n", padx=(8, 0))
        for text, cmd in (("▲ Выше", lambda: self._seq_move(-1)),
                          ("▼ Ниже", lambda: self._seq_move(1)),
                          ("Копия шага", self._seq_duplicate),
                          ("Удалить шаг", self._seq_remove),
                          ("Очистить всё", self._seq_clear),
                          ("1 → 12 по порядку", self._seq_fill_default)):
            ttk.Button(btns, text=text, width=18, command=cmd).pack(anchor="w", pady=(0, 4))
        ttk.Label(btns, text="Del — удалить,\nCtrl+↑/↓ — двигать", style="Hint.TLabel",
                  justify="left").pack(anchor="w", pady=(6, 0))

        # --- справа: добавление шагов, быстрый ввод, возврат ---
        right = ttk.Frame(t)
        right.grid(row=2, column=1, sticky="nw", padx=(18, 0), pady=(8, 0))
        ttk.Label(right, text="Добавить шаг (после выделенного, или в конец):",
                  style="Bold.TLabel").grid(row=0, column=0, sticky="w")
        pad = ttk.Frame(right)
        pad.grid(row=1, column=0, sticky="w", pady=(4, 0))
        for i in range(LINE_COUNT):
            tk.Button(pad, text=str(i + 1), width=4, font=("Segoe UI", 10, "bold"),
                      bg=LINE_COLORS_HEX[i], fg="black", activebackground=LINE_COLORS_HEX[i],
                      relief="raised", bd=1,
                      command=lambda i=i: self._seq_add(i)).grid(
                row=i // 6, column=i % 6, padx=(0, 4), pady=(0, 4))

        ttk.Label(right, text="Быстрый ввод (номера через «-», пробел или запятую):",
                  style="Bold.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
        quick = ttk.Frame(right)
        quick.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.seq_text = tk.StringVar(self.root, value=format_sequence(self.seq_list))
        ent = ttk.Entry(quick, textvariable=self.seq_text, width=30, font=("Consolas", 11))
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self._seq_apply_text())
        ttk.Button(quick, text="Применить", command=self._seq_apply_text).pack(
            side="left", padx=(6, 0))

        loop_row = ttk.Frame(right)
        loop_row.grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Label(loop_row, text="После последнего шага — к шагу №",
                  style="Bold.TLabel").pack(side="left")
        self.seq_loop_spin = ttk.Spinbox(loop_row, from_=1, to=max(1, len(self.seq_list)),
                                         width=5, textvariable=self.seq_loop_var)
        self.seq_loop_spin.pack(side="left", padx=6)
        ttk.Button(loop_row, text="= выдел.", command=self._seq_loop_from_sel).pack(
            side="left")

        ttk.Label(right, text="Как пойдёт цикл:", style="Bold.TLabel").grid(
            row=5, column=0, sticky="w", pady=(12, 0))
        self.seq_preview_var = tk.StringVar(self.root)
        ttk.Label(right, textvariable=self.seq_preview_var, wraplength=420, justify="left",
                  font=("Consolas", 11)).grid(row=6, column=0, sticky="w")
        self.seq_warn_var = tk.StringVar(self.root)
        ttk.Label(right, textvariable=self.seq_warn_var, foreground="#d97706",
                  wraplength=420, justify="left").grid(row=7, column=0, sticky="w",
                                                        pady=(4, 0))
        ttk.Button(right, text="Сбросить цикл (начать с шага 1)",
                   command=self.reset_cycle).grid(row=8, column=0, sticky="w", pady=(10, 0))
        ttk.Label(right, text="Шаги с выключенной линией («Активна» снята на вкладке «Линии») "
                              "пропускаются. Порог, чувствительность и пауза берутся с вкладки "
                              "«Линии». Слайдер «Начинать цикл с линии №» в этом режиме не "
                              "используется — цикл всегда начинается с шага 1.",
                  style="Hint.TLabel", wraplength=420, justify="left").grid(
            row=9, column=0, sticky="w", pady=(10, 0))

        self._seq_shown_step = None
        self._seq_refresh()

    def _seq_selected(self):
        sel = self.seq_lb.curselection()
        return sel[0] if sel else None

    def _seq_changed(self, select=None):
        """Очередь изменилась: обновить список, поле ввода и отправить в рабочий поток."""
        self._seq_refresh(select=select)
        self._push()

    def _seq_refresh(self, select=None, keep_text=False):
        if not hasattr(self, "seq_lb"):
            return
        seq = self.seq_list
        n = len(seq)
        try:
            loop = int(self.seq_loop_var.get())
        except (tk.TclError, ValueError):
            loop = 1
        loop0 = min(max(loop - 1, 0), max(n - 1, 0))
        enabled = [bool(v.get()) for v in self.line_on_vars]
        if select is None:
            select = self._seq_selected()
        top = self.seq_lb.yview()[0]
        self.seq_lb.delete(0, "end")
        for k, line in enumerate(seq):
            mark = "↻" if k == loop0 else " "
            off = "  (выкл)" if not enabled[line] else ""
            self.seq_lb.insert("end", f"{mark} Шаг {k + 1:>3}:  линия {line + 1:>2}{off}")
            self.seq_lb.itemconfig(k, foreground=THEME["muted"] if off else THEME["text"])
        self.seq_lb.yview_moveto(top)
        if select is not None and n:
            select = min(max(select, 0), n - 1)
            self.seq_lb.selection_set(select)
            self.seq_lb.see(select)
        self._seq_shown_step = None          # подсветку текущего шага нарисовать заново
        self.seq_loop_spin.configure(to=max(1, n))
        if not keep_text:
            self.seq_text.set(format_sequence(seq))

        if not seq:
            self.seq_preview_var.set("— очередь пуста —")
        else:
            prev = seq_preview(seq, loop0, enabled)
            self.seq_preview_var.set(" → ".join(str(i + 1) for i in prev) + " → …"
                                     if prev else "— все линии очереди выключены —")
        warn = []
        if self.seq_on_var.get() and not seq:
            warn.append("Очередь включена, но пуста — работает обычный порядок 1 → 12.")
        if seq and loop - 1 != loop0:
            warn.append(f"Шага №{loop} нет — возврат будет к шагу {loop0 + 1}.")
        if seq and any(enabled[i] for i in seq) and not any(enabled[i] for i in seq[loop0:]):
            warn.append(f"В повторяемой части (шаги {loop0 + 1}–{n}) все линии выключены — "
                        f"после последнего шага цикл начнётся с начала очереди.")
        self.seq_warn_var.set(" ".join(warn))

    def _seq_add(self, line):
        if len(self.seq_list) >= SEQ_MAX_STEPS:
            messagebox.showwarning(APP_TITLE, f"В очереди максимум {SEQ_MAX_STEPS} шагов.")
            return
        sel = self._seq_selected()
        pos = len(self.seq_list) if sel is None else sel + 1
        self.seq_list.insert(pos, line)
        self._seq_changed(select=pos)

    def _seq_remove(self):
        sel = self._seq_selected()
        if sel is None:
            return
        del self.seq_list[sel]
        self._seq_changed(select=sel if sel < len(self.seq_list) else sel - 1)

    def _seq_move(self, d):
        sel = self._seq_selected()
        if sel is None:
            return
        j = sel + d
        if not 0 <= j < len(self.seq_list):
            return
        self.seq_list[sel], self.seq_list[j] = self.seq_list[j], self.seq_list[sel]
        self._seq_changed(select=j)

    def _seq_duplicate(self):
        sel = self._seq_selected()
        if sel is None or len(self.seq_list) >= SEQ_MAX_STEPS:
            return
        self.seq_list.insert(sel + 1, self.seq_list[sel])
        self._seq_changed(select=sel + 1)

    def _seq_clear(self):
        if self.seq_list and messagebox.askyesno(APP_TITLE, "Удалить все шаги очереди?"):
            self.seq_list = []
            self.seq_loop_var.set(1)
            self._seq_changed()

    def _seq_fill_default(self):
        self.seq_list = list(range(LINE_COUNT))
        self.seq_loop_var.set(1)
        self._seq_changed(select=0)

    def _seq_apply_text(self):
        text = self.seq_text.get()
        seq = parse_sequence(text)
        if text.strip() and not seq:
            messagebox.showwarning(APP_TITLE, "Не найдено ни одного номера линии от 1 до 12.")
            return
        self.seq_list = seq
        self._seq_changed()
        self.shared.log(f"Очередь линий: {format_sequence(seq) or '(пусто)'}")

    def _seq_loop_from_sel(self):
        sel = self._seq_selected()
        if sel is not None:
            self.seq_loop_var.set(sel + 1)

    def _seq_highlight(self, step):
        """Подсветка шага, который сейчас ожидается (step = -1 — нет)."""
        if step == self._seq_shown_step or not hasattr(self, "seq_lb"):
            return
        old = self._seq_shown_step
        n = self.seq_lb.size()
        if old is not None and 0 <= old < n:
            self.seq_lb.itemconfig(old, background=THEME["list_bg"])
        if 0 <= step < n:
            self.seq_lb.itemconfig(step, background=THEME["hl_bg"])
            if self.nb.select() == str(self.seq_tab):
                self.seq_lb.see(step)
        self._seq_shown_step = step

    def _tab_signal(self):
        t = self._tab("Сигнал")
        ttk.Label(t, text="Какую клавишу нажимать, как долго её удерживать и как защититься от "
                          "повторных срабатываний.", wraplength=700, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(t, text="Клавиша сигнала", style="Bold.TLabel").grid(row=1, column=0,
                                                                       sticky="w", pady=(10, 0))
        ttk.Combobox(t, textvariable=self.key_var, values=KEYS, state="readonly",
                     width=12).grid(row=1, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Label(t, text="Клавиша отправляется драйвером Interception по скан-коду "
                          "(Пробел = 0x39) в активное окно — переключитесь на игру.",
                  style="Hint.TLabel", wraplength=640, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w")
        r = 3
        r = self._slider(t, r, "hold", "Длительность нажатия",
                         "Сколько клавиша удерживается. Если игра иногда «не замечает» нажатие, "
                         "увеличьте до 50–80 мс. 0 — мгновенное нажатие и отпускание.", "мс")
        r = self._slider(t, r, "cooldown", "Минимальная пауза между сигналами",
                         "Защита от двойных нажатий. 0 — без ограничения.", "мс")
        r = self._slider(t, r, "release", "Возврат в готовность",
                         "После сигнала программа ждёт, пока заполнение зоны упадёт ниже этой "
                         "доли порога (деталь уехала). Только тогда возможен следующий сигнал — "
                         "так на одну деталь приходится одно нажатие.", "% порога")
        ttk.Label(t, text="Клавиатура (Interception)", style="Bold.TLabel").grid(
            row=r, column=0, sticky="w", pady=(12, 0))
        self.kb_combo = ttk.Combobox(t, state="readonly")
        self.kb_combo.grid(row=r, column=1, sticky="ew", padx=10, pady=(12, 0))
        self.kb_combo.bind("<<ComboboxSelected>>", self._on_kb_selected)
        ttk.Button(t, text="Определить нажатием…", command=self.detect_keyboard).grid(
            row=r, column=2, pady=(12, 0))
        self.kb_status = tk.StringVar()
        ttk.Label(t, textvariable=self.kb_status, style="Hint.TLabel", wraplength=700,
                  justify="left").grid(row=r + 1, column=0, columnspan=3, sticky="w")
        self._refresh_kb_list()

    def _tab_capture(self):
        t = self._tab("Захват экрана")
        ttk.Label(t, text="Способ получения картинки. Если в окне «Исходное изображение» чёрный "
                          "кадр или не то, что нужно — попробуйте другой способ.",
                  wraplength=700, justify="left").grid(row=0, column=0, columnspan=3,
                                                       sticky="w")
        r = 1
        for mode, (title, hint) in self.MODE_TEXT.items():
            state = "normal"
            if mode == "dxcam" and dxcam is None:
                title += "  [не установлен]"
                state = "disabled"
            ttk.Radiobutton(t, text=title, value=mode, variable=self.mode_var,
                            state=state, command=self._on_mode).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Label(t, text=hint, style="Hint.TLabel", wraplength=700, justify="left").grid(
                row=r + 1, column=0, columnspan=3, sticky="w", padx=(22, 0))
            r += 2

        ttk.Label(t, text="Окно для захвата", style="Bold.TLabel").grid(
            row=r, column=0, sticky="w", pady=(12, 0))
        self.win_combo = ttk.Combobox(t, textvariable=self.win_var, state="readonly")
        self.win_combo.grid(row=r, column=1, sticky="ew", padx=10, pady=(12, 0))
        self.win_combo.bind("<<ComboboxSelected>>", self._on_window_selected)
        ttk.Button(t, text="Обновить список",
                   command=lambda: self._refresh_windows()).grid(row=r, column=2, pady=(12, 0))
        ttk.Label(t, text="Нужно только для режима «Конкретное окно». Окно должно быть "
                          "развёрнуто (не свёрнуто в панель задач).",
                  style="Hint.TLabel", wraplength=700, justify="left").grid(
            row=r + 1, column=0, columnspan=3, sticky="w")
        ttk.Label(t, text="Если изображение чёрное во ВСЕХ режимах, приложение запрещает захват "
                          "своего окна. Обходить такую защиту не нужно: надёжнее брать видео "
                          "напрямую из источника (камера / поток) или показывать его в другом "
                          "плеере.", style="Hint.TLabel", wraplength=700, justify="left").grid(
            row=r + 2, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _build_side(self, side):
        ttk.Label(side, text="Режим работы", style="Bold.TLabel").pack(anchor="w")
        ttk.Radiobutton(side, text="Зона (рамки, полосы)", value="zone",
                        variable=self.work_var, command=self._on_work_radio).pack(
            anchor="w", pady=(2, 0))
        ttk.Radiobutton(side, text="Цикличные линии (1→2→3→4)", value="lines",
                        variable=self.work_var, command=self._on_work_radio).pack(anchor="w")
        ttk.Separator(side).pack(fill="x", pady=8)
        self.arm_btn = ttk.Button(side, text="▶  Включить отправку сигнала (F8)",
                                  style="Big.TButton", command=self._on_arm_click)
        self.arm_btn.pack(fill="x")
        self.test_btn = ttk.Button(side, text="Тест клика (через 3 секунды)",
                                   command=self.test_click)
        self.test_btn.pack(fill="x", pady=(8, 0))
        self.test_var = tk.StringVar(value="Нажмите, затем переключитесь на игру.")
        ttk.Label(side, textvariable=self.test_var, style="Hint.TLabel", wraplength=300,
                  justify="left").pack(fill="x")

        ttk.Separator(side).pack(fill="x", pady=10)
        ttk.Label(side, text="Настройки (config.json)", style="Bold.TLabel").pack(anchor="w")
        row1 = ttk.Frame(side)
        row1.pack(fill="x", pady=(4, 0))
        ttk.Button(row1, text="Сохранить сейчас", command=self.save_default).pack(
            side="left", expand=True, fill="x")
        ttk.Button(row1, text="Загрузить", command=self.load_default).pack(
            side="left", expand=True, fill="x", padx=(4, 0))
        row2 = ttk.Frame(side)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Button(row2, text="Сохранить как…", command=self.save_as).pack(
            side="left", expand=True, fill="x")
        ttk.Button(row2, text="Из файла…", command=self.load_from).pack(
            side="left", expand=True, fill="x", padx=(4, 0))
        ttk.Button(side, text="Сбросить на стандартные", command=self.reset_defaults).pack(
            fill="x", pady=(4, 0))
        self.save_var = tk.StringVar(value="Автосохранение: каждую минуту")
        ttk.Label(side, textvariable=self.save_var, style="Hint.TLabel", wraplength=300,
                  justify="left").pack(fill="x", pady=(4, 0))

        ttk.Separator(side).pack(fill="x", pady=10)
        ttk.Label(side, text="Журнал событий", style="Bold.TLabel").pack(anchor="w")
        log_frame = ttk.Frame(side)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, width=38, height=8, wrap="word", state="disabled",
                                font=("Consolas", 9), bg=THEME["log_bg"], fg=THEME["text"],
                                insertbackground=THEME["text"], relief="flat", bd=1)
        sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------ клавиатура
    def _bind_arrows(self, widget):
        """Стрелки двигают область, даже если фокус на ползунке или вкладках."""
        if widget is self.root:
            for seq, d in (("<Left>", (-1, 0)), ("<Right>", (1, 0)),
                           ("<Up>", (0, -1)), ("<Down>", (0, 1))):
                self.root.bind_all(seq, lambda e, d=d: self._on_arrow(e, *d))
        if widget.winfo_class() in ("TScale", "TNotebook", "TRadiobutton",
                                    "TCheckbutton", "TButton"):
            for seq, d in (("<Left>", (-1, 0)), ("<Right>", (1, 0)),
                           ("<Up>", (0, -1)), ("<Down>", (0, 1))):
                widget.bind(seq, lambda e, d=d: self._on_arrow(e, *d))
        for child in widget.winfo_children():
            self._bind_arrows(child)

    def _on_arrow(self, event, dx, dy):
        if not self.is_visible():           # открыта другая вкладка главного окна
            return None
        try:
            focus = self.root.focus_get()
            if focus is not None and focus.winfo_class() in (
                    "TEntry", "TCombobox", "TSpinbox", "Entry", "Text", "Listbox"):
                return None
        except (KeyError, tk.TclError):
            return None
        big = bool(event.state & 0x0001)
        if self._on_scale_tab():             # на вкладке «Шкала» стрелки двигают шкалу
            if dy:
                self._nudge_shift(dy * (10 if big else 1))
            return "break"
        self._move(dx, dy, big=big)
        return "break"

    def _move(self, dx, dy, big=False):
        step = int(round(self.num["step"].get())) * (10 if big else 1)
        for key, d in (("x", dx), ("y", dy)):
            if d:
                lo, hi, _ = self.specs[key]
                value = int(round(self.num[key].get())) + d * step
                self.num[key].set(min(max(value, lo), hi))

    # ----------------------------------------------------------------- окна
    def _refresh_windows(self, select_title=None):
        own = APP_TITLE
        self.windows = [(h, t) for h, t in list_windows() if t != own]
        self.win_labels = {f"{t}   [#{h}]": (h, t) for h, t in self.windows}
        self.win_combo.configure(values=list(self.win_labels.keys()))
        wanted = select_title if select_title else self.win_title
        for label, (h, t) in self.win_labels.items():
            if h == self.hwnd or (wanted and t == wanted and not self.hwnd):
                self.win_var.set(label)
                self.hwnd, self.win_title = h, t
                break
        self._push()

    def _on_window_selected(self, _event=None):
        item = self.win_labels.get(self.win_var.get())
        if item:
            self.hwnd, self.win_title = item
            self._push()
            self.shared.log(f"Выбрано окно: {self.win_title}")

    def _on_mode(self):
        if self.mode_var.get() == "window":
            self._refresh_windows()

    # ------------------------------------------------------------- пипетка
    def _toggle_pipette(self):
        self.pipette = not self.pipette
        self.lbl_orig.configure(cursor="crosshair" if self.pipette else "")
        self.pipette_btn.configure(
            text="Кликните по детали на левом изображении… (отмена — нажмите ещё раз)"
            if self.pipette else "Пипетка: кликните по детали на левом изображении")

    def _on_pick(self, event):
        if not self.pipette or self._last_frame is None:
            return
        frame = self._last_frame
        h, w = frame.shape[:2]
        fx, fy = int(event.x / self._last_scale), int(event.y / self._last_scale)
        if not (0 <= fx < w and 0 <= fy < h):
            return
        patch = frame[max(0, fy - 2):fy + 3, max(0, fx - 2):fx + 3].reshape(-1, 3)
        mean_bgr = patch.mean(axis=0).astype(np.uint8).reshape(1, 1, 3)
        hh, ss, vv = (int(c) for c in cv2.cvtColor(mean_bgr, cv2.COLOR_BGR2HSV)[0, 0])
        self.num["hmin"].set(max(0, hh - 12))
        self.num["hmax"].set(min(179, hh + 12))
        self.num["smin"].set(max(0, ss - 70))
        self.num["vmin"].set(max(0, vv - 70))
        self.num["vmax"].set(255)
        self.shared.log(f"Цвет детали выбран пипеткой: H={hh}, S={ss}, V={vv}")
        self._toggle_pipette()

    # --------------------------------------------------------- сигнал и тест
    def _on_arm_click(self):
        self.shared.toggle_armed("кнопка")

    def test_click(self):
        self.test_btn.state(["disabled"])
        self._countdown(3)

    def _countdown(self, n):
        if n > 0:
            self.test_var.set(f"Переключитесь на игру… нажатие через {n} с")
            self.root.after(1000, lambda: self._countdown(n - 1))
            return
        key = self.key_var.get().strip() or "space"
        hold = int(round(self.num["hold"].get()))
        ok, dev, _lat, err = self.sender.press_sync(key, hold)
        if ok:
            msg = (f"Клавиша «{key}» отправлена через Interception (клавиатура {dev}). "
                   "Сработало ли действие в игре?")
            self.test_var.set(msg)
            self.shared.log("ТЕСТ: " + msg)
        else:
            self.test_var.set(f"Не отправлено: {err}")
            self.shared.log(f"ТЕСТ: ошибка отправки клавиши «{key}»: {err}")
        self.test_btn.state(["!disabled"])

    # ------------------------------------------------------ клавиатура
    def _kb_label(self, dev, hwid):
        return f"Клавиатура {dev} — {hwid}" if hwid else f"Клавиатура {dev}"

    def _refresh_kb_list(self):
        s = self.sender
        s.refresh_devices()
        self.kb_labels = {"Автоопределение (рекомендуется)": 0}
        for dev, hwid in s.devices:
            self.kb_labels[self._kb_label(dev, hwid)] = dev
        self.kb_combo.configure(values=list(self.kb_labels.keys()))
        current = next((lbl for lbl, d in self.kb_labels.items() if d == s.selected), None)
        if current is None:        # сохранённой клавиатуры больше нет — возвращаемся в авто
            s.selected = 0
            current = next(iter(self.kb_labels))
        self.kb_combo.set(current)
        self.kb_status.set(s.status_text())

    def _on_kb_selected(self, _event=None):
        self.sender.selected = self.kb_labels.get(self.kb_combo.get(), 0)
        self.shared.log("Клавиатура: " + self.kb_combo.get())

    def detect_keyboard(self):
        if not self.sender.available:
            messagebox.showwarning(APP_TITLE, "Interception недоступен:\n" + self.sender.error)
            return
        if self.sender.start_detect(6000):
            msg = "Нажмите любую клавишу на нужной клавиатуре (ждём 6 секунд)…"
            self.kb_status.set(msg)
            self.shared.log("Определение клавиатуры: " + msg)
        else:
            self.shared.log("Определение клавиатуры уже выполняется или недоступно.")

    def _handle_detect(self):
        res = self.sender.pop_detect()
        if res is None:
            return
        if res > 0:
            self.sender.selected = res
            self._refresh_kb_list()
            self.shared.log(f"Клавиатура определена: устройство {res}.")
        else:
            self.kb_status.set("Нажатие не обнаружено. Нажмите кнопку и повторите.")
            self.shared.log("Определение клавиатуры: нажатие не обнаружено.")

    # ---------------------------------------------------------- сохранение
    # ------------------------------------------------------- режим «Линии»
    def _on_work_radio(self):
        self.nb.select(self.zone_holder if self.work_var.get() == "zone" else self.lines_tab)

    def _on_main_tab(self, _event=None):
        """Клик по вкладке «Зона» / «Линии» переключает режим работы."""
        try:
            idx = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if idx == 0:
            self.work_var.set("zone")
        elif idx == 1:
            self.work_var.set("lines")

    def reset_cycle(self):
        self.shared.reset_cycle.set()

    def _capture_origin(self, p):
        """Начало координат: (0, 0) для экрана; левый верхний угол окна для режима «Окно».
        None — окно недоступно (закрыто или свёрнуто)."""
        if p["mode"] == "window":
            hwnd = p["hwnd"]
            if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
                return None
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return rect.left, rect.top
        return 0, 0

    def pick_lines(self, indices):
        """Выбор точек линий мышью: окно программы прячется, на экране — накладка."""
        indices = list(indices)
        if not indices:
            messagebox.showinfo(APP_TITLE, "Нет включённых линий: отметьте «Активна».")
            return
        if self._picker is not None:
            return
        self.shared.log("Выбор точек: кликайте мышью по экрану "
                        "(Esc или правая кнопка мыши — отмена).")
        self.root.withdraw()
        try:
            self._picker = PointPicker(self, list(indices))
        except Exception as exc:
            self._picker = None
            self.root.deiconify()
            messagebox.showerror(APP_TITLE, f"Не удалось запустить выбор точек:\n{exc}")

    def on_line_picked(self, idx, p1, p2):
        """Вызывается накладкой, когда обе точки линии выбраны (экранные координаты)."""
        ox, oy = self._capture_origin(self.shared.get_params()) or (0, 0)
        vals = {"x1": p1[0] - ox, "y1": p1[1] - oy, "x2": p2[0] - ox, "y2": p2[1] - oy}
        for suffix, value in vals.items():
            key = f"l{idx + 1}{suffix}"
            lo, hi, _ = self.specs[key]
            self.num[key].set(int(min(max(value, lo), hi)))
        self.line_on_vars[idx].set(True)          # выбранная мышью линия включается
        self.shared.log(f"Линия {idx + 1}: ({vals['x1']}, {vals['y1']}) → "
                        f"({vals['x2']}, {vals['y2']})")

    def on_pick_finished(self, cancelled):
        self._picker = None
        self.root.deiconify()
        self.root.lift()
        self.shared.log("Выбор точек отменён." if cancelled else "Точки линий заданы.")

    def _sync_preview_titles(self, lines_mode):
        if lines_mode == self._titles_lines:
            return
        self._titles_lines = lines_mode
        titles = (["Линии на кадре (активная — толще)",
                   "Карта изменений между кадрами (ярче = сильнее)",
                   "Пиксели вдоль линий (красные метки — изменились)"] if lines_mode else
                  ["Исходное изображение (зона — зелёный контур)",
                   "Маска: белое = деталь найдена",
                   "Что видит программа (цветные объекты)"])
        for box, text in zip(self.prev_boxes, titles):
            box.configure(text=text)

    # ------------------------------------------------ шкала массового сдвига
    def _on_scale_tab(self):
        try:
            return self.nb.select() == str(self.scale_tab)
        except (tk.TclError, AttributeError):
            return False

    def _shift_text(self):
        v = int(round(self.shift_var.get()))
        if v == 0:
            text = "Сдвиг: 0 пикс (исходное положение)"
        else:
            text = f"Сдвиг: {v:+d} пикс ({'вверх' if v < 0 else 'вниз'})"
        self.shift_text.set(text)

    def _nudge_shift(self, d):
        value = int(round(self.shift_var.get())) + int(d)
        self.shift_var.set(min(max(value, -SHIFT_LIMIT), SHIFT_LIMIT))

    def _on_shift(self, *_):
        """Слайдер сдвинули: Y всех 12 линий меняется на разницу с прошлым положением."""
        if self._shift_busy:
            return
        try:
            new = int(round(self.shift_var.get()))
        except tk.TclError:
            return
        self._shift_text()
        delta = new - self._shift_applied
        if delta == 0:
            return
        self._shift_applied = new
        self._bulk = True                   # одним пакетом: без 24 пересчётов настроек
        try:
            for i in range(1, LINE_COUNT + 1):
                for suffix in ("y1", "y2"):
                    key = f"l{i}{suffix}"
                    lo, hi, _ = self.specs[key]
                    try:
                        self.num[key].set(min(max(int(self.num[key].get()) + delta, lo), hi))
                    except tk.TclError:
                        pass
        finally:
            self._bulk = False
        self._push()

    def _set_all_lines(self, value):
        self._bulk = True
        try:
            for var in self.line_on_vars:
                var.set(value)
        finally:
            self._bulk = False
        self._push()

    # ------------------------------------------------------- автосохранение
    def _autosave(self):
        """Раз в минуту молча сохраняет все настройки в config.json."""
        if self._closing:
            return
        try:
            self._write_settings(SETTINGS_FILE)
            self.save_var.set("Автосохранение: " + time.strftime("%H:%M:%S"))
        except Exception as exc:
            self.save_var.set(f"Автосохранение не удалось: {exc}")
            self.shared.log(f"Автосохранение не удалось: {exc}")
        self.root.after(AUTOSAVE_MS, self._autosave)

    def load_default(self):
        if not SETTINGS_FILE.exists():
            messagebox.showinfo(APP_TITLE, f"Файл {SETTINGS_FILE.name} ещё не создан.\n"
                                           "Нажмите «Сохранить сейчас».")
            return
        data = self._read_settings(SETTINGS_FILE)
        if data:
            self._apply_settings(data)
            self.shared.log(f"Настройки загружены: {SETTINGS_FILE.name}")
            self.save_var.set("Загружено вручную: " + time.strftime("%H:%M:%S"))

    def snapshot(self):
        data = {"version": 5}
        data.update({k: int(round(v.get())) for k, v in self.num.items()})
        data["shape"] = int(self.shape_var.get())
        data["mode"] = self.mode_var.get()
        data["key"] = self.key_var.get().strip() or "space"
        data["kb_device"] = int(self.sender.selected)
        data["show_frame"] = bool(self.frame_var.get())
        data["work"] = self.work_var.get()
        data["line_hue"] = bool(self.line_hue_var.get())
        data["line_dry"] = bool(self.line_dry_var.get())
        data["show_lines"] = bool(self.show_lines_var.get())
        data["line_on"] = [bool(v.get()) for v in self.line_on_vars]
        data["seq_on"] = bool(self.seq_on_var.get())
        data["seq"] = list(self.seq_list)       # номера линий с нуля
        data["seq_text"] = format_sequence(self.seq_list)   # для чтения человеком
        try:
            data["seq_loop"] = int(self.seq_loop_var.get())
        except (tk.TclError, ValueError):
            data["seq_loop"] = 1
        data["shift"] = int(round(self.shift_var.get()))
        data["window_title"] = self.win_title
        return data

    def _write_settings(self, path):
        """Запись через временный файл: при сбое в момент записи config.json не портится."""
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    def _read_settings(self, path, quiet=False):
        try:
            if Path(path).exists():
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            msg = f"Не удалось прочитать настройки: {exc}"
            if quiet:
                self._startup_msgs.append(msg)
            else:
                messagebox.showerror(APP_TITLE, msg)
        return {}

    def save_default(self):
        try:
            self._write_settings(SETTINGS_FILE)
            self.shared.log(f"Настройки сохранены: {SETTINGS_FILE.name}")
            self.save_var.set("Сохранено вручную: " + time.strftime("%H:%M:%S"))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить настройки:\n{exc}")

    def save_as(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить настройки", defaultextension=".json",
            initialdir=str(SETTINGS_FILE.parent), initialfile="настройки.json",
            filetypes=[("Файл настроек JSON", "*.json")])
        if path:
            try:
                self._write_settings(path)
                self.shared.log(f"Настройки сохранены: {path}")
            except Exception as exc:
                messagebox.showerror(APP_TITLE, f"Не удалось сохранить настройки:\n{exc}")

    def load_from(self):
        path = filedialog.askopenfilename(
            title="Загрузить настройки", initialdir=str(SETTINGS_FILE.parent),
            filetypes=[("Файл настроек JSON", "*.json"), ("Все файлы", "*.*")])
        if path:
            data = self._read_settings(path)
            if data:
                self._apply_settings(data)
                self.shared.log(f"Настройки загружены: {path}")

    def reset_defaults(self):
        if messagebox.askyesno(APP_TITLE, "Сбросить все настройки на стандартные значения?"):
            data = {k: d for k, (_, _, d) in self.specs.items()}
            data.update({"shape": 0, "mode": "mss", "key": "space", "show_frame": True,
                         "window_title": "", "kb_device": 0, "work": "zone",
                         "line_hue": False, "line_dry": False, "show_lines": True,
                         "line_on": [i < 4 for i in range(LINE_COUNT)], "shift": 0,
                         "seq_on": False, "seq": [], "seq_loop": 1})
            self._apply_settings(data)
            self.shared.log("Настройки сброшены на стандартные.")

    def _apply_settings(self, d):
        self._bulk = True     # применяем всё пакетом, настройки уйдут в поток один раз
        for key, (lo, hi, _) in self.specs.items():
            if key in d:
                try:
                    value = min(max(float(d[key]), lo), hi)
                    self.num[key].set(int(round(value)) if isinstance(self.num[key], tk.IntVar)
                                      else value)
                except (TypeError, ValueError):
                    pass
        try:
            self.shape_var.set(min(max(int(d.get("shape", 0)), 0), len(SHAPES) - 1))
        except (TypeError, ValueError):
            pass
        mode = d.get("mode", "mss")
        if mode in self.MODE_TEXT and not (mode == "dxcam" and dxcam is None):
            self.mode_var.set(mode)
        key = str(d.get("key", "space"))
        self.key_var.set(key if key in VALID_KEYS else "space")
        try:
            self.sender.selected = max(0, min(MAX_KEYBOARDS, int(d.get("kb_device", 0))))
        except (TypeError, ValueError):
            self.sender.selected = 0
        self._refresh_kb_list()
        self.frame_var.set(bool(d.get("show_frame", True)))
        work = d.get("work", "zone")
        self.work_var.set(work if work in ("zone", "lines") else "zone")
        self.line_hue_var.set(bool(d.get("line_hue", False)))
        self.line_dry_var.set(bool(d.get("line_dry", False)))
        self.show_lines_var.set(bool(d.get("show_lines", True)))
        self._on_work_radio()
        on = d.get("line_on")
        if isinstance(on, list):
            for i in range(LINE_COUNT):
                self.line_on_vars[i].set(bool(on[i]) if i < len(on) else False)
        elif "line_count" in d:               # совместимость со старым конфигом
            try:
                cnt = int(d["line_count"])
            except (TypeError, ValueError):
                cnt = 4
            for i in range(LINE_COUNT):
                self.line_on_vars[i].set(i < cnt)
        try:
            shift = int(round(float(d.get("shift", 0))))
        except (TypeError, ValueError):
            shift = 0
        shift = min(max(shift, -SHIFT_LIMIT), SHIFT_LIMIT)
        self._shift_busy = True    # координаты линий в файле уже содержат этот сдвиг
        self.shift_var.set(shift)
        self._shift_applied = shift
        self._shift_busy = False
        self._shift_text()
        self.seq_on_var.set(bool(d.get("seq_on", False)))
        self.seq_list = self._clean_seq(d.get("seq", []))
        try:
            self.seq_loop_var.set(max(1, int(d.get("seq_loop", 1))))
        except (TypeError, ValueError):
            self.seq_loop_var.set(1)
        title = str(d.get("window_title", ""))
        self.hwnd = 0
        self.win_var.set("")
        self.win_title = title
        self._bulk = False
        self._seq_refresh()
        self._push()
        self._refresh_windows(select_title=title)

    # ------------------------------------------------------------ обновление
    def _tick(self):
        try:
            self._update_ui()
        except Exception:
            traceback.print_exc()
        if not self._closing:
            self.root.after(30, self._tick)

    def _update_ui(self):
        sh = self.shared
        # Горячая клавиша F8 (глобальная, опрос состояния клавиши — без хуков и библиотек)
        f8_down = user32.GetAsyncKeyState(0x77) < 0
        if f8_down and not self._f8_prev:
            sh.toggle_armed("F8")
        self._f8_prev = f8_down
        self._handle_detect()

        st = sh.get_stats()
        armed = sh.armed
        p = sh.get_params()
        lines_mode = p["work"] == "lines"
        self._sync_preview_titles(lines_mode)
        now = time.perf_counter()

        self.arm_btn.configure(text="■  Выключить отправку сигнала (F8)" if armed
                               else "▶  Включить отправку сигнала (F8)")

        if st["error"]:
            text, color = "ОШИБКА ЗАХВАТА", "#dc2626"
        elif st["black"]:
            text, color = "ЧЁРНЫЙ КАДР — захват заблокирован?", "#d97706"
        elif lines_mode:
            nxt = st["active"] + 1
            if seq_mode(p) and st.get("step", -1) >= 0:
                nxt = f"{nxt} (шаг {st['step'] + 1}/{st['seq_len']})"
            if st["fired_line"] >= 0 and now - st["fired_at"] < 0.45:
                text, color = (f"ЛИНИЯ {st['fired_line'] + 1} СРАБОТАЛА → КЛИК → "
                               f"ждём линию {nxt}", "#16a34a")
            elif p["line_dry"]:
                text, color = f"ПРОБНЫЙ ПРОГОН — ждём линию {nxt}", "#d97706"
            elif armed:
                text, color = f"ОТПРАВКА ВКЛЮЧЕНА — ждём линию {nxt}", "#2563eb"
            else:
                text, color = f"ОТПРАВКА ВЫКЛЮЧЕНА — ждём линию {nxt} (цикл стоит)", "#6b7280"
        elif armed and st["triggered"]:
            text, color = "ДЕТАЛЬ В ЗОНЕ — СИГНАЛ ОТПРАВЛЕН", "#16a34a"
        elif armed:
            text, color = "ОТПРАВКА ВКЛЮЧЕНА — ожидание детали", "#2563eb"
        elif st["triggered"]:
            text, color = "ДЕТАЛЬ В ЗОНЕ (отправка выключена)", "#d97706"
        else:
            text, color = "ОТПРАВКА ВЫКЛЮЧЕНА — режим настройки", "#6b7280"
        self.badge.configure(text=text, bg=color)

        fill = st["fill"]
        if lines_mode:
            thr = p["line_pct"]
            on = len(active_lines(p))
            self.fill_var.set(f"Линия {st['active'] + 1}: изменилось {fill:4.1f} %  "
                              f"(порог {thr} %)")
            self.stat_var.set(f"Кадров/с: {st['fps']:.0f}   Линий в цикле: {on}   "
                              f"Кругов: {st['cycles']}   Сигналов: {st['sent']}")
        else:
            thr = p["thr"]
            self.fill_var.set(f"Заполнение зоны: {fill:5.1f} %  (порог {thr} %)")
            self.stat_var.set(f"Кадров/с: {st['fps']:.0f}   Задержка: {st['latency']:.1f} мс   "
                              f"Сигналов: {st['sent']}")
        self._draw_bar(fill, thr, fill >= thr)

        if st["error"]:
            self.msg_var.set(st["error"])
        elif st["black"]:
            self.msg_var.set("Весь кадр чёрный. Попробуйте другой способ захвата (вкладка "
                             "«Захват экрана») или проверьте, что окно видно на экране.")
        elif not self.sender.available:
            self.msg_var.set("Interception недоступен: " + self.sender.error)
        else:
            self.msg_var.set("")

        item = sh.take_preview()
        if item is not None:
            self._render_preview(item)

        while True:
            try:
                line = sh.log_q.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)

        self._update_overlay(p)
        self._seq_highlight(st.get("step", -1) if lines_mode and seq_mode(p)
                            and not st["error"] else -1)

    def _draw_bar(self, fill, thr, hit):
        c = self.bar
        w = max(c.winfo_width(), 10)
        h = max(c.winfo_height(), 10)
        c.delete("all")
        c.create_rectangle(0, 0, w * min(fill, 100) / 100.0, h,
                           fill="#16a34a" if hit else "#3b82f6", outline="")
        tx = w * thr / 100.0
        c.create_line(tx, 0, tx, h, fill="#dc2626", width=3)

    def _render_zone_preview(self, frame, in_zone, shape_mask):
        h, w = frame.shape[:2]
        scale = min(self.PW / w, self.PH / h, 8.0)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_NEAREST if scale >= 1 else cv2.INTER_AREA
        a = cv2.resize(frame, (nw, nh), interpolation=interp)
        m = cv2.resize(in_zone, (nw, nh), interpolation=cv2.INTER_NEAREST)
        s = cv2.resize(shape_mask, (nw, nh), interpolation=cv2.INTER_NEAREST)
        contours, _ = cv2.findContours(s, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        left = a.copy()
        cv2.drawContours(left, contours, -1, (0, 255, 0), 2)
        mid = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
        cv2.drawContours(mid, contours, -1, (0, 200, 0), 2)
        right = cv2.bitwise_and(a, a, mask=m)
        cv2.drawContours(right, contours, -1, (0, 255, 255), 2)

        self._last_frame = frame
        self._last_scale = nw / float(w)
        for idx, (lbl, img) in enumerate(((self.lbl_orig, left), (self.lbl_mask, mid),
                                          (self.lbl_seen, right))):
            photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
            self._photos[idx] = photo  # ссылка нужна, иначе картинку удалит сборщик мусора
            lbl.configure(image=photo, text="")

    def _render_preview(self, item):
        if item[0] == "lines":
            self._render_lines_preview(*item[1:])
        else:
            self._render_zone_preview(*item[1:])

    def _show(self, idx, lbl, bgr):
        photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
        self._photos[idx] = photo  # ссылка нужна, иначе картинку удалит сборщик мусора
        lbl.configure(image=photo, text="")

    def _render_lines_preview(self, frame, diff, info):
        h, w = frame.shape[:2]
        scale = min(self.PW / w, self.PH / h, 8.0)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_NEAREST if scale >= 1 else cv2.INTER_AREA
        a = cv2.resize(frame, (nw, nh), interpolation=interp)
        d = cv2.applyColorMap(cv2.resize(diff, (nw, nh), interpolation=cv2.INTER_NEAREST),
                              cv2.COLORMAP_INFERNO)
        sc = nw / float(w)
        for i in info["act"]:
            x1, y1, x2, y2 = info["lines"][i]
            pt1 = (int((x1 + 0.5) * sc), int((y1 + 0.5) * sc))
            pt2 = (int((x2 + 0.5) * sc), int((y2 + 0.5) * sc))
            current = i == info["active"]
            color = LINE_COLORS_BGR[i]
            cv2.line(a, pt1, pt2, color, 3 if current else 1, cv2.LINE_AA)
            cv2.circle(a, pt1, 4, color, -1, cv2.LINE_AA)
            cv2.putText(a, str(i + 1), (pt1[0] + 6, pt1[1] - 6), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, color, 1, cv2.LINE_AA)
            cv2.line(d, pt1, pt2, (255, 255, 255), 2 if current else 1, cv2.LINE_AA)

        # Правая панель: по строке на каждую включённую линию
        strip = np.zeros((self.PH, self.PW, 3), np.uint8)
        act = info["act"]
        n = max(1, len(act))
        row_h = self.PH // n
        compact = row_h < 36                      # много линий: только полоска с процентом
        width = self.PW - 12
        for r, i in enumerate(act):
            y0 = r * row_h
            pix, changed = info["profiles"][i]
            pct = info["pcts"][i]
            hit = pct >= info["thr"]
            current = i == info["active"]
            if compact:
                bx0, bw_ = 56, self.PW - 66
                cv2.putText(strip, f"{i + 1:>2}", (6, y0 + row_h - 6), cv2.FONT_HERSHEY_SIMPLEX,
                            0.45, LINE_COLORS_BGR[i], 1, cv2.LINE_AA)
                cv2.rectangle(strip, (bx0, y0 + 3), (bx0 + bw_, y0 + row_h - 4), (70, 70, 70), 1)
                fill_w = int(bw_ * min(pct, 100.0) / 100.0)
                if fill_w > 0:
                    cv2.rectangle(strip, (bx0, y0 + 3), (bx0 + fill_w, y0 + row_h - 4),
                                  (0, 200, 0) if hit else (200, 120, 0), -1)
                tx = bx0 + int(bw_ * info["thr"] / 100.0)
                cv2.line(strip, (tx, y0 + 2), (tx, y0 + row_h - 3), (0, 0, 255), 1)
                cv2.putText(strip, f"{pct:.0f}%", (bx0 + bw_ - 34, y0 + row_h - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
            else:
                prof_h = max(8, row_h - 30)
                prof = cv2.resize(pix.reshape(1, -1, 3), (width, prof_h),
                                  interpolation=cv2.INTER_NEAREST)
                strip[y0 + 16:y0 + 16 + prof_h, 6:6 + width] = prof
                if changed is not None:
                    marks = cv2.resize(changed.astype(np.uint8).reshape(1, -1) * 255,
                                       (width, 4), interpolation=cv2.INTER_NEAREST)
                    region = strip[y0 + 16 + prof_h:y0 + 20 + prof_h, 6:6 + width]
                    region[marks > 0] = (0, 0, 255)
                cv2.putText(strip, f"{i + 1}: {pct:.1f}%" + (" <" if current else ""),
                            (6, y0 + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                            (0, 255, 0) if hit else (255, 255, 255), 1, cv2.LINE_AA)
            if current:
                cv2.rectangle(strip, (1, y0 + 1), (self.PW - 2, y0 + row_h - 2),
                              (255, 255, 255) if compact else LINE_COLORS_BGR[i], 1)
        self._show(0, self.lbl_orig, a)
        self._show(1, self.lbl_mask, d)
        self._show(2, self.lbl_seen, strip)

    def _append_log(self, line):
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", line + "\n")
        if int(t.index("end-1c").split(".")[0]) > 300:
            t.delete("1.0", "50.0")
        t.see("end")
        t.configure(state="disabled")

    def _update_overlay(self, p):
        origin = self._capture_origin(p)      # None — окно недоступно
        if origin is None:
            self.overlay.hide()
            self.line_overlay.hide()
            return
        ox, oy = origin
        if p["work"] == "lines":
            self.overlay.hide()
            if self.show_lines_var.get() and self.line_overlay.available:
                lines = [(ox + p[f"l{i}x1"], oy + p[f"l{i}y1"],
                          ox + p[f"l{i}x2"], oy + p[f"l{i}y2"]) for i in range(1, LINE_COUNT + 1)]
                active = self.shared.get_stats()["active"]
                used = set(active_lines(p))     # в режиме очереди — только линии из очереди
                self.line_overlay.update(lines, active,
                                         [int(i in used) for i in range(LINE_COUNT)])
            else:
                self.line_overlay.hide()
                if (self.show_lines_var.get() and not self.line_overlay.available
                        and not self._affinity_warned):
                    self._affinity_warned = True
                    self.shared.log("Линии на экране отключены: эта версия Windows не "
                                    "позволяет исключить их из захвата (нужна Windows 10 "
                                    "2004+). Линии видны в окне предпросмотра.")
        else:
            self.line_overlay.hide()
            if self.frame_var.get():
                self.overlay.show(ox + p["x"], oy + p["y"], p["w"], p["h"])
            else:
                self.overlay.hide()

    # ------------------------------------------------------------- выход
    def on_close(self):
        self.shutdown()
        self.root.destroy()

    def shutdown(self):
        """Сохранить настройки и остановить потоки (без закрытия окна)."""
        self._closing = True
        try:
            self._write_settings(SETTINGS_FILE)
        except Exception:
            pass
        self.shared.stop.set()
        self.worker.join(timeout=1.5)
        self.sender.close()
        if self._picker is not None:
            self._picker.finish(True)
        self.overlay.destroy()
        self.line_overlay.destroy()

    def run(self):
        self.root.mainloop()


def main():
    try:
        sys.setswitchinterval(0.001)  # интерфейс не "залипает" при загруженном рабочем потоке
    except Exception:
        pass
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)   # точные паузы (удержание клавиши)
    except Exception:
        pass

    if not is_admin():
        tmp = tk.Tk()
        tmp.withdraw()
        answer = messagebox.askyesnocancel(
            APP_TITLE,
            "Программа запущена БЕЗ прав администратора.\n\n"
            "Драйверу Interception для отправки нажатий обычно нужны права администратора.\n\n"
            "Да — перезапустить от имени администратора (рекомендуется)\n"
            "Нет — продолжить без прав (нажатия могут не работать)\n"
            "Отмена — выйти",
            icon="warning", parent=tmp)
        relaunched = False
        if answer:
            relaunched = relaunch_as_admin()
            if not relaunched:
                messagebox.showwarning(
                    APP_TITLE,
                    "Не удалось перезапуститься с правами администратора.\n"
                    "Запустите программу вручную: правый клик по консоли/ярлыку → "
                    "«Запуск от имени администратора».", parent=tmp)
        tmp.destroy()
        if answer is None or relaunched:
            return
    try:
        App().run()
    except Exception as exc:
        traceback.print_exc()
        try:
            messagebox.showerror(APP_TITLE, f"Критическая ошибка:\n{exc}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
