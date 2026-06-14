#!/usr/bin/env python3
"""
Iron Pride Invader Q9MX — нативный Linux редактор/клиент дисплея.

Слои: картинки, GIF, видео, текст (с радугой), системные датчики.
GIF/видео крутятся через GUI-таймер (без гонок потоков и статики после загрузки).
Темы: сохраняй раскладку под именем и переключай.
Поток: холст 1920x462 -> поворот 462x1920 -> ffmpeg libx264 -> 0x85 на /dev/ttyACM0.

Зависимости: python3-pyqt6, pyserial, pillow, psutil, ffmpeg
Запуск: python3 ironpride_editor.py   |   фоновый: --background
"""
import sys, os, json, math, glob, struct, time, threading, subprocess, queue
import urllib.request

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGraphicsView, QGraphicsScene,
    QGraphicsObject, QGraphicsItem, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QListWidget, QListWidgetItem, QSlider, QLabel, QFileDialog,
    QFrame, QCheckBox, QSystemTrayIcon, QMenu, QLineEdit, QComboBox, QColorDialog,
    QInputDialog,
)
from PyQt6.QtGui import (QPixmap, QImage, QImageReader, QPainter, QPen, QColor,
                         QBrush, QIcon, QFont, QFontMetrics, QLinearGradient, QDesktopServices)
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, QSettings, QUrl
from PIL import Image, ImageOps
import serial
import psutil

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink
    HAS_VIDEO = True
except Exception:
    HAS_VIDEO = False

PANEL_W, PANEL_H = 1920, 462
ENC_W, ENC_H     = 462, 1920
PORT = "/dev/ttyACM0"
BAUD = 2000000
FPS  = 15

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
VID_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")

CFG_DIR    = os.path.expanduser("~/.config/ironpride")
PROJ_FILE  = os.path.join(CFG_DIR, "project.json")
THEMES_DIR = os.path.join(CFG_DIR, "themes")
AUTOSTART  = os.path.expanduser("~/.config/autostart/ironpride.desktop")

MAGIC = bytes([0x5A, 0xA5])
def cmd(c, payload):
    return MAGIC + bytes([c, 0x00]) + struct.pack("<I", len(payload)) + payload


def kind_of(path):
    e = os.path.splitext(path)[1].lower()
    if e == ".gif": return "gif"
    if e in VID_EXT: return "video"
    return "img"


def _ri(p):
    try: return int(open(p).read().strip())
    except Exception: return None

def _gpu_busy():
    for p in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        v = _ri(p)
        if v is not None: return v
    return 0

def _gpu_temp():
    for p in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"):
        v = _ri(p)
        if v is not None: return v // 1000
    return 0

def _cpu_temp():
    try:
        t = psutil.sensors_temperatures()
        for k in ("k10temp", "zenpower", "coretemp"):
            if k in t and t[k]:
                return int(t[k][0].current)
    except Exception:
        pass
    return 0

def read_sensors():
    return {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent,
            "gpu": _gpu_busy(), "cput": _cpu_temp(), "gput": _gpu_temp()}


class ResizableItem(QGraphicsObject):
    HS = 26.0
    render_clean = False

    def __init__(self, kind, path=None, metric=None, label=None):
        super().__init__()
        self.kind = kind
        self.path = path
        self.metric = metric
        self.metric_label = label or ""
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.player = None
        self._pix = QPixmap()
        self.opacity_pct = 100
        self.spin = False
        self.spin_speed = 0.0
        self.media_speed = 100
        self.text = "Текст"; self.font_size = 64
        self.color = QColor(255, 255, 255); self.rainbow = False; self._hue = 0
        self._val = 0
        # анимация
        self._frames = []; self._durs = []; self._fidx = 0; self._facc = 0.0
        self._pending = None     # QImage от видео-потока, заберёт GUI-таймер

        if kind == "gif":
            self._load_gif(path)
        elif kind == "video" and HAS_VIDEO:
            self.sink = QVideoSink()
            self.player = QMediaPlayer()
            self.player.setVideoSink(self.sink)
            self.sink.videoFrameChanged.connect(self._vid_frame)
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.setLoops(QMediaPlayer.Loops.Infinite)
            QTimer.singleShot(0, self._start_media)
        elif kind == "text":
            self.render_text()
        elif kind == "stat":
            self.render_stat()
        else:
            self._pix = QPixmap(path)

        if self._pix.isNull():
            self._pix = QPixmap(320, 180); self._pix.fill(QColor(60, 60, 60))
        self._w = float(self._pix.width()); self._h = float(self._pix.height())
        self._sized = (kind != "video")
        self.setTransformOriginPoint(self._w / 2, self._h / 2)
        self._mode = None; self._fixed = QPointF(); self._rot_ref = 0.0

    # ---- GIF: предзагрузка кадров ----
    def _load_gif(self, path):
        r = QImageReader(path)
        self._frames, self._durs = [], []
        while True:
            img = r.read()
            if img.isNull(): break
            self._frames.append(QPixmap.fromImage(img))
            d = r.nextImageDelay()
            self._durs.append(d if d > 0 else 80)
        if not self._frames:
            pm = QPixmap(path)
            self._frames = [pm if not pm.isNull() else QPixmap(200, 200)]
            self._durs = [100]
        self._pix = self._frames[0]

    def tick_anim(self, dt):
        # вызывается из GUI-таймера редактора
        if self.kind == "gif" and len(self._frames) > 1:
            spd = max(0.05, self.media_speed / 100.0)
            self._facc += dt * 1000.0 * spd
            steps = 0
            while self._facc >= self._durs[self._fidx] and steps < 1000:
                self._facc -= self._durs[self._fidx]
                self._fidx = (self._fidx + 1) % len(self._frames)
                steps += 1
            self._pix = self._frames[self._fidx]
            self.update()
        elif self.kind == "video" and self._pending is not None:
            img = self._pending; self._pending = None
            self._pix = QPixmap.fromImage(img)
            if not self._sized:
                self._w = float(self._pix.width()); self._h = float(self._pix.height())
                self.setTransformOriginPoint(self._w / 2, self._h / 2)
                self._sized = True; self.prepareGeometryChange()
            self.update()
        if self.spin and self.spin_speed:
            self.setRotation(self.rotation() + self.spin_speed * dt)
        if self.kind == "text" and self.rainbow:
            self._hue = (self._hue + 5) % 360; self.render_text()

    def _start_media(self):
        if self.player:
            self.player.setPlaybackRate(self.media_speed / 100.0)
            self.player.play()

    def _vid_frame(self, frame):
        # может прийти из чужого потока -> только сохраняем QImage, без QPixmap/update
        if not frame.isValid(): return
        img = frame.toImage()
        if not img.isNull():
            self._pending = img.copy()

    def set_media_speed(self, pct):
        self.media_speed = pct
        if self.player:
            self.player.setPlaybackRate(pct / 100.0)

    # ---- текст ----
    def render_text(self):
        font = QFont("Sans Serif", self.font_size, QFont.Weight.Bold)
        fm = QFontMetrics(font)
        txt = self.text if self.text else " "
        w = max(1, fm.horizontalAdvance(txt) + 14); h = max(1, fm.height() + 10)
        pm = QPixmap(w, h); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setFont(font); p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if self.rainbow:
            grad = QLinearGradient(0, 0, w, 0)
            for i in range(7):
                grad.setColorAt(i / 6.0, QColor.fromHsv(int((self._hue + i * 60) % 360), 255, 255))
            pen = QPen(); pen.setBrush(QBrush(grad)); p.setPen(pen)
        else:
            p.setPen(self.color)
        p.drawText(7, fm.ascent() + 5, txt); p.end()
        self._pix = pm; self.update()

    def apply_text(self, refit=True):
        self.render_text()
        if refit:
            self._w = float(self._pix.width()); self._h = float(self._pix.height())
            self.setTransformOriginPoint(self._w / 2, self._h / 2)
            self.prepareGeometryChange()

    # ---- датчик ----
    def set_value(self, v):
        self._val = v; self.render_stat()

    def render_stat(self):
        size = 240; m = 22
        pm = QPixmap(size, size); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(m, m, size - 2 * m, size - 2 * m)
        p.setPen(QPen(QColor(55, 58, 66), 18)); p.drawArc(rect, 0, 360 * 16)
        frac = max(0.0, min(1.0, self._val / 100.0))
        pen = QPen(QColor.fromHsv(int((1 - frac) * 130), 230, 255), 18)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        p.drawArc(rect, 90 * 16, -int(360 * 16 * frac))
        p.setPen(QColor(255, 255, 255)); p.setFont(QFont("Sans Serif", 44, QFont.Weight.Bold))
        p.drawText(QRectF(0, size * 0.28, size, size * 0.34),
                   Qt.AlignmentFlag.AlignCenter, str(int(self._val)))
        p.setFont(QFont("Sans Serif", 18))
        p.drawText(QRectF(0, size * 0.58, size, size * 0.2),
                   Qt.AlignmentFlag.AlignCenter, self.metric_label)
        p.end(); self._pix = pm; self.update()

    @property
    def name(self):
        tag = {"gif": "[GIF] ", "video": "[VID] ", "text": "[TXT] ",
               "stat": "[SENS] "}.get(self.kind, "[IMG] ")
        if self.kind == "stat": return tag + self.metric_label
        if self.kind == "text": return tag + (self.text or "")[:16]
        return tag + (self.path.rsplit("/", 1)[-1] if self.path else "")

    # ---- геометрия / ручки ----
    def boundingRect(self):
        m = self.HS
        return QRectF(-m, -m * 3, self._w + 2 * m, self._h + m * 4)

    def _handles(self):
        w, h = self._w, self._h
        return {"tl": QPointF(0, 0), "tr": QPointF(w, 0), "bl": QPointF(0, h),
                "br": QPointF(w, h), "t": QPointF(w / 2, 0), "b": QPointF(w / 2, h),
                "l": QPointF(0, h / 2), "r": QPointF(w, h / 2)}

    def _rot_knob(self):
        return QPointF(self._w / 2, -self.HS * 1.8)

    def _hit(self, pos):
        for k, pt in self._handles().items():
            if abs(pos.x() - pt.x()) <= self.HS and abs(pos.y() - pt.y()) <= self.HS:
                return k
        return None

    def _near_rotate(self, pos):
        k = self._rot_knob()
        return (pos.x() - k.x()) ** 2 + (pos.y() - k.y()) ** 2 <= self.HS ** 2

    def _opp(self, k, w, h):
        x = w if "l" in k else (0 if "r" in k else w / 2)
        y = h if "t" in k else (0 if "b" in k else h / 2)
        return QPointF(x, y)

    def _center_scene(self):
        return self.mapToScene(QPointF(self._w / 2, self._h / 2))

    def paint(self, p, opt, widget=None):
        p.drawPixmap(QRectF(0, 0, self._w, self._h), self._pix, QRectF(self._pix.rect()))
        if self.isSelected() and not ResizableItem.render_clean:
            p.setPen(QPen(QColor(80, 160, 255), 0)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(0, 0, self._w, self._h))
            p.setBrush(QBrush(QColor(80, 160, 255))); r = self.HS * 0.55
            for pt in self._handles().values():
                p.drawRect(QRectF(pt.x() - r / 2, pt.y() - r / 2, r, r))
            knob = self._rot_knob()
            p.setPen(QPen(QColor(80, 160, 255), 0))
            p.drawLine(QPointF(self._w / 2, 0), knob)
            p.setBrush(QBrush(QColor(120, 220, 120)))
            p.drawEllipse(knob, r * 0.8, r * 0.8)

    def mousePressEvent(self, e):
        self.setSelected(True)
        if self._near_rotate(e.pos()):
            self._mode = "rot"
            v = e.scenePos() - self._center_scene()
            self._rot_ref = math.degrees(math.atan2(v.y(), v.x())) - self.rotation()
            e.accept(); return
        k = self._hit(e.pos())
        if k:
            self._mode = k
            self._fixed = self.mapToScene(self._opp(k, self._w, self._h))
            e.accept(); return
        self._mode = None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._mode == "rot":
            v = e.scenePos() - self._center_scene()
            self.setRotation(math.degrees(math.atan2(v.y(), v.x())) - self._rot_ref)
            e.accept(); return
        if self._mode:
            local = self.mapFromScene(e.scenePos()); m = self._mode
            left, top, right, bottom = 0.0, 0.0, self._w, self._h
            if "l" in m: left = local.x()
            if "r" in m: right = local.x()
            if "t" in m: top = local.y()
            if "b" in m: bottom = local.y()
            new_w = max(24.0, right - left); new_h = max(24.0, bottom - top)
            self.prepareGeometryChange()
            self._w, self._h = new_w, new_h
            self.setTransformOriginPoint(new_w / 2, new_h / 2)
            cur = self.mapToScene(self._opp(m, new_w, new_h))
            self.setPos(self.pos() + (self._fixed - cur))
            self.update(); e.accept(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._mode = None
        super().mouseReleaseEvent(e)

    def serialize(self):
        d = {"kind": self.kind, "path": self.path, "x": self.pos().x(), "y": self.pos().y(),
             "w": self._w, "h": self._h, "z": self.zValue(), "rot": self.rotation(),
             "opacity": self.opacity_pct, "spin": self.spin,
             "spin_speed": self.spin_speed, "mspeed": self.media_speed}
        if self.kind == "text":
            d.update(text=self.text, fsize=self.font_size, color=self.color.name(), rainbow=self.rainbow)
        if self.kind == "stat":
            d.update(metric=self.metric, label=self.metric_label)
        return d

    def restore(self, d):
        self.opacity_pct = d.get("opacity", 100); self.setOpacity(self.opacity_pct / 100)
        self.spin = d.get("spin", False); self.spin_speed = d.get("spin_speed", 0.0)
        self.set_media_speed(d.get("mspeed", 100))
        if self.kind == "text":
            self.text = d.get("text", self.text); self.font_size = d.get("fsize", 64)
            self.color = QColor(d.get("color", "#ffffff")); self.rainbow = d.get("rainbow", False)
            self.render_text()
        self.setPos(d["x"], d["y"]); self._w, self._h = d["w"], d["h"]; self._sized = True
        self.setTransformOriginPoint(self._w / 2, self._h / 2)
        self.setZValue(d["z"]); self.setRotation(d.get("rot", 0))
        self.prepareGeometryChange(); self.update()

    def cleanup(self):
        if self.player: self.player.stop()


class PanelView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.on_drop = None; self.on_delete = None

    def drawForeground(self, painter, rect):
        painter.setPen(QPen(QColor(90, 160, 255), 0)); painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.scene().sceneRect())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        if not (e.mimeData().hasUrls() and self.on_drop): return
        sp = self.mapToScene(e.position().toPoint())
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if path: self.on_drop(path, sp)
        e.acceptProposedAction()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.on_delete:
            self.on_delete()
        else:
            super().keyPressEvent(e)


class Streamer:
    def __init__(self, rotate=270, flip=False):
        self.rotate = rotate; self.flip = flip
        self.ser = None; self.ff = None; self.running = False
        self.q = queue.Queue(maxsize=2); self.lock = threading.Lock()

    def start(self):
        self.ser = serial.Serial(PORT, baudrate=BAUD, timeout=2); time.sleep(0.1)
        with self.lock:
            self.ser.write(cmd(0x90, bytes([0x01]))); time.sleep(0.2); self.ser.read(64)
            self.ser.write(cmd(0x80, bytes([0xFF]))); time.sleep(0.05)
            self.ser.write(cmd(0x81, bytes([0x01]))); time.sleep(0.05)
        self.ff = subprocess.Popen([
            "ffmpeg", "-loglevel", "quiet", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{ENC_W}x{ENC_H}", "-r", str(FPS), "-i", "pipe:0",
            "-vcodec", "libx264", "-profile:v", "baseline", "-level", "3.1",
            "-x264opts", "keyint=1:min-keyint=1:bframes=0",
            "-pix_fmt", "yuv420p", "-f", "h264", "pipe:1",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.running = True
        threading.Thread(target=self._encoder, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    def set_brightness(self, v):
        if self.ser and self.running:
            with self.lock:
                try: self.ser.write(cmd(0x80, bytes([max(0, min(255, v))])))
                except Exception: pass

    def push(self, rgb):
        try: self.q.put_nowait(rgb)
        except queue.Full:
            try: self.q.get_nowait(); self.q.put_nowait(rgb)
            except queue.Empty: pass

    def _encoder(self):
        while self.running:
            try: rgb = self.q.get(timeout=0.5)
            except queue.Empty: continue
            try: self.ff.stdin.write(rgb); self.ff.stdin.flush()
            except (BrokenPipeError, ValueError): break

    def _reader(self):
        SPS = bytes([0, 0, 0, 1, 0x67]); buf = b""
        while self.running:
            try: chunk = self.ff.stdout.read(4096)
            except Exception: break
            if not chunk: break
            buf += chunk
            while True:
                f = buf.find(SPS)
                if f < 0: break
                n = buf.find(SPS, f + 5)
                if n < 0: break
                with self.lock:
                    try: self.ser.write(cmd(0x85, buf[f:n]))
                    except Exception: self.running = False; return
                buf = buf[n:]

    def stop(self):
        self.running = False; time.sleep(0.1)
        if self.ff:
            try: self.ff.stdin.close()
            except Exception: pass
            self.ff.terminate(); self.ff = None
        if self.ser:
            try: self.ser.close()
            except Exception: pass
            self.ser = None



REPO_URL = "https://github.com/GitFlowLink/ironpride-display"
VERSION  = "1.1"
API_LATEST = "https://api.github.com/repos/GitFlowLink/ironpride-display/releases/latest"

TRANSLATIONS = {
    "ru": {
        "title":        "Iron Pride Display Editor",
        "add_img":      "+ Картинка",
        "add_gif":      "+ GIF",
        "add_video":    "+ Видео",
        "add_text":     "+ Текст",
        "sensors":      "Датчики:",
        "layers":       "Слои (верхний = спереди):",
        "layer_props":  "— Свойства слоя —",
        "text_ph":      "текст надписи",
        "rainbow":      "Радуга",
        "color":        "Цвет",
        "text_size":    "Размер текста:",
        "opacity":      "Прозрачность слоя:",
        "spin":         "Вращать слой",
        "spin_speed":   "Скорость вращения:",
        "media_speed":  "Скорость GIF/видео %:",
        "filter":       "Фильтр поверх:",
        "brightness":   "Яркость экрана:",
        "stream_start": "▶ Старт на экран",
        "stream_stop":  "■ Стоп",
        "rotate":       "⟳ Поворот панели",
        "themes":       "Темы:",
        "t_load":       "Загрузить",
        "t_save":       "Сохранить",
        "t_del":        "Удалить",
        "tray_chk":     "Сворачивать в трей",
        "auto_chk":     "Запускать с системой",
        "repo_btn":     "GitHub репозиторий",
        "tray_msg":     "Свёрнуто в трей, стрим продолжается",
        "tray_show":    "Показать окно",
        "tray_toggle":  "Старт/стоп стрим",
        "tray_quit":    "Выход",
        "port_err":     "ошибка порта",
        "t_saved":      "Тема «{}» сохранена",
        "t_name_ask":   "Название:",
        "t_name_title": "Сохранить тему",
        "filters":      ["Нет", "Ч/б", "Краснее", "Розовее", "Зеленее", "Синее"],
        "no_video":     "нет QtMultimedia",
        "repo":         "GitHub",
        "update_avail": "Обновить до {}",
        "update_none":  "",
        "lang_btn":     "EN",
    },
    "en": {
        "title":        "Iron Pride Display Editor",
        "add_img":      "+ Image",
        "add_gif":      "+ GIF",
        "add_video":    "+ Video",
        "add_text":     "+ Text",
        "sensors":      "Sensors:",
        "layers":       "Layers (top = front):",
        "layer_props":  "— Layer properties —",
        "text_ph":      "label text",
        "rainbow":      "Rainbow",
        "color":        "Color",
        "text_size":    "Text size:",
        "opacity":      "Layer opacity:",
        "spin":         "Auto-rotate",
        "spin_speed":   "Rotation speed:",
        "media_speed":  "GIF/video speed %:",
        "filter":       "Color filter:",
        "brightness":   "Screen brightness:",
        "stream_start": "▶ Stream to screen",
        "stream_stop":  "■ Stop",
        "rotate":       "⟳ Rotate panel",
        "themes":       "Themes:",
        "t_load":       "Load",
        "t_save":       "Save",
        "t_del":        "Delete",
        "tray_chk":     "Minimize to tray",
        "auto_chk":     "Launch on startup",
        "repo_btn":     "GitHub repository",
        "tray_msg":     "Minimized to tray, stream continues",
        "tray_show":    "Show window",
        "tray_toggle":  "Start/stop stream",
        "tray_quit":    "Quit",
        "port_err":     "port error",
        "t_saved":      "Theme «{}» saved",
        "t_name_ask":   "Name:",
        "t_name_title": "Save theme",
        "filters":      ["None", "B/W", "Redder", "Pinker", "Greener", "Bluer"],
        "no_video":     "QtMultimedia missing",
        "repo":         "GitHub",
        "update_avail": "Update to {}",
        "update_none":  "",
        "lang_btn":     "RU",
    },
}

def _icon_globe():
    pm = QPixmap(40, 40); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(90, 200, 130), 3)); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(5, 5, 30, 30)
    p.drawEllipse(14, 5, 12, 30)
    p.drawLine(5, 20, 35, 20)
    p.drawLine(8, 12, 32, 12); p.drawLine(8, 28, 32, 28)
    p.end(); return QIcon(pm)

def _icon_git():
    pm = QPixmap(40, 40); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(240, 80, 30), 4)); p.setBrush(QColor(240, 80, 30))
    # branch: line + two nodes
    p.drawLine(12, 8, 12, 32)
    p.setPen(QPen(QColor(240, 80, 30), 4))
    p.drawLine(12, 14, 28, 22)
    p.setBrush(QColor(245, 110, 60))
    p.drawEllipse(6, 4, 12, 12)
    p.drawEllipse(6, 28, 12, 12)
    p.drawEllipse(24, 16, 12, 12)
    p.end(); return QIcon(pm)


def _make_icon():
    """Generate app icon: blue rounded rect (display shape) with white dot."""
    pm = QPixmap(64, 64); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(30, 30, 40))
    p.drawRoundedRect(2, 2, 60, 60, 10, 10)
    p.setBrush(QColor(40, 120, 220))
    p.drawRoundedRect(8, 22, 48, 20, 5, 5)
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(26, 44, 12, 8)
    p.setBrush(QColor(120, 200, 255))
    p.drawEllipse(14, 27, 10, 10)
    p.end()
    return QIcon(pm)

# FILTERS now comes from TRANSLATIONS[lang]["filters"]
FILTERS = TRANSLATIONS["ru"]["filters"]

class Editor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("ironpride", "editor")
        self._lang = self.settings.value("lang", "ru")
        self.layers = []
        self.streamer = Streamer()
        self._last = time.time()
        self.resize(1150, 600)

        self.scene = QGraphicsScene(0, 0, PANEL_W, PANEL_H)
        self.scene.setBackgroundBrush(QColor(0, 0, 0))
        self.view = PanelView(self.scene)
        self.view.setBackgroundBrush(QColor(45, 47, 52))
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.on_drop = self._on_drop
        self.view.on_delete = self.delete_selected

        # реестр переводимых виджетов: (widget, key, setter)
        self._tr = []
        def reg(w, key, setter="text"):
            self._tr.append((w, key, setter)); return w

        side = QVBoxLayout()
        # верх: язык + гитхаб + версия
        toprow = QHBoxLayout()
        self.b_lang = QPushButton(_icon_globe(), self.T("lang_btn"))
        self.b_lang.setFixedWidth(58); self.b_lang.clicked.connect(self._toggle_lang)
        self.b_repo = QPushButton(_icon_git(), self.T("repo"))
        self.b_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_repo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        reg(self.b_repo, "repo")
        self.ver_lbl = QLabel(f"v{VERSION}")
        self.ver_lbl.setStyleSheet("color:#888;")
        toprow.addWidget(self.b_lang); toprow.addWidget(self.b_repo)
        toprow.addWidget(self.ver_lbl); toprow.addStretch()
        side.addLayout(toprow)

        # кнопка обновления (скрыта пока нет апдейта)
        self.b_update = QPushButton(); self.b_update.setVisible(False)
        self.b_update.setStyleSheet("background:#2e7d32; color:white; font-weight:bold;")
        self.b_update.clicked.connect(self._do_update)
        side.addWidget(self.b_update)

        addrow = QGridLayout()
        self._add_btns = []
        for n, (key, kind) in enumerate((("add_img", "img"), ("add_gif", "gif"),
                                         ("add_video", "video"), ("add_text", "text"))):
            b = QPushButton(self.T(key)); reg(b, key)
            if kind == "video" and not HAS_VIDEO:
                b.setEnabled(False); b.setToolTip(self.T("no_video"))
            b.clicked.connect(lambda _, k=kind: self.add_layer(k))
            addrow.addWidget(b, n // 2, n % 2)
        side.addLayout(addrow)

        side.addWidget(reg(QLabel(self.T("sensors")), "sensors"))
        srow = QGridLayout(); self.stat_chk = {}
        for n, (mk, lbl) in enumerate((("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM"),
                                       ("cput", "CPU°"), ("gput", "GPU°"))):
            ch = QCheckBox(lbl)
            ch.toggled.connect(lambda on, m=mk, l=lbl: self._toggle_stat(m, l, on))
            srow.addWidget(ch, n // 3, n % 3); self.stat_chk[mk] = ch
        side.addLayout(srow)

        side.addWidget(reg(QLabel(self.T("layers")), "layers"))
        self.list = QListWidget(); self.list.currentRowChanged.connect(self._select)
        side.addWidget(self.list)
        row = QHBoxLayout()
        for text, fn in (("▲", lambda: self.reorder(-1)), ("▼", lambda: self.reorder(1)),
                         ("✕", self.delete_selected)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        side.addLayout(row)

        side.addWidget(self._hline())
        side.addWidget(reg(QLabel(self.T("layer_props")), "layer_props"))
        self.txt_edit = QLineEdit(); self.txt_edit.setPlaceholderText(self.T("text_ph"))
        reg(self.txt_edit, "text_ph", "placeholder")
        self.txt_edit.textChanged.connect(self._text_changed); side.addWidget(self.txt_edit)
        trow = QHBoxLayout()
        self.chk_rainbow = QCheckBox(self.T("rainbow")); reg(self.chk_rainbow, "rainbow")
        self.chk_rainbow.toggled.connect(self._rainbow)
        b_col = QPushButton(self.T("color")); reg(b_col, "color"); b_col.clicked.connect(self._pick_color)
        trow.addWidget(self.chk_rainbow); trow.addWidget(b_col); side.addLayout(trow)
        side.addWidget(reg(QLabel(self.T("text_size")), "text_size"))
        self.fsize = QSlider(Qt.Orientation.Horizontal); self.fsize.setRange(12, 220)
        self.fsize.setValue(64); self.fsize.valueChanged.connect(self._fsize); side.addWidget(self.fsize)

        side.addWidget(reg(QLabel(self.T("opacity")), "opacity"))
        self.opac = QSlider(Qt.Orientation.Horizontal); self.opac.setRange(0, 100)
        self.opac.setValue(100); self.opac.valueChanged.connect(self._opacity); side.addWidget(self.opac)
        self.chk_spin = QCheckBox(self.T("spin")); reg(self.chk_spin, "spin"); self.chk_spin.toggled.connect(self._spin)
        side.addWidget(self.chk_spin)
        side.addWidget(reg(QLabel(self.T("spin_speed")), "spin_speed"))
        self.spinsp = QSlider(Qt.Orientation.Horizontal); self.spinsp.setRange(0, 360)
        self.spinsp.setValue(60); self.spinsp.valueChanged.connect(self._spin_speed); side.addWidget(self.spinsp)
        side.addWidget(reg(QLabel(self.T("media_speed")), "media_speed"))
        self.mspeed = QSlider(Qt.Orientation.Horizontal); self.mspeed.setRange(10, 300)
        self.mspeed.setValue(100); self.mspeed.valueChanged.connect(self._mspeed); side.addWidget(self.mspeed)

        side.addWidget(self._hline())
        side.addWidget(reg(QLabel(self.T("filter")), "filter"))
        self.filt = QComboBox(); self.filt.addItems(self.T("filters")); side.addWidget(self.filt)
        side.addWidget(reg(QLabel(self.T("brightness")), "brightness"))
        self.bri = QSlider(Qt.Orientation.Horizontal); self.bri.setRange(0, 255)
        self.bri.setValue(255); self.bri.valueChanged.connect(self.streamer.set_brightness); side.addWidget(self.bri)
        self.b_stream = QPushButton(self.T("stream_start")); self.b_stream.setCheckable(True)
        self.b_stream.clicked.connect(self.toggle_stream); side.addWidget(self.b_stream)
        self.b_rot = QPushButton(self.T("rotate")); reg(self.b_rot, "rotate")
        self.b_rot.clicked.connect(self._toggle_rotate); side.addWidget(self.b_rot)

        side.addWidget(self._hline())
        side.addWidget(reg(QLabel(self.T("themes")), "themes"))
        self.theme_combo = QComboBox(); side.addWidget(self.theme_combo)
        throw = QHBoxLayout()
        for key, fn in (("t_load", self.load_theme), ("t_save", self.save_theme),
                        ("t_del", self.delete_theme)):
            b = QPushButton(self.T(key)); reg(b, key); b.clicked.connect(fn); throw.addWidget(b)
        side.addLayout(throw)

        side.addWidget(self._hline())
        self.chk_tray = QCheckBox(self.T("tray_chk")); reg(self.chk_tray, "tray_chk")
        self.chk_tray.setChecked(self.settings.value("tray", True, bool))
        self.chk_tray.toggled.connect(lambda v: self.settings.setValue("tray", v)); side.addWidget(self.chk_tray)
        self.chk_auto = QCheckBox(self.T("auto_chk")); reg(self.chk_auto, "auto_chk")
        self.chk_auto.setChecked(os.path.exists(AUTOSTART))
        self.chk_auto.toggled.connect(self._toggle_autostart); side.addWidget(self.chk_auto)

        side.addStretch()

        side_w = QWidget(); side_w.setLayout(side); side_w.setFixedWidth(270)
        rootl = QHBoxLayout(); rootl.addWidget(self.view, 1); rootl.addWidget(side_w)
        c = QWidget(); c.setLayout(rootl); self.setCentralWidget(c)

        self._app_icon = _make_icon()
        self.setWindowIcon(self._app_icon)
        self.setWindowTitle(self.T("title"))
        self._make_tray()
        self._refresh_themes()
        self.load_project()

        self.cap_timer = QTimer(self); self.cap_timer.timeout.connect(self._capture)
        self.anim = QTimer(self); self.anim.timeout.connect(self._anim_tick); self.anim.start(40)
        self.stat_timer = QTimer(self); self.stat_timer.timeout.connect(self._update_stats); self.stat_timer.start(1000)
        psutil.cpu_percent()
        self._latest_ver = None; self._update_url = None
        self._check_update()

    def T(self, key):
        return TRANSLATIONS[self._lang][key]

    def _hline(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine); return f

    def _make_tray(self):
        self.tray = QSystemTrayIcon(self._app_icon, self); self.tray.setToolTip("Iron Pride Display")
        menu = QMenu()
        menu.addAction(self.T("tray_show"), self.showNormal)
        menu.addAction(self.T("tray_toggle"), lambda: self.b_stream.click())
        menu.addSeparator(); menu.addAction(self.T("tray_quit"), self._quit)
        self.tray.setContextMenu(menu); self.tray.activated.connect(self._tray_click); self.tray.show()

    def _tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal(); self.activateWindow()

    def resizeEvent(self, e):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio); super().resizeEvent(e)

    def showEvent(self, e):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio); super().showEvent(e)

    # ---- слои ----
    def add_layer(self, kind, path=None, restore=None, pos=None, metric=None, label=None):
        if kind in ("img", "gif", "video") and path is None and restore is None:
            if kind == "gif": flt = "GIF (*.gif)"
            elif kind == "video": flt = "Видео (*.mp4 *.mkv *.webm *.mov *.avi *.m4v)"
            else: flt = "Картинки (*.png *.jpg *.jpeg *.webp *.bmp)"
            path, _ = QFileDialog.getOpenFileName(self, "Выбери файл", "", flt)
            if not path: return None
        item = ResizableItem(kind, path=path, metric=metric, label=label)
        item.setZValue(len(self.layers)); item.setOpacity(item.opacity_pct / 100)
        if pos is not None:
            item.setPos(pos.x() - item._w / 2, pos.y() - item._h / 2)
        self.scene.addItem(item); self.layers.append(item)
        self.list.addItem(QListWidgetItem(item.name)); self.list.setCurrentRow(self.list.count() - 1)
        if restore: item.restore(restore)
        return item

    def _on_drop(self, path, scenepos):
        if os.path.exists(path):
            self.add_layer(kind_of(path), path=path, pos=scenepos)

    def _cur(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.layers): return self.layers[i]
        for it in self.layers:
            if it.isSelected(): return it
        return None

    def _select(self, row):
        for i, it in enumerate(self.layers):
            it.setSelected(i == row)
        it = self._cur()
        if not it: return
        ws = (self.opac, self.spinsp, self.mspeed, self.fsize, self.chk_spin,
              self.chk_rainbow, self.txt_edit)
        for w in ws: w.blockSignals(True)
        self.opac.setValue(int(it.opacity_pct)); self.chk_spin.setChecked(it.spin)
        self.spinsp.setValue(int(it.spin_speed)); self.mspeed.setValue(int(it.media_speed))
        self.txt_edit.setText(it.text if it.kind == "text" else "")
        self.fsize.setValue(int(it.font_size)); self.chk_rainbow.setChecked(it.rainbow)
        for w in ws: w.blockSignals(False)

    def reorder(self, d):
        i = self.list.currentRow(); j = i + d
        if not (0 <= i < len(self.layers) and 0 <= j < len(self.layers)): return
        self.layers[i], self.layers[j] = self.layers[j], self.layers[i]
        for z, it in enumerate(self.layers): it.setZValue(z)
        self.list.clear()
        for it in self.layers: self.list.addItem(QListWidgetItem(it.name))
        self.list.setCurrentRow(j)

    def _remove_index(self, i):
        if 0 <= i < len(self.layers):
            it = self.layers[i]
            if it.kind == "stat" and it.metric in self.stat_chk:
                self.stat_chk[it.metric].blockSignals(True)
                self.stat_chk[it.metric].setChecked(False)
                self.stat_chk[it.metric].blockSignals(False)
            it.cleanup(); self.scene.removeItem(it)
            del self.layers[i]; self.list.takeItem(i)
            for z, l in enumerate(self.layers): l.setZValue(z)

    def delete_selected(self):
        i = self.list.currentRow()
        if not (0 <= i < len(self.layers)):
            for idx, it in enumerate(self.layers):
                if it.isSelected(): i = idx; break
        self._remove_index(i)

    def _clear_layers(self):
        for it in list(self.layers):
            it.cleanup(); self.scene.removeItem(it)
        self.layers = []; self.list.clear()
        for ch in self.stat_chk.values():
            ch.blockSignals(True); ch.setChecked(False); ch.blockSignals(False)

    def _toggle_stat(self, metric, label, on):
        if on:
            self.add_layer("stat", metric=metric, label=label, pos=QPointF(PANEL_W / 2, PANEL_H / 2))
        else:
            for idx, it in enumerate(self.layers):
                if it.kind == "stat" and it.metric == metric:
                    self._remove_index(idx); break

    # ---- свойства ----
    def _opacity(self, v):
        it = self._cur()
        if it: it.opacity_pct = v; it.setOpacity(v / 100)
    def _spin(self, on):
        it = self._cur()
        if it: it.spin = on
    def _spin_speed(self, v):
        it = self._cur()
        if it: it.spin_speed = float(v)
    def _mspeed(self, v):
        it = self._cur()
        if it: it.set_media_speed(v)
    def _text_changed(self, s):
        it = self._cur()
        if it and it.kind == "text":
            it.text = s; it.apply_text(); self._refresh_name(it)
    def _fsize(self, v):
        it = self._cur()
        if it and it.kind == "text": it.font_size = v; it.apply_text()
    def _rainbow(self, on):
        it = self._cur()
        if it and it.kind == "text": it.rainbow = on; it.apply_text()
    def _pick_color(self):
        it = self._cur()
        if it and it.kind == "text":
            c = QColorDialog.getColor(it.color, self, "Цвет текста")
            if c.isValid():
                it.color = c; it.rainbow = False
                self.chk_rainbow.blockSignals(True); self.chk_rainbow.setChecked(False)
                self.chk_rainbow.blockSignals(False); it.apply_text()
    def _refresh_name(self, it):
        i = self.layers.index(it); self.list.item(i).setText(it.name)

    # ---- стрим ----
    def _toggle_rotate(self):
        self.streamer.rotate = 90 if self.streamer.rotate == 270 else 270
        self.view.rotate(180)

    def autostart_stream(self):
        if not self.b_stream.isChecked():
            self.b_stream.setChecked(True); self.toggle_stream(True)

    def toggle_stream(self, on):
        if on:
            try:
                self.streamer.start()
            except Exception as ex:
                self.b_stream.setChecked(False); self.b_stream.setText(self.T("port_err"))
                self.tray.showMessage("Iron Pride", str(ex)); return
            self.cap_timer.start(int(1000 / FPS))
            self.b_stream.setText(self.T("stream_stop")); self.b_stream.setChecked(True)
        else:
            self.cap_timer.stop(); self.streamer.stop()
            self.b_stream.setText(self.T("stream_start")); self.b_stream.setChecked(False)

    def _anim_tick(self):
        now = time.time(); dt = now - self._last; self._last = now
        for it in list(self.layers):
            it.tick_anim(dt)

    def _update_stats(self):
        vals = read_sensors()
        for it in self.layers:
            if it.kind == "stat": it.set_value(vals.get(it.metric, 0))

    def _apply_filter(self, pil):
        f = self.filt.currentText()
        if f == "Нет": return pil
        if f == "Ч/б": return ImageOps.grayscale(pil).convert("RGB")
        mul = {"Краснее": (1.0, 0.45, 0.45), "Розовее": (1.0, 0.6, 0.85),
               "Зеленее": (0.45, 1.0, 0.45), "Синее": (0.5, 0.6, 1.0)}.get(f)
        if not mul: return pil
        r, g, b = pil.split()
        r = r.point(lambda v: int(v * mul[0])); g = g.point(lambda v: int(v * mul[1]))
        b = b.point(lambda v: int(v * mul[2]))
        return Image.merge("RGB", (r, g, b))

    def _capture(self):
        try:
            img = QImage(PANEL_W, PANEL_H, QImage.Format.Format_RGB888)
            img.fill(Qt.GlobalColor.black)
            ResizableItem.render_clean = True
            p = QPainter(img)
            self.scene.render(p, QRectF(0, 0, PANEL_W, PANEL_H), QRectF(0, 0, PANEL_W, PANEL_H))
            p.end()
            ResizableItem.render_clean = False
            bits = img.constBits(); bits.setsize(img.sizeInBytes())
            pil = Image.frombytes("RGB", (PANEL_W, PANEL_H), bytes(bits))
            pil = self._apply_filter(pil)
            pil = pil.transpose(Image.ROTATE_270 if self.streamer.rotate == 270 else Image.ROTATE_90)
            if self.streamer.flip: pil = pil.transpose(Image.FLIP_LEFT_RIGHT)
            self.streamer.push(pil.tobytes())
        except Exception:
            ResizableItem.render_clean = False

    # ---- темы / проект ----
    def _toggle_autostart(self, on):
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            if getattr(sys, "frozen", False):          # собранный бинарь
                exec_line = f'Exec="{sys.executable}" --background'
            else:                                        # обычный .py
                exec_line = f'Exec=python3 "{os.path.abspath(__file__)}" --background'
            with open(AUTOSTART, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Iron Pride Display\n"
                        f"{exec_line}\n"
                        "X-GNOME-Autostart-enabled=true\nTerminal=false\n")
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)

    def _theme_path(self, name):
        return os.path.join(THEMES_DIR, name + ".json")

    def _refresh_themes(self):
        cur = self.theme_combo.currentText() if hasattr(self, "theme_combo") else ""
        self.theme_combo.blockSignals(True); self.theme_combo.clear()
        if os.path.isdir(THEMES_DIR):
            for fn in sorted(os.listdir(THEMES_DIR)):
                if fn.endswith(".json"):
                    self.theme_combo.addItem(fn[:-5])
        i = self.theme_combo.findText(cur)
        if i >= 0: self.theme_combo.setCurrentIndex(i)
        self.theme_combo.blockSignals(False)

    def save_theme(self):
        name, ok = QInputDialog.getText(self, self.T("t_name_title"), self.T("t_name_ask"))
        if not (ok and name.strip()): return
        os.makedirs(THEMES_DIR, exist_ok=True)
        with open(self._theme_path(name.strip()), "w") as f:
            json.dump([it.serialize() for it in self.layers], f)
        self._refresh_themes()
        self.theme_combo.setCurrentText(name.strip())
        self.tray.showMessage("Iron Pride", self.T("t_saved").format(name.strip()))

    def load_theme(self):
        name = self.theme_combo.currentText()
        if not name or not os.path.exists(self._theme_path(name)): return
        try:
            data = json.load(open(self._theme_path(name)))
        except Exception:
            return
        self._clear_layers(); self._load_data(data); self.save_project()

    def delete_theme(self):
        name = self.theme_combo.currentText()
        if name and os.path.exists(self._theme_path(name)):
            os.remove(self._theme_path(name)); self._refresh_themes()

    def _load_data(self, data):
        for d in data:
            k = d.get("kind", "img")
            if k in ("img", "gif", "video") and not (d.get("path") and os.path.exists(d["path"])):
                continue
            it = self.add_layer(k, path=d.get("path"), restore=d,
                                metric=d.get("metric"), label=d.get("label"))
            if it and k == "stat" and d.get("metric") in self.stat_chk:
                self.stat_chk[d["metric"]].blockSignals(True)
                self.stat_chk[d["metric"]].setChecked(True)
                self.stat_chk[d["metric"]].blockSignals(False)

    def save_project(self):
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(PROJ_FILE, "w") as f:
            json.dump([it.serialize() for it in self.layers], f)

    def load_project(self):
        if not os.path.exists(PROJ_FILE): return
        try: data = json.load(open(PROJ_FILE))
        except Exception: return
        self._load_data(data)

    def start_background(self):
        self.autostart_stream()

    def _toggle_lang(self):
        self._lang = "en" if self._lang == "ru" else "ru"
        self.settings.setValue("lang", self._lang)
        self._apply_lang()

    def _apply_lang(self):
        """Refresh every translatable widget without rebuilding the window."""
        self.setWindowTitle(self.T("title"))
        # все зарегистрированные виджеты
        for w, key, setter in self._tr:
            if setter == "text": w.setText(self.T(key))
            elif setter == "placeholder": w.setPlaceholderText(self.T(key))
        self.b_lang.setText(self.T("lang_btn"))
        self.b_stream.setText(
            self.T("stream_stop") if self.b_stream.isChecked() else self.T("stream_start"))
        # трей-меню
        menu = self.tray.contextMenu()
        if menu:
            labels = [self.T("tray_show"), self.T("tray_toggle"), self.T("tray_quit")]
            j = 0
            for a in menu.actions():
                if not a.isSeparator() and j < len(labels):
                    a.setText(labels[j]); j += 1
        # комбобокс фильтров
        cur = self.filt.currentIndex()
        self.filt.blockSignals(True); self.filt.clear()
        self.filt.addItems(self.T("filters")); self.filt.setCurrentIndex(cur)
        self.filt.blockSignals(False)
        # кнопка обновления (если видна)
        if self._latest_ver:
            self.b_update.setText(self.T("update_avail").format(self._latest_ver))

    # ---- проверка обновлений ----
    def _check_update(self):
        self._latest_ver = None
        def worker():
            try:
                req = urllib.request.Request(API_LATEST, headers={"User-Agent": "ironpride"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.load(r)
                tag = (data.get("tag_name") or "").lstrip("v")
                assets = data.get("assets", [])
                url = None
                for a in assets:
                    if a.get("name") == "ironpride-display":
                        url = a.get("browser_download_url"); break
                if tag and self._newer(tag, VERSION):
                    self._latest_ver = tag; self._update_url = url
                    QTimer.singleShot(0, self._show_update)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _newer(a, b):
        def parts(s): return [int(x) for x in s.split(".") if x.isdigit()]
        return parts(a) > parts(b)

    def _show_update(self):
        if self._latest_ver:
            self.b_update.setText(self.T("update_avail").format(self._latest_ver))
            self.b_update.setVisible(True)
            self.tray.showMessage("Iron Pride", self.T("update_avail").format(self._latest_ver))

    def _do_update(self):
        # качаем новый бинарь и заменяем текущий (если это собранный бинарь)
        if not getattr(self, "_update_url", None) or not getattr(sys, "frozen", False):
            QDesktopServices.openUrl(QUrl(REPO_URL + "/releases/latest")); return
        try:
            target = sys.executable
            tmp = target + ".new"
            req = urllib.request.Request(self._update_url, headers={"User-Agent": "ironpride"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                f.write(r.read())
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
            self.tray.showMessage("Iron Pride", "OK — restart to apply / перезапусти")
            self.b_update.setVisible(False)
        except Exception as ex:
            self.tray.showMessage("Iron Pride", f"update failed: {ex}")
            QDesktopServices.openUrl(QUrl(REPO_URL + "/releases/latest"))

    def _quit(self):
        self.save_project(); self.cap_timer.stop(); self.streamer.stop(); QApplication.quit()

    def closeEvent(self, e):
        self.save_project()
        if self.chk_tray.isChecked() and self.tray.isVisible():
            e.ignore(); self.hide()
            self.tray.showMessage("Iron Pride", self.T("tray_msg"))
        else:
            self.cap_timer.stop(); self.streamer.stop(); e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = Editor()
    if "--background" in sys.argv:
        w.start_background()
    else:
        w.show(); w.autostart_stream()
    sys.exit(app.exec())
