#!/usr/bin/env python3
"""
Iron Pride Invader Q9MX — нативный Linux редактор/клиент дисплея.

Холст 1920x462 (видимая ландшафт-область панели). Слои: картинки и GIF,
можно двигать мышкой, масштабировать, менять порядок, удалять.
Поток: рендер холста -> поворот в 462x1920 -> постоянный ffmpeg (libx264,
baseline, keyint=1) -> каждый кадр оборачивается в команду 0x85 и шлётся
на /dev/ttyACM0. Протокол реверснут из APEXSTORM.

Зависимости: python3-pyqt6, pyserial, pillow, ffmpeg
Запуск: python3 ironpride_editor.py
"""
import sys, struct, time, threading, subprocess, queue

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsItem, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QSlider, QLabel, QFileDialog, QFrame,
)
from PyQt6.QtGui import QPixmap, QImage, QMovie, QPainter
from PyQt6.QtCore import Qt, QTimer, QRectF
from PIL import Image
import serial

# ---- геометрия панели (реверснуто из APEXSTORM) ----
PANEL_W, PANEL_H = 1920, 462      # видимая ландшафт-область
ENC_W, ENC_H     = 462, 1920      # буфер H.264 (панель сама разворачивает)
PORT = "/dev/ttyACM0"
BAUD = 2000000
FPS  = 15

MAGIC = bytes([0x5A, 0xA5])
def cmd(c, payload):
    return MAGIC + bytes([c, 0x00]) + struct.pack("<I", len(payload)) + payload


class Layer:
    """Один слой: статичная картинка или анимированный GIF."""
    def __init__(self, path, is_gif):
        self.path = path
        self.is_gif = is_gif
        self.item = QGraphicsPixmapItem()
        self.item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.movie = None
        if is_gif:
            self.movie = QMovie(path)
            self.movie.frameChanged.connect(self._frame)
            self.movie.start()
            self._frame()
        else:
            self.item.setPixmap(QPixmap(path))

    def _frame(self):
        self.item.setPixmap(self.movie.currentPixmap())

    @property
    def name(self):
        base = self.path.rsplit("/", 1)[-1]
        return ("[GIF] " if self.is_gif else "[IMG] ") + base


class Streamer:
    """Тянет кадры из колбэка, гонит через ffmpeg и шлёт на дисплей."""
    def __init__(self, rotate=270, flip=False):
        self.rotate = rotate
        self.flip = flip
        self.ser = None
        self.ff = None
        self.running = False
        self.q = queue.Queue(maxsize=2)
        self.lock = threading.Lock()

    def start(self):
        self.ser = serial.Serial(PORT, baudrate=BAUD, timeout=2)
        time.sleep(0.1)
        with self.lock:
            self.ser.write(cmd(0x90, bytes([0x01])))  # hello
            time.sleep(0.2)
            self.ser.read(64)
            self.ser.write(cmd(0x80, bytes([0xFF])))  # яркость 255
            time.sleep(0.05)
            self.ser.write(cmd(0x81, bytes([0x01])))  # ориентация
            time.sleep(0.05)

        self.ff = subprocess.Popen([
            "ffmpeg", "-loglevel", "quiet",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
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
                self.ser.write(cmd(0x80, bytes([max(0, min(255, v))])))

    def push(self, rgb_bytes):
        """RGB 462x1920 кадр от GUI-потока (без блокировки, дропаем лишнее)."""
        try:
            self.q.put_nowait(rgb_bytes)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(rgb_bytes)
            except queue.Empty:
                pass

    def _encoder(self):
        while self.running:
            try:
                rgb = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.ff.stdin.write(rgb)
                self.ff.stdin.flush()
            except (BrokenPipeError, ValueError):
                break

    def _reader(self):
        SPS = bytes([0, 0, 0, 1, 0x67])
        buf = b""
        while self.running:
            chunk = self.ff.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                first = buf.find(SPS)
                if first < 0:
                    break
                nxt = buf.find(SPS, first + 5)
                if nxt < 0:
                    break
                frame = buf[first:nxt]
                with self.lock:
                    try:
                        self.ser.write(cmd(0x85, frame))
                    except (serial.SerialException, ValueError):
                        self.running = False
                        return
                buf = buf[nxt:]

    def stop(self):
        self.running = False
        time.sleep(0.1)
        if self.ff:
            try:
                self.ff.stdin.close()
            except Exception:
                pass
            self.ff.terminate()
            self.ff = None
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None


class Editor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Iron Pride Display Editor")
        self.resize(1200, 600)
        self.layers = []
        self.streamer = Streamer()
        self.cap_timer = QTimer(self)
        self.cap_timer.timeout.connect(self._capture)

        # ---- холст ----
        self.scene = QGraphicsScene(0, 0, PANEL_W, PANEL_H)
        self.scene.setBackgroundBrush(Qt.GlobalColor.black)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # ---- правая панель ----
        side = QVBoxLayout()
        b_img = QPushButton("+ Картинка")
        b_gif = QPushButton("+ GIF")
        b_img.clicked.connect(lambda: self.add_layer(False))
        b_gif.clicked.connect(lambda: self.add_layer(True))
        side.addWidget(b_img)
        side.addWidget(b_gif)

        side.addWidget(QLabel("Слои (верхний = спереди):"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select_from_list)
        side.addWidget(self.list)

        row = QHBoxLayout()
        b_up = QPushButton("▲"); b_dn = QPushButton("▼"); b_del = QPushButton("✕")
        b_up.clicked.connect(lambda: self.reorder(-1))
        b_dn.clicked.connect(lambda: self.reorder(1))
        b_del.clicked.connect(self.delete_layer)
        row.addWidget(b_up); row.addWidget(b_dn); row.addWidget(b_del)
        side.addLayout(row)

        side.addWidget(QLabel("Размер слоя:"))
        self.scale_sl = QSlider(Qt.Orientation.Horizontal)
        self.scale_sl.setRange(10, 400); self.scale_sl.setValue(100)
        self.scale_sl.valueChanged.connect(self._scale_changed)
        side.addWidget(self.scale_sl)

        side.addWidget(self._hline())

        side.addWidget(QLabel("Яркость экрана:"))
        self.bri_sl = QSlider(Qt.Orientation.Horizontal)
        self.bri_sl.setRange(0, 255); self.bri_sl.setValue(255)
        self.bri_sl.valueChanged.connect(lambda v: self.streamer.set_brightness(v))
        side.addWidget(self.bri_sl)

        self.b_stream = QPushButton("▶ Старт на экран")
        self.b_stream.setCheckable(True)
        self.b_stream.clicked.connect(self.toggle_stream)
        side.addWidget(self.b_stream)

        b_flip = QPushButton("⟳ Перевернуть (90/270)")
        b_flip.clicked.connect(self._toggle_rotate)
        side.addWidget(b_flip)
        side.addStretch()

        side_w = QWidget(); side_w.setLayout(side); side_w.setFixedWidth(260)
        root = QHBoxLayout()
        root.addWidget(self.view, 1)
        root.addWidget(side_w)
        c = QWidget(); c.setLayout(root); self.setCentralWidget(c)

    def _hline(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine); return f

    def resizeEvent(self, e):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        super().resizeEvent(e)

    def showEvent(self, e):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        super().showEvent(e)

    # ---- слои ----
    def add_layer(self, is_gif):
        flt = "GIF (*.gif)" if is_gif else "Картинки (*.png *.jpg *.jpeg *.webp *.bmp)"
        path, _ = QFileDialog.getOpenFileName(self, "Выбери файл", "", flt)
        if not path:
            return
        layer = Layer(path, is_gif)
        layer.item.setZValue(len(self.layers))
        self.scene.addItem(layer.item)
        self.layers.append(layer)
        self.list.addItem(QListWidgetItem(layer.name))
        self.list.setCurrentRow(self.list.count() - 1)

    def _current(self):
        i = self.list.currentRow()
        return self.layers[i] if 0 <= i < len(self.layers) else None

    def _select_from_list(self, row):
        for i, l in enumerate(self.layers):
            l.item.setSelected(i == row)
        cur = self._current()
        if cur:
            self.scale_sl.blockSignals(True)
            self.scale_sl.setValue(int(cur.item.scale() * 100))
            self.scale_sl.blockSignals(False)

    def _scale_changed(self, v):
        cur = self._current()
        if cur:
            cur.item.setScale(v / 100.0)

    def reorder(self, d):
        i = self.list.currentRow()
        j = i + d
        if not (0 <= i < len(self.layers) and 0 <= j < len(self.layers)):
            return
        self.layers[i], self.layers[j] = self.layers[j], self.layers[i]
        for z, l in enumerate(self.layers):
            l.item.setZValue(z)
        self.list.clear()
        for l in self.layers:
            self.list.addItem(QListWidgetItem(l.name))
        self.list.setCurrentRow(j)

    def delete_layer(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.layers):
            self.scene.removeItem(self.layers[i].item)
            if self.layers[i].movie:
                self.layers[i].movie.stop()
            del self.layers[i]
            self.list.takeItem(i)
            for z, l in enumerate(self.layers):
                l.item.setZValue(z)

    # ---- стрим ----
    def _toggle_rotate(self):
        self.streamer.rotate = 90 if self.streamer.rotate == 270 else 270

    def toggle_stream(self, on):
        if on:
            try:
                self.streamer.start()
            except Exception as ex:
                self.b_stream.setChecked(False)
                self.b_stream.setText(f"ошибка: {ex}")
                return
            self.cap_timer.start(int(1000 / FPS))
            self.b_stream.setText("■ Стоп")
        else:
            self.cap_timer.stop()
            self.streamer.stop()
            self.b_stream.setText("▶ Старт на экран")

    def _capture(self):
        # рендер сцены на GUI-потоке -> RGB 462x1920 -> в очередь стримера
        img = QImage(PANEL_W, PANEL_H, QImage.Format.Format_RGB888)
        img.fill(Qt.GlobalColor.black)
        p = QPainter(img)
        self.scene.render(p, QRectF(0, 0, PANEL_W, PANEL_H),
                          QRectF(0, 0, PANEL_W, PANEL_H))
        p.end()
        bits = img.constBits(); bits.setsize(img.sizeInBytes())
        pil = Image.frombytes("RGB", (PANEL_W, PANEL_H), bytes(bits))
        if self.streamer.rotate == 270:
            pil = pil.transpose(Image.ROTATE_270)
        else:
            pil = pil.transpose(Image.ROTATE_90)
        if self.streamer.flip:
            pil = pil.transpose(Image.FLIP_LEFT_RIGHT)
        self.streamer.push(pil.tobytes())

    def closeEvent(self, e):
        self.cap_timer.stop()
        self.streamer.stop()
        super().closeEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Editor()
    w.show()
    sys.exit(app.exec())
