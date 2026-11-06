# -*- coding: utf-8 -*-
r"""
ИИ-АВТОПИЛОТ — движок (без интерфейса).

Что делает каждый кадр:
  1. Захват области игры (mss / dxcam) и уменьшение до «рабочей» ширины.
  2. Оптический поток Farneback: какие пиксели движутся и с какой скоростью.
     Общий сдвиг всей картинки (камера поднимается) вычитается.
  3. Модель фона: гистограмма H-S пикселей вокруг столба. Всё, что на фон не похоже, —
     объект (деталь или столб).
  4. Столб (башня): неподвижный объект, который касается низа области. Верх столба и его
     края -> зона над столбом и точка прицеливания.
  5. Деталь: объект над столбом, который движется. По истории положений и оптическому
     потоку строится прогноз движения (скорость, ускорение — подходит и для «маятника»).
  6. Клик: в момент, когда прогноз места падения совпадает с центром столба, с учётом
     задержки и выученного смещения тайминга.
  7. Оценка: очки на экране (easyocr): +2 идеал, +1 обычно, 0 промах. Если очки не
     читаются — оценка по геометрии (насколько ровно деталь легла на столб).
  8. Обучение:
       * тайминг — обучение с подкреплением (контекстный бандит по скорости детали) +
         поправка по измеренной ошибке попадания;
       * детали — серия снимков за пролёт -> шаблон (медиана) + гистограмма цвета.
         Обрезанные после промаха детали узнаются как «искажение» известного типа, а не как
         новая деталь (сравнение с левой/правой частью шаблона и с выученными искажениями).
  9. База данных: папка ai_database (вырезанные детали, фоны, верх столба, шаблоны, таблица
     обучения).
"""
import collections
import json
import math
import queue
import random
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

import conveyor_monitor_v5 as cm

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "ai_database"
AI_CONFIG_FILE = BASE_DIR / "ai_config.json"

TPL_W, TPL_H = 64, 32          # размер нормализованного шаблона детали


def imread(path, flags=cv2.IMREAD_COLOR):
    """cv2.imread не открывает пути с кириллицей — читаем байты сами."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, flags) if data.size else None
    except Exception:
        return None


def imwrite(path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        Path(path).write_bytes(buf.tobytes())
    return ok

# Состояния автопилота (для интерфейса)
ST_STOPPED, ST_SEARCH, ST_WAIT, ST_TRACK, ST_DROP, ST_EVAL = (
    "stopped", "search", "wait", "track", "drop", "eval")
STATE_TEXT = {
    ST_STOPPED: "Остановлен",
    ST_SEARCH: "Ищу столб деталей…",
    ST_WAIT: "Столб найден — жду новую деталь",
    ST_TRACK: "Слежу за деталью, рассчитываю момент клика",
    ST_DROP: "Клик! Деталь падает",
    ST_EVAL: "Оцениваю результат (счёт)",
}


def _screen_rect():
    u = cm.user32
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def default_config():
    sx, sy, sw, sh = _screen_rect()
    return {
        # Область игры (экранные координаты). 0 ширина = весь основной экран.
        "roi_x": sx, "roi_y": sy, "roi_w": sw, "roi_h": sh,
        # Область счёта (экранные координаты). 0 ширина = авто (верх по центру области игры).
        "score_x": 0, "score_y": 0, "score_w": 0, "score_h": 0,
        "capture": "mss",            # mss | dxcam
        "key": "space",              # клавиша или mouse_left / mouse_right
        "hold_ms": 40,               # удержание клавиши
        "work_width": 360,           # ширина картинки для анализа, пикс
        "motion_thr": 10,            # порог движения ×0.1 пикс/кадр (10 = 1.0)
        "bg_thr": 12,                # порог «не фон» (отличие от карты фона)
        "tex_thr": 18,               # порог текстуры (узоры деталей)
        "min_block": 1,              # минимальный размер детали, ‰ площади кадра
        "zone_h": 30,                # высота зоны над столбом, % от высоты кадра
        "latency_ms": 25,            # задержка системы (захват + драйвер + игра)
        "lead_ms": 0,                # ручное смещение тайминга (+ раньше, − позже)
        "cooldown_ms": 700,          # минимум между кликами
        "settle_ms": 900,            # сколько ждать приземления детали
        "perfect_pct": 6,            # «идеал» по геометрии: ошибка < N% ширины детали
        "explore_pct": 15,           # доля проб (исследование), %
        "learn_rate": 30,            # скорость обучения, %
        "match_pct": 72,             # порог «та же деталь», %
        "ocr_on": True,              # читать очки easyocr
        "learn_on": True,            # обучаться
        "save_db": True,             # сохранять снимки в базу
        "dry_run": False,            # пробный прогон: без кликов
        "show_preview": True,
    }


def load_config():
    cfg = default_config()
    try:
        if AI_CONFIG_FILE.exists():
            data = json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in cfg and type(v) in (int, float, bool, str):
                        cfg[k] = type(cfg[k])(v) if not isinstance(cfg[k], bool) else bool(v)
    except Exception:
        pass
    if cfg["key"] not in cm.VALID_KEYS:
        cfg["key"] = "space"
    return cfg


def save_config(cfg):
    tmp = AI_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(AI_CONFIG_FILE)


# =============================================================================
# Модель фона (гистограммы)
# =============================================================================
class BackgroundModel:
    """Модель фона из трёх частей:
      1. Карта фона — плавная картинка фона, восстановленная по пикселям БЕЗ объектов
         (нормированная свёртка). Справляется с градиентами и «свечением» в центре экрана.
      2. Гистограмма H-S фона вокруг столба — пиксели типичного для фона цвета без
         текстуры не считаются объектом.
      3. Текстура — у деталей много мелких линий и узоров, у фона их нет.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.hist = None
        self.samples = 0
        self.mask_small = None      # где на уменьшенной картинке фон (1) / объект (0)
        self.last_dist = None

    def update(self, hsv, mask, rate=0.12):
        """Обновить гистограмму фона по пикселям mask (255 — фон)."""
        if mask is not None and cv2.countNonZero(mask) < 50:
            return
        h = cv2.calcHist([hsv], [0, 1], mask, [30, 32], [0, 180, 0, 256])
        cv2.normalize(h, h, 0, 255, cv2.NORM_MINMAX)
        self.hist = h if self.hist is None else (1 - rate) * self.hist + rate * h
        self.samples += 1

    def foreground(self, small, hsv, thr_pct, tex_thr):
        """255 там, где пиксель — объект (деталь, столб), а не фон."""
        H, W = small.shape[:2]
        sw = 96
        sh = max(8, int(round(H * sw / W)))
        lab_s = cv2.cvtColor(cv2.resize(small, (sw, sh), interpolation=cv2.INTER_AREA),
                             cv2.COLOR_BGR2LAB).astype(np.float32)
        lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        tex = cv2.blur(np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3)), (5, 5))
        k = max(5, sw // 6) | 1
        if self.mask_small is None or self.mask_small.shape != (sh, sw):
            self.mask_small = np.ones((sh, sw), np.float32)
            iters = 3                                  # первый кадр — несколько уточнений
        else:
            iters = 1
        k3, k5, k7 = (np.ones((n, n), np.uint8) for n in (3, 5, 7))
        for _ in range(iters):
            m = self.mask_small
            num = cv2.blur(lab_s * m[..., None], (k, k))
            den = cv2.blur(m, (k, k))[..., None]
            bg_s = num / np.maximum(den, 1e-3)
            bg = cv2.resize(bg_s, (W, H), interpolation=cv2.INTER_LINEAR)
            d = lab - bg
            dist = np.sqrt(d[..., 0] ** 2 + 2.0 * d[..., 1] ** 2 + 2.0 * d[..., 2] ** 2)
            obj = (dist > thr_pct * 2.0) | (tex > tex_thr)
            if self.hist is not None:                  # цвет типичен для фона и нет узора
                bp = cv2.calcBackProject([hsv], [0, 1], self.hist.astype(np.float32),
                                         [0, 180, 0, 256], 1)
                obj &= ~((bp > 200) & (tex < tex_thr * 0.5) & (dist < thr_pct * 4.0))
            fg = obj.astype(np.uint8) * 255
            fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k3)
            fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k5)
            small_obj = cv2.resize(cv2.dilate(fg, k7), (sw, sh), interpolation=cv2.INTER_AREA)
            self.mask_small = (small_obj < 60).astype(np.float32)
        self.last_dist = dist
        return fg


# =============================================================================
# Библиотека деталей: шаблоны, искажения
# =============================================================================
def _norm_gray(bgr):
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (TPL_W, TPL_H), interpolation=cv2.INTER_AREA)
    return cv2.equalizeHist(g)


def _hs_hist(bgr_list):
    h = None
    for b in bgr_list:
        hsv = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
        x = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        h = x if h is None else h + x
    cv2.normalize(h, h, 1, 0, cv2.NORM_L1)
    return h.astype(np.float32)


def _ncc(a, b):
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0])


class FlightSignature:
    """Признаки одной детали за пролёт (по серии снимков)."""

    def __init__(self, crops, sizes):
        self.crops = crops
        grays = [_norm_gray(c) for c in crops]
        self.template = np.median(np.stack(grays), axis=0).astype(np.uint8)
        self.hist = _hs_hist(crops)
        self.w = float(np.median([s[0] for s in sizes]))   # отн. ширина (доля кадра)
        self.h = float(np.median([s[1] for s in sizes]))   # отн. высота
        # «постоянство» признаков: насколько снимки похожи на общий шаблон
        self.stability = float(np.mean([_ncc(self.template, g) for g in grays]))


class DetailLibrary:
    """Типы деталей. Каждый тип: шаблон полной детали, гистограмма, размеры и выученные
    искажения (обрезанные/суженные варианты после неточной укладки)."""

    def __init__(self, db_dir, log):
        self.dir = Path(db_dir)
        self.log = log
        self.types = []
        self._load()

    # ---------------------------------------------------------------- файлы
    def _load(self):
        meta = self.dir / "library.json"
        if not meta.exists():
            return
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            for t in data.get("types", []):
                tpl = imread(self.dir / "templates" / f"type_{t['id']:02d}.png",
                             cv2.IMREAD_GRAYSCALE)
                if tpl is None:
                    continue
                t["template"] = tpl.astype(np.float32)
                t["hist"] = np.array(t["hist"], np.float32).reshape(16, 16)
                variants = []
                for v in t.get("variants", []):
                    img = imread(self.dir / "templates" / v["file"], cv2.IMREAD_GRAYSCALE)
                    if img is not None:
                        v["template"] = img.astype(np.float32)
                        variants.append(v)
                t["variants"] = variants
                self.types.append(t)
        except Exception as exc:
            self.log(f"[ИИ] Не удалось прочитать библиотеку деталей: {exc}")

    def save(self):
        tdir = self.dir / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        out = []
        for t in self.types:
            imwrite(tdir / f"type_{t['id']:02d}.png", t["template"].astype(np.uint8))
            vs = []
            for k, v in enumerate(t["variants"]):
                v["file"] = f"type_{t['id']:02d}_var{k:02d}.png"
                imwrite(tdir / v["file"], v["template"].astype(np.uint8))
                vs.append({"ratio": v["ratio"], "count": v["count"], "file": v["file"],
                           "side": v.get("side", "")})
            out.append({"id": t["id"], "count": t["count"], "w": t["w"], "h": t["h"],
                        "hist": t["hist"].reshape(-1).tolist(), "variants": vs,
                        "created": t.get("created", "")})
        tmp = self.dir / "library.json.tmp"
        tmp.write_text(json.dumps({"types": out}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "library.json")

    # ---------------------------------------------------------------- сравнение
    def _compare(self, t, sig):
        hist_sim = 1.0 - float(cv2.compareHist(t["hist"], sig.hist, cv2.HISTCMP_BHATTACHARYYA))
        h_sim = min(t["h"], sig.h) / max(t["h"], sig.h, 1e-6)
        ratio = sig.w / max(t["w"], 1e-6)
        side = ""
        if ratio < 0.95:
            # Деталь уже полной: сравниваем с левой и правой частью шаблона
            cols = max(4, int(round(TPL_W * ratio)))
            left = cv2.resize(t["template"][:, :cols], (TPL_W, TPL_H))
            right = cv2.resize(t["template"][:, TPL_W - cols:], (TPL_W, TPL_H))
            nl, nr = _ncc(sig.template, left), _ncc(sig.template, right)
            ncc, side = (nl, "левая часть") if nl >= nr else (nr, "правая часть")
            for v in t["variants"]:               # выученные искажения
                if abs(v["ratio"] - ratio) < 0.1:
                    nv = _ncc(sig.template, v["template"])
                    if nv > ncc:
                        ncc, side = nv, v.get("side", "") or "выученное искажение"
        else:
            ncc = _ncc(sig.template, t["template"])
        score = 0.45 * hist_sim + 0.2 * h_sim + 0.35 * max(0.0, ncc)
        return score, ratio, side

    def classify(self, sig, match_thr, learn=True):
        """Возвращает (тип, ratio, описание). Новый тип создаётся, только если деталь не
        похожа ни на одну известную даже с учётом искажений."""
        best, best_score, best_ratio, best_side = None, -1.0, 1.0, ""
        for t in self.types:
            score, ratio, side = self._compare(t, sig)
            if score > best_score:
                best, best_score, best_ratio, best_side = t, score, ratio, side
        if best is not None and best_score >= match_thr:
            desc = f"тип {best['id']} (сходство {best_score * 100:.0f}%)"
            if best_ratio < 0.95:
                desc += f", искажение: ширина {best_ratio * 100:.0f}% ({best_side})"
            if learn:
                self._learn(best, sig, best_ratio, best_side)
            return best, best_ratio, desc
        if not learn:
            return None, 1.0, "неизвестная деталь"
        t = {"id": (max([x["id"] for x in self.types]) + 1) if self.types else 1,
             "template": sig.template.astype(np.float32), "hist": sig.hist.copy(),
             "w": sig.w, "h": sig.h, "count": 1, "variants": [],
             "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.types.append(t)
        return t, 1.0, f"НОВЫЙ тип {t['id']} (на известные похожа на {max(best_score, 0) * 100:.0f}%)"

    def _learn(self, t, sig, ratio, side):
        t["count"] += 1
        t["hist"] = (0.85 * t["hist"] + 0.15 * sig.hist).astype(np.float32)
        t["h"] = 0.85 * t["h"] + 0.15 * sig.h
        if ratio > 1.05:                       # раньше видели только обрезанную деталь
            t["w"] = sig.w
            t["template"] = sig.template.astype(np.float32)
        elif ratio >= 0.95:                    # полная деталь: уточняем шаблон
            t["w"] = 0.85 * t["w"] + 0.15 * sig.w
            t["template"] = 0.85 * t["template"] + 0.15 * sig.template.astype(np.float32)
        else:                                  # искажение: запоминаем как вариант типа
            for v in t["variants"]:
                if abs(v["ratio"] - ratio) < 0.05:
                    v["ratio"] = 0.8 * v["ratio"] + 0.2 * ratio
                    v["template"] = 0.8 * v["template"] + 0.2 * sig.template.astype(np.float32)
                    v["count"] += 1
                    return
            t["variants"].append({"ratio": ratio, "count": 1, "side": side,
                                  "template": sig.template.astype(np.float32)})
            t["variants"] = sorted(t["variants"], key=lambda v: -v["count"])[:12]


# =============================================================================
# Обучение тайминга (Reinforcement Learning)
# =============================================================================
class TimingAgent:
    """Контекстный бандит: контекст — скорость детали (корзина), действие — смещение
    момента клика. Плюс «поправка» bias по измеренной ошибке попадания."""

    ACTIONS = list(range(-60, 61, 10))         # мс; + клик раньше, − позже
    SPEED_BINS = [0.15, 0.3, 0.5, 0.8, 1.2]    # скорость в ширинах кадра в секунду

    def __init__(self):
        n = len(self.ACTIONS)
        nb = len(self.SPEED_BINS) + 1
        self.q = [[0.0] * n for _ in range(nb)]
        self.cnt = [[0] * n for _ in range(nb)]
        self.bias = [0.0] * nb
        self.bias_n = [0] * nb
        self.total = 0

    def bucket(self, speed_rel):
        for i, b in enumerate(self.SPEED_BINS):
            if speed_rel < b:
                return i
        return len(self.SPEED_BINS)

    def epsilon(self, explore_pct):
        return explore_pct / 100.0 / (1.0 + self.total / 60.0)   # со временем реже пробует

    def choose(self, b, explore_pct):
        zero = self.ACTIONS.index(0)
        if random.random() < self.epsilon(explore_pct):
            # пробуем соседние смещения (далеко не прыгаем)
            return max(0, min(len(self.ACTIONS) - 1, zero + random.randint(-3, 3)))
        qs = self.q[b]
        best = max(qs)
        cands = [i for i, v in enumerate(qs) if v >= best - 1e-9]
        return min(cands, key=lambda i: abs(i - zero))

    def offset_ms(self, b, a):
        return self._bias(b) + self.ACTIONS[a]

    def _bias(self, b):
        if self.bias_n[b]:
            return self.bias[b]
        known = [self.bias[i] for i in range(len(self.bias)) if self.bias_n[i]]
        return sum(known) / len(known) if known else 0.0   # новая корзина — среднее

    def update(self, b, a, reward, time_err_ms, rate_pct):
        alpha = rate_pct / 100.0
        self.total += 1
        self.cnt[b][a] += 1
        self.q[b][a] += alpha * (reward - self.q[b][a])
        for j in (a - 1, a + 1):                  # соседние смещения — слабее
            if 0 <= j < len(self.ACTIONS):
                self.q[b][j] += 0.3 * alpha * (reward - self.q[b][j])
        if time_err_ms is not None and abs(time_err_ms) < 600:
            if not self.bias_n[b]:
                self.bias[b] = self._bias(b)
            step = max(-80.0, min(80.0, 0.6 * time_err_ms))
            self.bias[b] = max(-300.0, min(300.0, self.bias[b] + step))
            self.bias_n[b] += 1
            if abs(step) > 15:                    # поправка большая -> оценки устарели
                self.q[b] = [v * 0.5 for v in self.q[b]]

    def to_json(self):
        return {"q": self.q, "cnt": self.cnt, "bias": self.bias, "bias_n": self.bias_n,
                "total": self.total, "actions": self.ACTIONS}

    def load(self, d):
        try:
            if d.get("actions") != self.ACTIONS:
                return
            self.q, self.cnt = d["q"], d["cnt"]
            self.bias, self.bias_n, self.total = d["bias"], d["bias_n"], int(d["total"])
        except Exception:
            self.__init__()


# =============================================================================
# Считывание очков (easyocr)
# =============================================================================
class ScoreReader:
    def __init__(self, log):
        self.log = log
        self.reader = None
        self.status = "не загружен"
        self.ready = False
        self._q = queue.Queue()
        self._loading = False
        self._thread = threading.Thread(target=self._loop, name="ScoreOCR", daemon=True)
        self._thread.start()

    def load_async(self):
        if self.ready or self._loading:
            return
        self._loading = True
        threading.Thread(target=self._load, name="OCRLoad", daemon=True).start()

    def _load(self):
        try:
            self.status = "загрузка easyocr…"
            import easyocr   # noqa: импорт тяжёлый, поэтому только здесь
            gpu = False
            try:
                import torch
                gpu = bool(torch.cuda.is_available())
            except Exception:
                pass
            self.status = "загрузка модели распознавания (первый раз — скачивание)…"
            self.reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
            self.ready = True
            self.status = f"готов ({'GPU' if gpu else 'CPU'})"
            self.log(f"[ИИ] easyocr готов к чтению очков ({'GPU' if gpu else 'CPU'}).")
        except ImportError:
            self.status = "easyocr не установлен"
            self.log("[ИИ] easyocr не установлен — оценка будет по геометрии укладки.")
        except Exception as exc:
            self.status = f"ошибка: {exc}"
            self.log(f"[ИИ] easyocr не запустился: {exc}")
        finally:
            self._loading = False

    def submit(self, bgr, callback):
        """Распознать число на картинке; callback(value or None) вызовется в потоке OCR."""
        if not self.ready:
            callback(None)
            return
        self._q.put((bgr, callback))

    def _loop(self):
        while True:
            bgr, cb = self._q.get()
            value = None
            try:
                value = self._read(bgr)
            except Exception as exc:
                self.status = f"ошибка чтения: {exc}"
            try:
                cb(value)
            except Exception:
                traceback.print_exc()

    def _read(self, bgr):
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        scale = max(1.0, 64.0 / max(1, g.shape[0]))
        g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        res = self.reader.readtext(g, allowlist="0123456789", detail=1, paragraph=False)
        best = None
        for _box, text, conf in res:
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits and conf > 0.3:
                # самое крупное и уверенное число
                if best is None or (len(digits), conf) > (len(best[0]), best[1]):
                    best = (digits, conf)
        return int(best[0]) if best else None


# =============================================================================
# База данных снимков
# =============================================================================
class Database:
    MAX_PER_DIR = 400

    def __init__(self, root):
        self.root = Path(root)
        for sub in ("blocks", "backgrounds", "towers", "templates", "flights"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._counts = {}

    def _can_write(self, d):
        n = self._counts.get(d)
        if n is None:
            n = sum(1 for _ in d.glob("*.png")) if d.exists() else 0
        if n >= self.MAX_PER_DIR:
            return False
        self._counts[d] = n + 1
        return True

    def save(self, sub, name, img):
        d = self.root / sub
        d.mkdir(parents=True, exist_ok=True)
        if img is None or img.size == 0 or not self._can_write(d):
            return
        imwrite(d / f"{name}.png", img)

    def load_json(self, name, default):
        f = self.root / name
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default
        except Exception:
            return default

    def save_json(self, name, data):
        f = self.root / name
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)


# =============================================================================
# Автопилот
# =============================================================================
class Autopilot(threading.Thread):
    def __init__(self, sender, log):
        super().__init__(name="AIAutopilot", daemon=True)
        self.sender = sender
        self._log = log
        self.cfg = load_config()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.running = threading.Event()
        self.db = Database(DB_DIR)
        self.library = DetailLibrary(DB_DIR, self.log)
        self.agent = TimingAgent()
        self.agent.load(self.db.load_json("rl_timing.json", {}))
        learned = self.db.load_json("learned.json", {})
        self.fall_time = float(learned.get("fall_time", 0.0))   # время падения, с
        self.drift_k = float(learned.get("drift_k", 0.0))       # снос при падении (0..1)
        self.totals = learned.get("totals", {"2": 0, "1": 0, "0": 0})
        self.ocr = ScoreReader(self.log)
        self.bg = BackgroundModel()
        self._preview = None
        self._stats = {}
        self._last_save = time.perf_counter()
        self._reset_runtime()
        self._set_state(ST_STOPPED)

    # ------------------------------------------------------------ интерфейс
    def log(self, msg):
        self._log(msg if msg.startswith("[ИИ]") else "[ИИ] " + msg)

    def set_config(self, cfg):
        with self._lock:
            self.cfg = dict(cfg)

    def get_stats(self):
        with self._lock:
            return dict(self._stats)

    def take_preview(self):
        with self._lock:
            item, self._preview = self._preview, None
            return item

    def start_pilot(self):
        if self.running.is_set():
            return
        self._reset_runtime()
        self.bg.reset()
        if self.cfg["ocr_on"]:
            self.ocr.load_async()
        self.running.set()
        self._set_state(ST_SEARCH)
        self.log("Автопилот запущен" + (" (пробный прогон, без кликов)"
                                        if self.cfg["dry_run"] else "") + ".")

    def stop_pilot(self):
        if not self.running.is_set():
            return
        self.running.clear()
        self._set_state(ST_STOPPED)
        self.persist()
        self.log("Автопилот остановлен. Обучение сохранено.")

    def reset_learning(self):
        self.agent = TimingAgent()
        self.fall_time, self.drift_k = 0.0, 0.0
        self.totals = {"2": 0, "1": 0, "0": 0}
        self.persist()
        self.log("Обучение тайминга сброшено (снимки и детали в базе остались).")

    def shutdown(self):
        self.running.clear()
        self._stop_evt.set()
        self.persist()

    def persist(self):
        try:
            self.db.save_json("rl_timing.json", self.agent.to_json())
            self.db.save_json("learned.json", {"fall_time": self.fall_time,
                                               "drift_k": self.drift_k, "totals": self.totals})
            self.library.save()
        except Exception as exc:
            self.log(f"Не удалось сохранить обучение: {exc}")

    # ------------------------------------------------------------ состояние
    def _reset_runtime(self):
        self.prev_small = None
        self.prev_t = 0.0
        self.tower = None          # dict: top, left, right, cx, contour, bbox
        self.tower_seen = 0.0
        self.last_block = None
        self.static_age = None
        self.track = collections.deque(maxlen=40)
        self.flight = None         # снимки текущей детали
        self.click = None          # данные о последнем клике
        self.last_click_t = 0.0
        self.lost_since = None
        self.score = None
        self.score_before = None
        self.pending_ocr = None
        self._score_req_t = 0.0
        self._score_busy = False
        self.flights = 0
        self.last_result = ""
        self.fps, self._frames, self._t_fps = 0.0, 0, time.perf_counter()
        self.last_err = ""
        self.speed = 0.0
        self.pred_x = None
        self.offset_now = 0.0

    def _set_state(self, st):
        self.state = st
        self.state_t = time.perf_counter()

    def _publish(self, extra=None):
        t = self.totals
        n = t["2"] + t["1"] + t["0"]
        s = {
            "state": self.state, "state_text": STATE_TEXT[self.state],
            "running": self.running.is_set(), "fps": self.fps, "error": self.last_err,
            "score": self.score, "last_result": self.last_result,
            "perfect": t["2"], "normal": t["1"], "miss": t["0"], "total": n,
            "accuracy": (t["2"] + t["1"]) * 100.0 / n if n else 0.0,
            "perfect_rate": t["2"] * 100.0 / n if n else 0.0,
            "tower": self.tower is not None, "speed": self.speed,
            "offset": self.offset_now, "fall_ms": self.fall_time * 1000.0,
            "drift_k": self.drift_k, "types": len(self.library.types),
            "explore": self.agent.epsilon(self.cfg["explore_pct"]) * 100.0,
            "learn_total": self.agent.total, "ocr": self.ocr.status,
            "flights": self.flights,
        }
        if extra:
            s.update(extra)
        with self._lock:
            self._stats = s

    # ------------------------------------------------------------ главный цикл
    def run(self):
        backend, backend_key = None, None
        while not self._stop_evt.is_set():
            if not self.running.is_set():
                if backend is not None:
                    backend.close()
                    backend, backend_key = None, None
                self._publish()
                self._stop_evt.wait(0.1)
                continue
            cfg = self.cfg
            try:
                if backend is None or backend_key != cfg["capture"]:
                    if backend is not None:
                        backend.close()
                    backend = (cm.DxcamBackend() if cfg["capture"] == "dxcam"
                               else cm.MssBackend())
                    backend_key = cfg["capture"]
                rx, ry, rw, rh = self._roi(cfg)
                frame = backend.grab(rx, ry, rw, rh)
                if frame is None:
                    self._stop_evt.wait(0.001)
                    continue
                now = time.perf_counter()
                self._step(frame, now, cfg, backend)
                self.last_err = ""
                if now - self._last_save > 60:
                    self._last_save = now
                    self.persist()
            except Exception as exc:
                msg = str(exc) if isinstance(exc, cm.BackendError) else \
                    f"{type(exc).__name__}: {exc}"
                if msg != self.last_err:
                    self.log(f"Ошибка: {msg}")
                    traceback.print_exc()
                self.last_err = msg
                self.prev_small = None
                if backend is not None:
                    backend.close()
                backend, backend_key = None, None
                self._publish()
                self._stop_evt.wait(0.2)
        if backend is not None:
            backend.close()

    @staticmethod
    def _roi(cfg):
        if cfg["roi_w"] < 20 or cfg["roi_h"] < 20:
            return _screen_rect()
        return cfg["roi_x"], cfg["roi_y"], cfg["roi_w"], cfg["roi_h"]

    def _score_rect(self, cfg):
        if cfg["score_w"] >= 8 and cfg["score_h"] >= 8:
            return cfg["score_x"], cfg["score_y"], cfg["score_w"], cfg["score_h"]
        rx, ry, rw, rh = self._roi(cfg)       # авто: верх по центру
        return rx + rw // 4, ry, rw // 2, max(20, rh * 18 // 100)

    # ------------------------------------------------------------ один кадр
    def _step(self, frame, now, cfg, backend):
        fh, fw = frame.shape[:2]
        W = int(cfg["work_width"])
        scale = W / float(fw)
        H = max(40, int(round(fh * scale)))
        small = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # ---- 1. Оптический поток (Farneback) на половинном разрешении ----
        g2 = cv2.resize(gray, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
        if self.prev_small is None or self.prev_small.shape != g2.shape:
            self.prev_small, self.prev_t = g2, now
            return
        dt = max(1e-3, now - self.prev_t)
        flow = cv2.calcOpticalFlowFarneback(self.prev_small, g2, None,
                                            0.5, 3, 13, 3, 5, 1.1, 0)
        self.prev_small, self.prev_t = g2, now
        fx, fy = flow[..., 0], flow[..., 1]
        gx, gy = float(np.median(fx[::3, ::3])), float(np.median(fy[::3, ::3]))
        rel_x, rel_y = fx - gx, fy - gy                # минус движение камеры
        mag = np.hypot(rel_x, rel_y)
        moving_s = (mag > cfg["motion_thr"] / 10.0).astype(np.uint8) * 255
        moving = cv2.resize(moving_s, (W, H), interpolation=cv2.INTER_NEAREST)
        moving = cv2.dilate(moving, np.ones((5, 5), np.uint8))

        # ---- 2. Фон (гистограммы) ----
        # Выборка фона: края кадра + кольцо вокруг столба. Исключаем только известные
        # объекты (столб, деталь) и движущиеся пиксели — так фон не «съедает» детали,
        # а светлые/тёмные участки неба (градиент) не выпадают из модели.
        if self._frames % 2 == 0 or self.bg.samples < 10:
            sample = np.zeros((H, W), np.uint8)
            sample[: H // 5, :] = 255
            sample[:, : W // 8] = 255
            sample[:, W - W // 8:] = 255
            t = self.tower
            if t is not None:
                x, y, w, h = t["bbox"]
                pad = max(6, w // 2)
                sample[max(0, y - 3 * pad):min(H, y + h), max(0, x - pad):min(W, x + w + pad)] = 255
                sample[max(0, y - 3):, max(0, x - 3):min(W, x + w + 3)] = 0   # сам столб
            lb = self.last_block
            if lb is not None:
                pad = max(lb["w"], lb["h"]) // 2 + 4
                sample[max(0, lb["y"] - pad):lb["y"] + lb["h"] + pad,
                       max(0, lb["x"] - pad):lb["x"] + lb["w"] + pad] = 0
            # давно неподвижные объекты вдали от столба (облака, табло, декор) — это фон
            if self.static_age is not None and self.static_age.shape == (H, W):
                old = self.static_age > max(60, int(self.fps * 3))
                if t is not None:
                    x, y, w, h = t["bbox"]
                    old[max(0, y - 3):, max(0, x - 3):min(W, x + w + 3)] = False
                sample[old] = 255
            sample[moving > 0] = 0
            self.bg.update(hsv, sample, rate=0.3 if self.bg.samples < 10 else 0.04)
        fg = self.bg.foreground(small, hsv, cfg["bg_thr"], cfg["tex_thr"])
        still = (fg > 0) & (moving == 0)
        if self.static_age is None or self.static_age.shape != (H, W):
            self.static_age = np.zeros((H, W), np.uint16)
        self.static_age = np.where(still, np.minimum(self.static_age + 1, 60000), 0
                                   ).astype(np.uint16)
        if self.tower is not None:
            # давно неподвижный декор вдали от столба (облака, табло, кнопки) — не объект
            decor = self.static_age > max(90, int(self.fps * 3))
            t = self.tower
            pad = max(8, t["width"] // 2)
            decor[max(0, t["top"] - 6):, max(0, t["left"] - pad):min(W, t["right"] + pad)] = False
            fg[decor] = 0

        # ---- 3. Столб: неподвижный объект, касающийся низа кадра ----
        static = cv2.bitwise_and(fg, cv2.bitwise_not(moving))
        if self.state != ST_DROP or self.tower is None:
            tower = self._find_tower(static, W, H)
            if tower is not None:
                self.tower = tower
                self.tower_seen = now
            elif self.state in (ST_WAIT, ST_SEARCH) and now - self.tower_seen > 0.5:
                self.tower = None                   # столб пропал надолго, а не на кадр
        else:
            tower = self._find_tower(static, W, H)     # во время падения — только для оценки

        # ---- 4. Деталь: движущийся объект над столбом ----
        block = self._find_block(fg, moving, rel_x, W, H, dt)
        if block is not None or (self.lost_since and now - self.lost_since > 1.0):
            self.last_block = block
        if block is not None:
            self.track.append((now, block["cx"], block["cy"], block["w"], block["h"]))
            self.lost_since = None
        elif self.lost_since is None:
            self.lost_since = now
        while self.track and now - self.track[0][0] > 0.6:
            self.track.popleft()

        model = self._motion_model(now, block)
        self.speed = abs(model[1]) / W if model else 0.0

        # ---- 5. Автомат состояний ----
        self._state_machine(now, cfg, small, frame, scale, block, model, tower, W, H, backend)

        # ---- 6. Статистика и предпросмотр ----
        self._frames += 1
        if now - self._t_fps >= 0.5:
            self.fps = self._frames / (now - self._t_fps)
            self._frames, self._t_fps = 0, now
        self._publish()
        if cfg["show_preview"] and self._frames % 2 == 0:
            vis = self._draw(small, block, model, W, H, cfg)
            with self._lock:
                self._preview = (vis, fg, mag, moving)

    # ------------------------------------------------------------ поиск столба
    def _find_tower(self, static, W, H):
        n, labels, stats, _ = cv2.connectedComponentsWithStats(static, connectivity=8)
        prev = self.tower
        best, best_score = 0, 0.0
        for k in range(1, n):
            x, y, w, h, area = stats[k]
            bottom = y + h
            # столб стоит внизу кадра (на краю или на платформе/земле, которая слилась с фоном)
            if bottom < 0.5 * H or area < 0.002 * W * H or h < 3:
                continue
            centre = 1.0 - 0.5 * abs((x + w / 2.0) - W / 2.0) / (W / 2.0)
            score = area * (bottom / H) ** 3 * centre
            if bottom >= H - 2:
                score *= 1.5
            if w > 4 * h:                              # плоское и широкое: кнопка, полоса
                score *= 0.15
            if prev is not None:                       # тот же столб, что и раньше
                px, py, pw, ph = prev["bbox"]
                if x < px + pw and px < x + w and y < py + ph + 4 and py < y + h + 4:
                    score *= 3.0
            if score > best_score:
                best, best_score = k, score
        if not best:
            return None
        x, y, w, h, _ = stats[best]
        comp = (labels[y:y + h, x:x + w] == best)
        # Профиль верхней поверхности: для каждого столбца — самый верхний пиксель объекта.
        # Столб — самый высокий участок (так он находится, даже если слился с землёй).
        has = comp.any(axis=0)
        top_col = np.where(has, comp.argmax(axis=0), h).astype(np.int32)
        peak = int(top_col[has].min()) if has.any() else 0
        band = max(3, int(H * 0.03))
        near = top_col <= peak + band
        # непрерывный участок вокруг самой высокой точки (с допуском на щели текстуры)
        cols = np.nonzero(near)[0]
        centre_col = int(np.argmin(np.where(has, top_col, h + 1)))
        gap = max(2, W // 100)
        left_c = right_c = centre_col
        for c in cols[cols < centre_col][::-1]:
            if left_c - c > gap:
                break
            left_c = c
        for c in cols[cols > centre_col]:
            if c - right_c > gap:
                break
            right_c = c
        top = peak + y
        left, right = int(left_c) + x, int(right_c) + x
        cnts, _ = cv2.findContours(comp.astype(np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        contour = max(cnts, key=cv2.contourArea) + np.array([[x, y]]) if cnts else None
        if prev is not None and abs(prev["top"] - top) <= 2:    # сглаживание дрожания
            left = int(round(0.6 * prev["left"] + 0.4 * left))
            right = int(round(0.6 * prev["right"] + 0.4 * right))
        return {"top": top, "left": left, "right": right, "cx": (left + right) / 2.0,
                "width": max(1, right - left), "contour": contour,
                "bbox": (left, top, max(1, right - left), max(1, y + h - top))}

    # ------------------------------------------------------------ поиск детали
    def _find_block(self, fg, moving, rel_x, W, H, dt):
        area_min = self.cfg["min_block"] / 1000.0 * W * H
        limit = H
        if self.tower is not None:
            limit = self.tower["top"] - 2           # деталь — только над столбом
            if self.state == ST_DROP:
                limit = H                           # падающая деталь может быть ниже
        if limit < 5:
            return None
        last = self.track[-1] if self.track else None
        # Два набора кандидатов: объекты «не фон» и «не фон И движется». Второй отделяет
        # деталь, если она на кадре слилась с неподвижным объектом (облако, декор).
        fg_lim = fg[:limit]
        mov_fg = cv2.bitwise_and(fg_lim, moving[:limit])
        best, best_score = None, -1e9
        for mask in (fg_lim, mov_fg):
            n, labels, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
            for k in range(1, n):
                x, y, w, h, area = stats[k]
                if area < area_min or area > 0.3 * W * H or w > 0.9 * W:
                    continue
                m = moving[y:y + h, x:x + w][labels[y:y + h, x:x + w] == k]
                motion = float(np.mean(m > 0)) if m.size else 0.0
                score = motion * 2.0
                if last is not None:
                    lw, lh = last[3], last[4]
                    size_err = max(abs(w - lw) / max(lw, 1), abs(h - lh) / max(lh, 1))
                    if size_err > 0.6 and motion < 0.5:
                        continue                    # не похоже по размеру на нашу деталь
                    d = math.hypot(cent[k][0] - last[1], cent[k][1] - last[2]) / W
                    score += 1.0 - min(1.0, d * 4)
                    score += 0.8 * (1.0 - min(1.0, size_err))
                elif motion < 0.25:
                    continue                        # новая деталь должна двигаться
                if score > best_score:
                    best_score = score
                    best = (int(x), int(y), int(w), int(h),
                            float(cent[k][0]), float(cent[k][1]))
        if best is None:
            return None
        x, y, w, h, cx, cy = best
        # скорость по оптическому потоку (поток в половинном разрешении)
        x2, y2 = x // 2, y // 2
        sub = rel_x[y2:max(y2 + 1, (y + h) // 2), x2:max(x2 + 1, (x + w) // 2)]
        vflow = float(np.median(sub)) * 2.0 / dt if sub.size else 0.0
        return {"x": x, "y": y, "w": w, "h": h, "cx": cx, "cy": cy, "vflow": vflow}

    def _motion_model(self, now, block):
        """(x0, v, a) — положение, скорость (пикс/с) и ускорение по истории + поток."""
        if len(self.track) < 3:
            if block is not None and abs(block["vflow"]) > 1:
                return block["cx"], block["vflow"], 0.0
            return None
        t = np.array([p[0] - now for p in self.track])
        x = np.array([p[1] for p in self.track])
        if t[-1] - t[0] < 0.04:
            return None
        deg = 2 if len(t) >= 8 else 1
        coef = np.polyfit(t, x, deg)
        if deg == 2:
            a, v, x0 = 2 * coef[0], coef[1], coef[2]
        else:
            a, v, x0 = 0.0, coef[0], coef[1]
        if block is not None and abs(block["vflow"]) > 1:
            v = 0.6 * v + 0.4 * block["vflow"]        # смешиваем с оптическим потоком
        a = max(-8000.0, min(8000.0, a))
        return float(x0), float(v), float(a)

    # ------------------------------------------------------------ состояния
    def _state_machine(self, now, cfg, small, frame, scale, block, model, tower, W, H, backend):
        st = self.state
        if st == ST_SEARCH:
            if self.tower is not None:
                self.log(f"Столб найден: верх на {self.tower['top']} пикс, ширина "
                         f"{self.tower['width']} пикс (в рабочем масштабе).")
                self._set_state(ST_WAIT)
                self._read_score_async(backend, cfg, before=True)
            return

        if st == ST_WAIT:
            if self.tower is None:
                self._set_state(ST_SEARCH)
                return
            # базовый счёт: в начале и время от времени, пока деталь не летит
            if (cfg["ocr_on"] and self.ocr.ready and not self._score_busy
                    and (self.score_before is None or now - self._score_req_t > 2.0)):
                self._read_score_async(backend, cfg, before=True)
            if (block is not None and model is not None and abs(model[1]) > W * 0.05
                    and now - self.last_click_t > cfg["cooldown_ms"] / 1000.0):
                self.flight = {"t0": now, "crops": [], "sizes": [], "last": 0.0}
                self._set_state(ST_TRACK)
            return

        if st == ST_TRACK:
            if self.tower is None:
                self._set_state(ST_SEARCH)
                return
            if block is None:
                if self.lost_since and now - self.lost_since > 0.4:
                    self._finish_flight(None)
                    self._set_state(ST_WAIT)
                return
            if model is not None and abs(model[1]) > W * 0.03:
                self.flight["moved"] = now
            if now - self.flight.get("moved", self.flight["t0"]) > 1.0:
                self.log("Деталь потеряна (объект перестал двигаться) — жду новую.")
                self.flight = None
                self.track.clear()
                self._set_state(ST_WAIT)
                return
            self._collect_crop(now, frame, block, scale, W, H)
            if model is None:
                return
            self._decide(now, cfg, block, model, W)
            return

        if st == ST_DROP:
            c = self.click
            elapsed = now - c["t_fire"]
            landed = False
            if tower is not None and elapsed > 0.05:
                rise = c["top"] - tower["top"]
                # изометрические коробки: столб растёт только на толщину коробки
                if rise >= max(2.0, 0.15 * c["bh"]):
                    landed = True
            if c["dry"]:
                if elapsed > 0.25:
                    self._set_state(ST_WAIT)
                    self.track.clear()
                return
            if landed or elapsed > cfg["settle_ms"] / 1000.0:
                self._evaluate(now, cfg, tower if landed else None, small, backend)
            return

        if st == ST_EVAL:
            pend = self.pending_ocr
            if pend is not None and (pend["done"] or now - pend["t"] > 3.0):
                self._apply_result(pend)
                self.pending_ocr = None
                self._set_state(ST_WAIT)
                self.track.clear()

    def _collect_crop(self, now, frame, block, scale, W, H):
        f = self.flight
        if f is None or len(f["crops"]) >= 10 or now - f["last"] < 0.06:
            return
        inv = 1.0 / scale
        x0, y0 = int(block["x"] * inv), int(block["y"] * inv)
        x1, y1 = int((block["x"] + block["w"]) * inv), int((block["y"] + block["h"]) * inv)
        crop = frame[max(0, y0):y1, max(0, x0):x1]
        if crop.size and crop.shape[0] >= 4 and crop.shape[1] >= 4:
            f["crops"].append(crop.copy())
            f["sizes"].append((block["w"] / W, block["h"] / H))
            f["last"] = now

    def _finish_flight(self, click):
        """Пролёт окончен: строим шаблон по серии снимков и узнаём деталь."""
        f, self.flight = self.flight, None
        if f is None or len(f["crops"]) < 3:
            return ""
        cfg = self.cfg
        try:
            sig = FlightSignature(f["crops"], f["sizes"])
            t, ratio, desc = self.library.classify(sig, cfg["match_pct"] / 100.0,
                                                   learn=cfg["learn_on"])
        except Exception as exc:
            return f"не удалось разобрать деталь: {exc}"
        if cfg["save_db"] and t is not None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            sub = f"blocks/type_{t['id']:02d}" + ("/distorted" if ratio < 0.95 else "")
            for k, crop in enumerate(f["crops"][::3]):
                self.db.save(sub, f"{stamp}_{k}", crop)
        return desc

    def _decide(self, now, cfg, block, model, W):
        """Когда кликать: прогноз точки падения совпадает с центром столба."""
        x0, v, a = model
        tower = self.tower
        if block["y"] + block["h"] > tower["top"] + 2:
            return
        target = tower["cx"]
        b = self.agent.bucket(abs(v) / W)
        if "action" not in self.flight:
            self.flight["action"] = self.agent.choose(b, cfg["explore_pct"])
        act = self.flight["action"]
        lead = (cfg["latency_ms"] + cfg["lead_ms"] + self.agent.offset_ms(b, act)) / 1000.0
        self.offset_now = self.agent.offset_ms(b, act) + cfg["lead_ms"]
        tf = self.fall_time if self.fall_time > 0 else 0.0
        ts = np.arange(0.0, 1.5, 0.002)
        xs = x0 + v * ts + 0.5 * a * ts * ts
        vs = v + a * ts
        f = xs + self.drift_k * vs * tf - target
        self.pred_x = float(x0 + self.drift_k * v * tf)
        sign0 = np.sign(f[0])
        cross = np.nonzero((np.sign(f) != sign0) | (np.abs(f) < 0.5))[0]
        if not len(cross):
            return
        tau = float(ts[cross[0]])                    # через сколько секунд совпадёт
        frame_dt = 1.0 / max(self.fps, 20.0)
        # Клик нужен за lead секунд до совпадения (lead < 0 — после совпадения).
        # Решаем, когда до клика или до совпадения осталось меньше кадра.
        if tau - lead > frame_dt * 0.75 and tau > frame_dt * 0.75:
            return                                   # ещё рано — решим на следующем кадре
        delay = max(0.0, tau - lead)
        self._fire(now, delay, cfg, block, (x0, v, a), target, b, act)

    def _fire(self, now, delay, cfg, block, model, target, b, act):
        x0, v, a = model
        t_fire = now + delay
        dry = bool(cfg["dry_run"])
        self.click = {"t_fire": t_fire, "x_at": x0 + v * delay + 0.5 * a * delay * delay,
                      "v": v, "target": target, "top": self.tower["top"],
                      "left": self.tower["left"], "right": self.tower["right"],
                      "bw": block["w"], "bh": block["h"], "bucket": b, "action": act,
                      "offset": self.agent.offset_ms(b, act), "dry": dry}
        self.last_click_t = t_fire
        note = (f"ИИ: деталь {abs(v):.0f} пикс/с, смещение "
                f"{self.click['offset'] + cfg['lead_ms']:+.0f} мс")
        if dry:
            self.log(f"[пробный прогон] клик был бы через {delay * 1000:.0f} мс ({note}).")
        else:
            def send():
                if self.sender is None or not self.sender.request(
                        cfg["key"], int(cfg["hold_ms"]), time.perf_counter(), 0.0, note):
                    self.log("Клик не отправлен: Interception недоступен или занят.")
            if delay > 0.0015:
                t_target = time.perf_counter() + delay

                def precise():                       # точнее threading.Timer в Windows
                    while True:
                        r = t_target - time.perf_counter()
                        if r <= 0:
                            break
                        if r > 0.003:
                            time.sleep(r - 0.002)
                    send()
                threading.Thread(target=precise, name="AIClick", daemon=True).start()
            else:
                send()
        self.click["desc"] = self._finish_flight(self.click)
        self._set_state(ST_DROP)

    # ------------------------------------------------------------ оценка
    def _evaluate(self, now, cfg, tower, small, backend):
        c = self.click
        res = {"t": now, "done": False, "ocr": None, "click": c}
        if tower is None:                            # столб не вырос — деталь упала мимо
            res.update(geo=0, err_px=None, overlap=0.0)
        else:
            fall = now - c["t_fire"]
            self.fall_time = fall if self.fall_time <= 0 else 0.8 * self.fall_time + 0.2 * fall
            landed_cx = tower["cx"]
            err = landed_cx - c["target"]
            old_w = c["right"] - c["left"]
            if tower["width"] < old_w - 2 and abs(err) > 0.5:
                # деталь обрезалась: видна только общая часть, её центр сдвинут на половину
                # ошибки. Настоящая ошибка = насколько уменьшилась ширина.
                err = (1 if err > 0 else -1) * float(old_w - tower["width"])
            inter = max(0, min(tower["right"], c["right"]) - max(tower["left"], c["left"]))
            overlap = inter / max(1.0, c["bw"])
            # снос детали во время падения
            travel = c["v"] * max(fall, 1e-3)
            if abs(travel) > 3:
                k = ((c["target"] + err) - c["x_at"]) / travel
                self.drift_k = max(0.0, min(1.0, 0.85 * self.drift_k + 0.15 * k))
            perfect = abs(err) <= cfg["perfect_pct"] / 100.0 * max(c["bw"], 1)
            res.update(geo=2 if perfect else 1, err_px=err, overlap=overlap)
            if cfg["save_db"]:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                H, W = small.shape[:2]
                y0 = max(0, tower["top"] - c["bh"] * 2)
                self.db.save("towers", f"{stamp}", small[y0:min(H, tower["top"] + c["bh"] * 2),
                             max(0, tower["left"] - 10):min(W, tower["right"] + 10)])
                if self.flights % 5 == 0:
                    bgm = small.copy()
                    if self.bg.last_dist is not None and self.bg.last_dist.shape == (H, W):
                        bgm[self.bg.last_dist > cfg["bg_thr"] * 2.0] = 0
                    self.db.save("backgrounds", f"{stamp}", bgm)
        self.pending_ocr = res
        self._set_state(ST_EVAL)
        if cfg["ocr_on"] and self.ocr.ready and self.score_before is not None:
            self._read_score_async(backend, cfg, before=False, res=res)
        else:
            res["done"] = True

    def _read_score_async(self, backend, cfg, before, res=None):
        """Снимок области счёта (всегда через mss: dxcam может не отдать новый кадр)."""
        if not (cfg["ocr_on"] and self.ocr.ready):
            if res is not None:
                res["done"] = True
            return
        try:
            if getattr(self, "_score_sct", None) is None:
                self._score_sct = cm.MssBackend()
            sx, sy, sw, sh = self._score_rect(cfg)
            img = self._score_sct.grab(sx, sy, sw, sh)
        except Exception:
            img = None
            self._score_sct = None
        if img is None:
            if res is not None:
                res["done"] = True
            return
        t_req = time.perf_counter()
        self._score_req_t = t_req
        self._score_busy = True

        def done(value):
            self._score_busy = False
            if before:
                # если за время чтения успели кликнуть — значение уже не «до клика»
                if value is not None and self.last_click_t < t_req:
                    self.score = value
                    self.score_before = value
            else:
                if value is not None:
                    self.score = value
                if res is not None:
                    res["ocr"] = value
                    res["done"] = True
        self.ocr.submit(img, done)

    def _apply_result(self, res):
        c = res["click"]
        cfg = self.cfg
        outcome, source = res["geo"], "геометрия"
        if res["ocr"] is not None and self.score_before is not None:
            delta = res["ocr"] - self.score_before
            if delta in (0, 1, 2):
                outcome, source = delta, "очки"
            self.score_before = res["ocr"]
        reward = {2: 1.0, 1: 0.3, 0: -1.0}[outcome]
        time_err = None
        if res["err_px"] is not None and abs(c["v"]) > 1:
            late_px = res["err_px"] * (1 if c["v"] > 0 else -1)   # >0 — кликнули поздно
            time_err = late_px / abs(c["v"]) * 1000.0
        if cfg["learn_on"]:
            self.agent.update(c["bucket"], c["action"], reward, time_err, cfg["learn_rate"])
        self.totals[str(outcome)] += 1
        self.flights += 1
        name = {2: "+2 ИДЕАЛ", 1: "+1 обычно", 0: "0 ПРОМАХ"}[outcome]
        self.last_result = name
        parts = [f"Деталь №{self.flights}: {name} (по {source})"]
        if res["err_px"] is not None:
            parts.append(f"ошибка {res['err_px']:+.1f} пикс")
        if time_err is not None:
            parts.append(f"≈{time_err:+.0f} мс")
        parts.append(f"смещение {c['offset'] + cfg['lead_ms']:+.0f} мс")
        if c.get("desc"):
            parts.append(c["desc"])
        self.log(" | ".join(parts))

    # ------------------------------------------------------------ картинка
    def _draw(self, small, block, model, W, H, cfg):
        vis = small.copy()
        t = self.tower
        if t is not None:
            if t["contour"] is not None:
                cv2.drawContours(vis, [t["contour"]], -1, (80, 220, 80), 1)
            zh = int(H * cfg["zone_h"] / 100.0)
            cv2.rectangle(vis, (t["left"], max(0, t["top"] - zh)), (t["right"], t["top"]),
                          (255, 200, 0), 1)
            cx = int(t["cx"])
            cv2.line(vis, (cx, 0), (cx, t["top"]), (255, 200, 0), 1)
            cv2.putText(vis, "target", (min(W - 50, cx + 3), 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 200, 0), 1)
        if block is not None:
            cv2.rectangle(vis, (block["x"], block["y"]),
                          (block["x"] + block["w"], block["y"] + block["h"]), (0, 220, 255), 2)
            if model is not None:
                x0, v, _a = model
                y = int(block["cy"])
                cv2.arrowedLine(vis, (int(x0), y), (int(x0 + v * 0.25), y), (0, 120, 255), 2,
                                tipLength=0.3)
        if self.pred_x is not None and self.state == ST_TRACK:
            px = int(self.pred_x)
            cv2.line(vis, (px, 0), (px, H), (255, 0, 255), 1)
        for p in list(self.track):
            cv2.circle(vis, (int(p[1]), int(p[2])), 1, (0, 255, 255), -1)
        return vis
