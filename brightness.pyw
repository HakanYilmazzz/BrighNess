import sys
import os
import json
import math
import queue
import threading
import logging
import winreg
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

import screen_brightness_control as sbc
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QSlider, QLabel,
                             QPushButton, QSystemTrayIcon, QMenu, QAction, QActionGroup, QFrame,
                             QGraphicsDropShadowEffect, QLayout)
from PyQt5.QtCore import Qt, QTimer, QThread, QPointF, QRectF, QSize, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import (QIcon, QPixmap, QPainter, QColor, QCursor, QPaintEvent, QPen, QBrush,
                         QPainterPath, QLinearGradient, QFont)

APP_NAME = "ParlaklikKontrol"
SINGLE_INSTANCE_KEY = "ParlaklikKontrolApp_SingleInstance_Key"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
LOG_PATH = os.path.join(CONFIG_DIR, "app.log")

SHADOW = 12  # penceredeki gölge payı (px)

DEFAULT_SETTINGS = {
    "lat": 39.6484,
    "lon": 27.8826,
    "tz_offset": None,         # None: sistem saat dilimini kullan
    "min_brightness": 10,      # gece/otomatik modda inilecek en düşük parlaklık (%)
    "mode": "auto",            # auto | gece | gunduz | custom
    "night_light": False,
    "night_light_strength": 15,
    "custom_values": {},       # özel modda monitör başına parlaklık
}
VALID_MODES = ("auto", "gece", "gunduz", "custom")

# DDC/CI çağrıları aynı anda tek iş parçacığından yapılsın (okuma ve yazma çakışmasın)
DDC_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Ayarlar ve günlük
# ----------------------------------------------------------------------------
def setup_logging():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        handler = RotatingFileHandler(LOG_PATH, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    except Exception:
        logging.basicConfig(level=logging.INFO)

    def _excepthook(exc_type, exc, tb):
        logging.critical("Yakalanmamış hata", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings["custom_values"] = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in DEFAULT_SETTINGS:
                if key in data:
                    settings[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning(f"Ayar dosyası okunamadı, varsayılanlar kullanılıyor: {e}")

    if settings["mode"] not in VALID_MODES:
        settings["mode"] = "auto"
    try:
        settings["min_brightness"] = max(0, min(50, int(settings["min_brightness"])))
        settings["night_light_strength"] = max(0, min(100, int(settings["night_light_strength"])))
    except (TypeError, ValueError):
        settings["min_brightness"] = DEFAULT_SETTINGS["min_brightness"]
        settings["night_light_strength"] = DEFAULT_SETTINGS["night_light_strength"]
    if not isinstance(settings["custom_values"], dict):
        settings["custom_values"] = {}
    return settings


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        logging.warning(f"Ayarlar kaydedilemedi: {e}")


def system_tz_offset():
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600.0 if offset is not None else 3.0


# ----------------------------------------------------------------------------
# Güneş hesabı (NOAA, çevrimdışı)
# ----------------------------------------------------------------------------
def solar_altitude(now, lat, lon, tz_offset):
    """Verilen yerel zamanda güneşin ufuktan yüksekliği (derece)."""
    day_of_year = now.timetuple().tm_yday
    gamma = (2 * math.pi / 365) * (day_of_year - 1 + (now.hour - 12) / 24)

    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))

    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))

    time_offset = eqtime + 4 * lon - 60 * tz_offset
    tst = now.hour * 60 + now.minute + now.second / 60.0 + time_offset

    ha_rad = math.radians((tst / 4.0) - 180.0)
    lat_rad = math.radians(lat)

    sin_alpha = (math.sin(lat_rad) * math.sin(decl) +
                 math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad))
    sin_alpha = max(-1.0, min(1.0, sin_alpha))
    return math.degrees(math.asin(sin_alpha))


def altitude_to_brightness(alpha_deg, min_brightness=0):
    if alpha_deg <= -6.0:
        brightness = 0
    elif alpha_deg >= 45.0:
        brightness = 100
    else:
        norm = (alpha_deg + 6.0) / 51.0
        brightness = int(round(100.0 * math.sin(norm * (math.pi / 2.0))))
    return max(min_brightness, min(100, brightness))


def get_offline_solar_brightness(lat=39.6484, lon=27.8826, tz_offset=3, min_brightness=0, now=None):
    now = now or datetime.now()
    return altitude_to_brightness(solar_altitude(now, lat, lon, tz_offset), min_brightness)


def day_curve(date, lat, lon, tz_offset, min_brightness, step_min=10):
    """Gün boyunca (00:00 - 24:00) parlaklık noktaları."""
    base = datetime(date.year, date.month, date.day)
    return [altitude_to_brightness(solar_altitude(base + timedelta(minutes=m), lat, lon, tz_offset), min_brightness)
            for m in range(0, 24 * 60 + 1, step_min)]


def sun_times(date, lat, lon, tz_offset):
    """Gün doğumu ve batımı (yerel saat). Kutup koşullarında None dönebilir."""
    base = datetime(date.year, date.month, date.day)
    rise = sunset = None
    prev = solar_altitude(base, lat, lon, tz_offset)
    for minute in range(1, 24 * 60):
        t = base + timedelta(minutes=minute)
        alt = solar_altitude(t, lat, lon, tz_offset)
        if prev < 0 <= alt and rise is None:
            rise = t
        if prev >= 0 > alt:
            sunset = t
        prev = alt
    return rise, sunset


# ----------------------------------------------------------------------------
# Vektör ikonlar (emoji yerine)
# ----------------------------------------------------------------------------
def glyph(kind, color, size=18):
    px = size * 2
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(color)
    c = QPointF(px / 2, px / 2)

    def rays(center, r_in, r_out, angles_deg):
        pen = QPen(col, px * 0.085)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for a in angles_deg:
            rad = math.radians(a)
            p.drawLine(QPointF(center.x() + r_in * math.cos(rad), center.y() - r_in * math.sin(rad)),
                       QPointF(center.x() + r_out * math.cos(rad), center.y() - r_out * math.sin(rad)))

    if kind == "sun":
        r = px * 0.19
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(c, r, r)
        rays(c, r + px * 0.1, r + px * 0.22, range(0, 360, 45))
    elif kind == "auto":
        hy = px * 0.70
        center = QPointF(px / 2, hy)
        r = px * 0.2
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPie(QRectF(center.x() - r, hy - r, 2 * r, 2 * r), 0, 180 * 16)
        rays(center, r + px * 0.09, r + px * 0.2, (25, 65, 115, 155))
        pen = QPen(col, px * 0.085)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(px * 0.1, hy + px * 0.1), QPointF(px * 0.9, hy + px * 0.1))
    elif kind == "moon":
        outer = QPainterPath()
        outer.addEllipse(c, px * 0.34, px * 0.34)
        cut = QPainterPath()
        cut.addEllipse(QPointF(c.x() + px * 0.18, c.y() - px * 0.12), px * 0.3, px * 0.3)
        p.fillPath(outer.subtracted(cut), col)
    elif kind == "flame":
        cx = px / 2
        path = QPainterPath()
        top = QPointF(cx, px * 0.08)
        path.moveTo(top)
        path.cubicTo(QPointF(cx + px * 0.08, px * 0.34), QPointF(cx + px * 0.33, px * 0.44), QPointF(cx + px * 0.3, px * 0.66))
        path.cubicTo(QPointF(cx + px * 0.28, px * 0.88), QPointF(cx - px * 0.28, px * 0.88), QPointF(cx - px * 0.3, px * 0.66))
        path.cubicTo(QPointF(cx - px * 0.33, px * 0.44), QPointF(cx - px * 0.08, px * 0.34), top)
        p.fillPath(path, col)

    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


# ----------------------------------------------------------------------------
# Gece ışığı katmanı
# ----------------------------------------------------------------------------
class NightLightOverlay(QWidget):
    def __init__(self, screen):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._opacity = 0.15
        self.setGeometry(screen.geometry())

    def show_overlay(self):
        self.show()

    def hide_overlay(self):
        self.hide()

    def set_opacity(self, value_0_to_100):
        self._opacity = max(0.0, min(0.25, value_0_to_100 / 400.0))
        self.update()

    def paintEvent(self, event: QPaintEvent):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(255, 140, 0, int(self._opacity * 255)))


# ----------------------------------------------------------------------------
# Arka plan iş parçacıkları
# ----------------------------------------------------------------------------
class HardwareWorker(QThread):
    """Parlaklık yazma işlerini arka planda yapar; aynı monitör için yalnızca en son değeri uygular."""

    def __init__(self):
        super().__init__()
        self.task_queue = queue.Queue()
        self._last_written = {}

    def add_task(self, monitor, value):
        self.task_queue.put((monitor, value))

    def set_known(self, monitor, value):
        """Ekrandan okunan gerçek değeri önbelleğe işle (monitörün kendi düğmeleriyle değişmiş olabilir)."""
        self._last_written[monitor] = value

    def run(self):
        stop = False
        while not stop:
            item = self.task_queue.get()  # bloklayıcı: boştayken uyanmaz
            if item is None:
                break
            latest = {item[0]: item[1]}
            while True:
                try:
                    nxt = self.task_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                latest[nxt[0]] = nxt[1]

            for monitor, value in latest.items():
                if self._last_written.get(monitor) == value:
                    continue
                try:
                    with DDC_LOCK:
                        sbc.set_brightness(value, display=monitor)
                    self._last_written[monitor] = value
                except Exception as e:
                    logging.warning(f"'{monitor}' donanım parlaklık ayarı başarısız: {e}")

    def stop(self):
        self.task_queue.put(None)
        self.wait(3000)


class MonitorScanner(QThread):
    """Monitör listesini ve mevcut parlaklıkları arayüzü bekletmeden okur."""
    scanned = pyqtSignal(list, dict)

    def run(self):
        values = {}
        try:
            with DDC_LOCK:
                monitors = list(sbc.list_monitors())
        except Exception as e:
            logging.error(f"Monitör listesi alınamadı: {e}")
            monitors = []

        for mon in monitors:
            try:
                with DDC_LOCK:
                    values[mon] = sbc.get_brightness(display=mon)[0]
            except Exception as e:
                logging.warning(f"'{mon}' parlaklığı okunamadı: {e}")
                values[mon] = None
        self.scanned.emit(monitors, values)


# ----------------------------------------------------------------------------
# Güneş eğrisi bileşeni
# ----------------------------------------------------------------------------
class SunCurve(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(58)
        self._points = []
        self._frac = 0.0

    def set_data(self, points, fraction_of_day):
        self._points = points
        self._frac = max(0.0, min(1.0, fraction_of_day))
        self.update()

    def paintEvent(self, event):
        if len(self._points) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 6.0, w - 6.0, 6.0, h - 15.0
        n = len(self._points)

        def pt(i):
            x = left + (right - left) * i / (n - 1)
            y = bottom - (bottom - top) * self._points[i] / 100.0
            return QPointF(x, y)

        line = QPainterPath(pt(0))
        for i in range(1, n):
            line.lineTo(pt(i))

        fill = QPainterPath(line)
        fill.lineTo(QPointF(right, bottom))
        fill.lineTo(QPointF(left, bottom))
        fill.closeSubpath()
        grad = QLinearGradient(0, top, 0, bottom)
        grad.setColorAt(0.0, QColor(0, 120, 215, 120))
        grad.setColorAt(1.0, QColor(0, 120, 215, 8))
        p.fillPath(fill, QBrush(grad))

        p.setPen(QPen(QColor("#3399FF"), 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)

        p.setPen(QColor("#7d7d7d"))
        font = QFont("Segoe UI")
        font.setPixelSize(9)
        p.setFont(font)
        for hour in (0, 6, 12, 18, 24):
            x = left + (right - left) * hour / 24.0
            box_x = min(max(x - 12, 0), w - 24)
            p.drawText(QRectF(box_x, bottom + 2, 24, 12), Qt.AlignCenter, f"{hour:02d}")

        # şimdiki an
        idx = self._frac * (n - 1)
        i0 = int(math.floor(idx))
        i1 = min(i0 + 1, n - 1)
        a, b = pt(i0), pt(i1)
        t = idx - i0
        now_pt = QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)
        p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.DashLine))
        p.drawLine(QPointF(now_pt.x(), top), QPointF(now_pt.x(), bottom))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFB300"))
        p.drawEllipse(now_pt, 3.8, 3.8)


# ----------------------------------------------------------------------------
# Ana pencere
# ----------------------------------------------------------------------------
STYLE = """
    QWidget { color: white; font-family: 'Segoe UI', sans-serif; }
    QLabel { background-color: transparent; }
    QFrame#Root {
        background-color: #202020;
        border: 1px solid #353535;
        border-radius: 14px;
    }
    QFrame#MonitorCard, QFrame#SunPanel {
        background-color: #2a2a2a;
        border-radius: 12px;
    }
    QSlider::groove:horizontal { border-radius: 4px; height: 8px; background: #3a3a3a; }
    QSlider::sub-page:horizontal { background: #0078D7; border-radius: 4px; height: 8px; }
    QSlider::handle:horizontal {
        background: #0078D7; width: 18px; height: 18px; margin: -5px 0;
        border-radius: 9px; border: 2px solid #0078D7;
    }
    QSlider::handle:horizontal:hover { background: #1e90ff; }
    QSlider::groove:horizontal:disabled { background: #2c2c2c; }
    QSlider::sub-page:horizontal:disabled { background: #4a4a4a; }
    QSlider::handle:horizontal:disabled { background: #5a5a5a; border: 2px solid #5a5a5a; }
    QPushButton {
        background-color: #333333; border-radius: 10px; padding: 6px 10px;
        font-weight: 500; font-size: 12px; color: white; border: 1px solid #3d3d3d;
    }
    QPushButton:hover { background-color: #444444; }
    QPushButton:pressed { background-color: #1c1c1c; }
"""

BTN_ACTIVE = "background-color: #0078D7; border-color: #005a9e; border-radius: 10px; font-weight: bold; color: white;"
BTN_DEFAULT = "background-color: #333333; border-color: #3d3d3d; border-radius: 10px; font-weight: 500; color: white;"


class BrightnessWindow(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.tray_icon = None
        self.on_update_callback = None
        self.anchor_geo = None

        self.night_light_overlays = []
        self.night_light_enabled = False
        self.monitor_controls = []  # (monitör, slider, değer etiketi)
        self._last_monitors = None
        self._pending_full_apply = True

        self.min_brightness = self.settings["min_brightness"]
        self.current_mode = self.settings["mode"]
        self.custom_values = {str(k): v for k, v in self.settings["custom_values"].items()}
        self.target_value = None
        self._sun_cache_key = None
        self._sun_points = []
        self._sun_times = (None, None)

        self.hardware_worker = HardwareWorker()
        self.hardware_worker.start()
        self.scanner = MonitorScanner()
        self.scanner.scanned.connect(self.on_scanned)

        self.setStyleSheet(STYLE)

        self.save_timer = QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(600)
        self.save_timer.timeout.connect(lambda: save_settings(self.settings))

        self.fade_timer = QTimer()
        self.fade_timer.setInterval(100)
        self.fade_timer.timeout.connect(self.fade_step)

        self.auto_mode_check_timer = QTimer()
        self.auto_mode_check_timer.setInterval(60000)
        self.auto_mode_check_timer.timeout.connect(self._minute_tick)
        self.auto_mode_check_timer.start()

        self.initUI()
        self.update_button_styles()
        self.request_scan()

    # ---- ayarlar ----
    def schedule_save(self):
        self.save_timer.start()

    def _tz(self):
        tz = self.settings.get("tz_offset")
        return float(tz) if tz is not None else system_tz_offset()

    # ---- arayüz ----
    def initUI(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        outer.setSizeConstraint(QLayout.SetFixedSize)  # pencere içeriğe göre boyutlanır

        self.root = QFrame()
        self.root.setObjectName("Root")
        self.root.setFixedWidth(340)
        shadow = QGraphicsDropShadowEffect(self.root)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.root.setGraphicsEffect(shadow)
        outer.addWidget(self.root)

        self.main_layout = QVBoxLayout(self.root)
        self.main_layout.setContentsMargins(15, 15, 15, 15)
        self.main_layout.setSpacing(0)

        # mod düğmeleri
        preset_layout = QHBoxLayout()
        preset_layout.setSpacing(6)
        self.auto_btn = QPushButton(" Güneş")
        self.gece_btn = QPushButton(" Gece")
        self.gunduz_btn = QPushButton(" Gündüz")
        self._btn_kinds = [(self.auto_btn, "auto"), (self.gece_btn, "moon"), (self.gunduz_btn, "sun")]
        for btn, _ in self._btn_kinds:
            btn.setIconSize(QSize(16, 16))
            btn.setCursor(Qt.PointingHandCursor)
            preset_layout.addWidget(btn, 1)
        self.auto_btn.clicked.connect(self.apply_auto_mode)
        self.gece_btn.clicked.connect(lambda: self.on_preset_click("gece"))
        self.gunduz_btn.clicked.connect(lambda: self.on_preset_click("gunduz"))
        self.main_layout.addLayout(preset_layout)

        self.custom_mode_lbl = QLabel("")
        self.custom_mode_lbl.setAlignment(Qt.AlignCenter)
        self.custom_mode_lbl.setStyleSheet("color: #3399FF; font-size: 11px; font-weight: 600;")
        self.custom_mode_lbl.setFixedHeight(18)
        self.main_layout.addWidget(self.custom_mode_lbl)

        # güneş paneli
        self.sun_panel = QFrame()
        self.sun_panel.setObjectName("SunPanel")
        sun_layout = QVBoxLayout(self.sun_panel)
        sun_layout.setContentsMargins(10, 8, 10, 6)
        sun_layout.setSpacing(2)
        self.sun_info_lbl = QLabel("")
        self.sun_info_lbl.setStyleSheet("color: #c8c8c8; font-size: 11px; font-weight: 500;")
        self.sun_times_lbl = QLabel("")
        self.sun_times_lbl.setStyleSheet("color: #8a8a8a; font-size: 10px;")
        self.sun_curve = SunCurve()
        sun_layout.addWidget(self.sun_info_lbl)
        sun_layout.addWidget(self.sun_curve)
        sun_layout.addWidget(self.sun_times_lbl)
        self.main_layout.addWidget(self.sun_panel)
        self.main_layout.addSpacing(10)

        # gece ışığı
        night_light_header = QHBoxLayout()
        flame = QLabel()
        flame.setPixmap(glyph("flame", "#FF9F2E", 16).pixmap(16, 16))
        self.night_light_lbl = QLabel("Gece Işığı")
        self.night_light_lbl.setStyleSheet("color: #d0d0d0; font-size: 12px; font-weight: 500;")
        self.night_light_btn = QPushButton("●")
        self.night_light_btn.setCheckable(True)
        self.night_light_btn.setFixedSize(44, 24)
        self.night_light_btn.setCursor(Qt.PointingHandCursor)
        self.night_light_btn.clicked.connect(self.toggle_night_light)
        night_light_header.addWidget(flame)
        night_light_header.addSpacing(6)
        night_light_header.addWidget(self.night_light_lbl)
        night_light_header.addStretch()
        night_light_header.addWidget(self.night_light_btn)
        self.main_layout.addLayout(night_light_header)
        self.main_layout.addSpacing(6)

        self.night_light_slider = QSlider(Qt.Horizontal)
        self.night_light_slider.setRange(0, 100)
        self.night_light_slider.setValue(self.settings["night_light_strength"])
        self.night_light_slider.setEnabled(False)
        self.night_light_slider.valueChanged.connect(self.update_night_light_opacity)
        self.main_layout.addWidget(self.night_light_slider)
        self.main_layout.addSpacing(10)

        self.monitors_layout = QVBoxLayout()
        self.monitors_layout.setSpacing(8)
        self.main_layout.addLayout(self.monitors_layout)
        self._show_monitor_message("Monitörler aranıyor…", "#a0a0a0")

        self.update_night_light_switch_style()
        if self.settings["night_light"]:
            self.night_light_btn.setChecked(True)
            self.toggle_night_light()

    def _show_monitor_message(self, text, color):
        self._clear_layout(self.monitors_layout)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 500;")
        lbl.setAlignment(Qt.AlignCenter)
        self.monitors_layout.addWidget(lbl)

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    # ---- tepsi bilgisi ----
    def update_tooltip(self):
        if not self.tray_icon:
            return
        mode_names = {"auto": "Güneş Modu", "gece": "Gece Modu", "gunduz": "Gündüz Modu", "custom": "Özel Mod"}
        lines = [f"Parlaklık Kontrolü ({mode_names.get(self.current_mode, 'Özel Mod')})"]
        if self.monitor_controls:
            for mon, slider, _ in self.monitor_controls:
                lines.append(f"• {mon}: %{slider.value()}")
        else:
            lines.append("• Monitör Bulunamadı")
        self.tray_icon.setToolTip("\n".join(lines))

    def update_button_styles(self):
        for btn, kind in self._btn_kinds:
            mode = {"auto": "auto", "moon": "gece", "sun": "gunduz"}[kind]
            active = self.current_mode == mode
            btn.setStyleSheet(BTN_ACTIVE if active else BTN_DEFAULT)
            btn.setIcon(glyph(kind, "#ffffff" if active else "#c8c8c8", 16))
        self.custom_mode_lbl.setText("● Özel Mod Aktif" if self.current_mode == "custom" else "")
        self.update_tooltip()

    # ---- güneş ----
    def _sun_args(self):
        return self.settings["lat"], self.settings["lon"], self._tz()

    def calculate_auto_brightness(self):
        lat, lon, tz = self._sun_args()
        return get_offline_solar_brightness(lat, lon, tz, self.min_brightness)

    def update_sun_panel(self):
        now = datetime.now()
        lat, lon, tz = self._sun_args()
        key = (now.date(), self.min_brightness, lat, lon, tz)
        if key != self._sun_cache_key:
            self._sun_points = day_curve(now.date(), lat, lon, tz, self.min_brightness)
            self._sun_times = sun_times(now.date(), lat, lon, tz)
            self._sun_cache_key = key

        alt = solar_altitude(now, lat, lon, tz)
        target = self.calculate_auto_brightness()
        if alt <= -6:
            self.sun_info_lbl.setText(f"Gece · otomatik hedef %{target}")
        elif alt < 0:
            self.sun_info_lbl.setText(f"Alacakaranlık · otomatik hedef %{target}")
        else:
            self.sun_info_lbl.setText(f"Güneş {alt:.0f}° yükseklikte · otomatik hedef %{target}")

        rise, sunset = self._sun_times
        if rise and sunset:
            self.sun_times_lbl.setText(f"↑ Gün doğumu {rise:%H:%M}      ↓ Gün batımı {sunset:%H:%M}")
        else:
            self.sun_times_lbl.setText("")
        frac = (now.hour * 60 + now.minute + now.second / 60.0) / 1440.0
        self.sun_curve.set_data(self._sun_points, frac)

    def set_min_brightness(self, value):
        self.min_brightness = int(value)
        self.settings["min_brightness"] = self.min_brightness
        self.schedule_save()
        self.update_sun_panel()
        if self.current_mode in ("auto", "gece"):
            self.apply_preset(self.mode_target(self.current_mode))
            self._notify()

    def mode_target(self, mode):
        if mode == "auto":
            return self.calculate_auto_brightness()
        if mode == "gece":
            return self.min_brightness
        return 100

    # ---- gece ışığı ----
    def rebuild_night_light_overlays(self):
        for overlay in self.night_light_overlays:
            overlay.close()
        self.night_light_overlays.clear()
        for screen in QApplication.screens():
            overlay = NightLightOverlay(screen)
            overlay.set_opacity(self.night_light_slider.value())
            self.night_light_overlays.append(overlay)

    def update_night_light_switch_style(self):
        if self.night_light_enabled:
            style = """
                QPushButton {
                    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFA733, stop:1 #FF8C00);
                    border: 1px solid #e67e22; border-radius: 12px; color: white;
                    font-size: 15px; text-align: right; padding-right: 3px;
                }
                QPushButton:hover {
                    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFB74D, stop:1 #FF9800);
                }
            """
        else:
            style = """
                QPushButton {
                    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3d3d3d, stop:1 #333333);
                    border: 1px solid #4a4a4a; border-radius: 12px; color: #9a9a9a;
                    font-size: 15px; text-align: left; padding-left: 3px;
                }
                QPushButton:hover {
                    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #464646, stop:1 #3a3a3a);
                }
            """
        self.night_light_btn.setStyleSheet(style)

    def toggle_night_light(self):
        self.night_light_enabled = self.night_light_btn.isChecked()
        self.night_light_slider.setEnabled(self.night_light_enabled)
        self.update_night_light_switch_style()

        if self.night_light_enabled:
            self.rebuild_night_light_overlays()
            for overlay in self.night_light_overlays:
                overlay.show_overlay()
            if self.isVisible():
                self.raise_()
                self.activateWindow()
        else:
            for overlay in self.night_light_overlays:
                overlay.hide_overlay()

        self.settings["night_light"] = self.night_light_enabled
        self.schedule_save()

    def update_night_light_opacity(self, value):
        self.settings["night_light_strength"] = int(value)
        self.schedule_save()
        if not self.night_light_overlays:
            return
        for overlay in self.night_light_overlays:
            overlay.set_opacity(value)
        if self.isVisible():
            self.raise_()
            self.activateWindow()

    def _set_night_light(self, enabled):
        if self.night_light_enabled != enabled:
            self.night_light_btn.setChecked(enabled)
            self.toggle_night_light()

    # ---- modlar ----
    def _set_mode(self, mode):
        self.current_mode = mode
        self.settings["mode"] = mode
        self.schedule_save()
        self.update_button_styles()

    def _notify(self):
        if self.on_update_callback:
            self.on_update_callback()

    def apply_auto_mode(self):
        self._set_mode("auto")
        target = self.calculate_auto_brightness()
        logging.info(f"Güneş Modu Aktif -> Hedef Parlaklık: %{target}")
        self.apply_preset(target)
        self._set_night_light(False)
        self.update_sun_panel()
        self._notify()

    def on_preset_click(self, mode):
        self._set_mode(mode)
        self.apply_preset(self.mode_target(mode))
        self._set_night_light(mode == "gece")
        self._notify()

    def reapply_mode(self):
        """Başlangıçta ve monitör listesi değişince kayıtlı modu uygular."""
        if self.current_mode == "custom":
            for mon, slider, _ in self.monitor_controls:
                saved = self.custom_values.get(str(mon))
                if isinstance(saved, (int, float)):
                    slider.setValue(max(0, min(100, int(saved))))
        else:
            self.apply_preset(self.mode_target(self.current_mode))

    def _minute_tick(self):
        if self.current_mode == "auto":
            target = self.calculate_auto_brightness()
            if self.target_value != target:
                logging.info(f"Güneş açısı değişti, parlaklık güncelleniyor: %{target}")
                self.apply_preset(target)
        if self.isVisible():
            self.update_sun_panel()

    # ---- monitörler ----
    def request_scan(self):
        if not self.scanner.isRunning():
            self.scanner.start()

    def refresh_monitors(self):
        self._last_monitors = None  # arayüzü zorla yeniden kur
        self.request_scan()

    def on_scanned(self, monitors, values):
        for mon, bright in values.items():
            if bright is not None:
                self.hardware_worker.set_known(mon, bright)
        if monitors != self._last_monitors:
            self.build_monitor_controls(monitors, values)
            self._last_monitors = list(monitors)
            if self.night_light_enabled:
                self.rebuild_night_light_overlays()
                for overlay in self.night_light_overlays:
                    overlay.show_overlay()
            self.reapply_mode()
        elif not self.fade_timer.isActive():
            for mon, slider, val_lbl in self.monitor_controls:
                bright = values.get(mon)
                if bright is None:
                    continue
                slider.blockSignals(True)
                slider.setValue(bright)
                slider.blockSignals(False)
                val_lbl.setText(f"%{bright}")
            if self.current_mode == "auto":
                target = self.calculate_auto_brightness()
                if self.target_value != target:
                    self.apply_preset(target)
        self.update_tooltip()
        self._notify()

    def build_monitor_controls(self, monitors, values):
        self._clear_layout(self.monitors_layout)
        self.monitor_controls.clear()

        if not monitors:
            lbl = QLabel("Monitör Bulunamadı")
            lbl.setStyleSheet("color: #ff6b6b; font-size: 13px; font-weight: 500;")
            lbl.setAlignment(Qt.AlignCenter)
            self.monitors_layout.addWidget(lbl)
            self._relayout()
            return

        for i, mon in enumerate(monitors, start=1):
            current = values.get(mon)
            if current is None:
                current = 50
            display_name = mon if mon and str(mon).strip() and "none" not in str(mon).lower() else f"Monitör {i}"

            frame = QFrame()
            frame.setObjectName("MonitorCard")
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(12, 10, 12, 12)
            frame_layout.setSpacing(8)

            header = QHBoxLayout()
            lbl = QLabel(display_name)
            lbl.setStyleSheet("color: #a0a0a0; font-size: 12px; font-weight: 500;")
            val_lbl = QLabel(f"%{current}")
            val_lbl.setStyleSheet("color: #3399FF; font-size: 14px; font-weight: bold;")
            val_lbl.setAlignment(Qt.AlignRight)
            header.addWidget(lbl)
            header.addStretch()
            header.addWidget(val_lbl)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(current)
            slider.valueChanged.connect(lambda val, m=mon, vl=val_lbl: self.on_slider_changed(m, val, vl))
            slider.sliderPressed.connect(self.on_manual_slider_interaction)
            slider.actionTriggered.connect(lambda _a: self.on_manual_slider_interaction())  # klavye ve tekerlek

            self.monitor_controls.append((mon, slider, val_lbl))
            frame_layout.addLayout(header)
            frame_layout.addWidget(slider)
            self.monitors_layout.addWidget(frame)

        self._relayout()

    def _relayout(self):
        self.update_tooltip()
        self.adjustSize()
        if self.isVisible():
            self.reposition()

    def on_manual_slider_interaction(self):
        if self.current_mode != "custom":
            self._set_mode("custom")
            self._remember_custom_values()
            self._notify()

    def _remember_custom_values(self):
        self.custom_values = {str(mon): slider.value() for mon, slider, _ in self.monitor_controls}
        self.settings["custom_values"] = self.custom_values
        self.schedule_save()

    def on_slider_changed(self, monitor, value, val_label):
        val_label.setText(f"%{value}")
        self.hardware_worker.add_task(monitor, value)
        if self.current_mode == "custom":
            self.custom_values[str(monitor)] = value
            self.settings["custom_values"] = self.custom_values
            self.schedule_save()
        self._notify()

    # ---- geçiş efekti ----
    def apply_preset(self, target_value):
        self.target_value = target_value
        self.fade_timer.start(100)

    def fade_step(self):
        all_done = True
        step_size = 3
        for mon, slider, val_lbl in self.monitor_controls:
            current = slider.value()
            if current != self.target_value:
                if current < self.target_value:
                    new_val = min(current + step_size, self.target_value)
                else:
                    new_val = max(current - step_size, self.target_value)
                slider.setValue(new_val)
                all_done = False
        if all_done:
            self.fade_timer.stop()

    # ---- pencere ----
    def reposition(self):
        geo = self.anchor_geo or QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        gap = 10
        x = geo.right() + 1 - self.width() + SHADOW - gap
        y = geo.bottom() + 1 - self.height() + SHADOW - gap
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        self.update_sun_panel()
        self.update_tooltip()
        self.request_scan()
        self.reposition()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def cleanup_overlays(self):
        for overlay in self.night_light_overlays:
            overlay.close()
        self.night_light_overlays.clear()


# ----------------------------------------------------------------------------
# Sistem tepsisi uygulaması
# ----------------------------------------------------------------------------
class SystemTrayApp:
    def __init__(self):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.server_key = SINGLE_INSTANCE_KEY
        self.check_single_instance()

        self.settings = load_settings()
        self.window = BrightnessWindow(self.settings)
        self.app_name = "ParlaklikKontrol_App"

        self.tray = QSystemTrayIcon()
        self.window.tray_icon = self.tray

        # ikon yalnızca değişiklik olunca, kısa bir toplama süresiyle çizilir
        self._icon_key = None
        self._icon_timer = QTimer()
        self._icon_timer.setSingleShot(True)
        self._icon_timer.setInterval(150)
        self._icon_timer.timeout.connect(self._update_icon)
        self.window.on_update_callback = self.request_icon_update
        self._update_icon()

        self.tray.setVisible(True)
        self.tray.activated.connect(self.on_tray_click)

        self.menu = QMenu()

        self.refresh_action = QAction("Monitörleri Yenile", self.menu)
        self.refresh_action.triggered.connect(self.window.refresh_monitors)
        self.menu.addAction(self.refresh_action)

        self.floor_menu = self.menu.addMenu("Gece taban parlaklığı")
        self.floor_group = QActionGroup(self.floor_menu)
        self.floor_group.setExclusive(True)
        for value in (0, 5, 10, 15, 20, 30):
            action = QAction(f"%{value}", self.floor_menu)
            action.setCheckable(True)
            action.setChecked(value == self.window.min_brightness)
            action.triggered.connect(lambda _checked, v=value: self.window.set_min_brightness(v))
            self.floor_group.addAction(action)
            self.floor_menu.addAction(action)

        self.menu.addSeparator()

        self.startup_action = QAction("Windows ile Başlat", self.menu)
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(self.check_startup_status())
        self.startup_action.triggered.connect(self.toggle_startup)
        self.menu.addAction(self.startup_action)

        self.menu.addSeparator()

        quit_action = QAction("Çıkış", self.menu)
        quit_action.triggered.connect(self.quit_app)
        self.menu.addAction(quit_action)

        self.tray.setContextMenu(self.menu)

    def check_single_instance(self):
        socket = QLocalSocket()
        socket.connectToServer(self.server_key)
        if socket.waitForConnected(500):
            socket.write(b"SHOW")
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            sys.exit(0)

        QLocalServer.removeServer(self.server_key)
        self.local_server = QLocalServer()
        self.local_server.newConnection.connect(self.handle_new_connection)
        self.local_server.listen(self.server_key)

    def handle_new_connection(self):
        client = self.local_server.nextPendingConnection()
        if client:
            client.readyRead.connect(lambda: self.on_client_ready_read(client))

    def on_client_ready_read(self, client):
        data = client.readAll().data()
        if b"SHOW" in data:
            self.show_window()
        client.disconnectFromServer()

    def show_window(self):
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self.window.anchor_geo = screen.availableGeometry()
        self.window.reposition()
        self.window.show()
        self.window.reposition()
        self.window.raise_()
        self.window.activateWindow()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_window()
        elif reason == QSystemTrayIcon.MiddleClick:
            self.window.apply_auto_mode()

    def check_startup_status(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as registry_key:
                winreg.QueryValueEx(registry_key, self.app_name)
            return True
        except (FileNotFoundError, OSError):
            return False

    def toggle_startup(self, state):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as registry_key:
                if state:
                    script_path = os.path.abspath(sys.argv[0])
                    if script_path.endswith('.pyw') or script_path.endswith('.py'):
                        pythonw_exe = sys.executable.replace("python.exe", "pythonw.exe")
                        command = f'"{pythonw_exe}" "{script_path}"'
                    else:
                        command = f'"{script_path}"'
                    winreg.SetValueEx(registry_key, self.app_name, 0, winreg.REG_SZ, command)
                else:
                    winreg.DeleteValue(registry_key, self.app_name)
        except Exception as e:
            logging.error(f"Başlangıç ayarı değiştirilirken hata oluştu: {e}")

    # ---- tepsi ikonu ----
    def request_icon_update(self):
        if not self._icon_timer.isActive():
            self._icon_timer.start()

    def _average_brightness(self):
        controls = self.window.monitor_controls
        if not controls:
            return 50
        return sum(s.value() for _, s, _ in controls) / len(controls)

    def _update_icon(self):
        mode = self.window.current_mode
        avg = self._average_brightness()
        key = (int(round(avg)), mode)
        if key != self._icon_key:
            self._icon_key = key
            self.tray.setIcon(self.create_icon(avg, mode))
        self.window.update_tooltip()

    def create_icon(self, brightness=75, mode="auto"):
        scale = 2
        pixmap = QPixmap(32 * scale, 32 * scale)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(scale, scale)

        # Parlaklık 0-100 → renk: soluk gri (#828282) → altın (#FFB300)
        t = max(0.0, min(1.0, brightness / 100.0))
        sun_color = QColor(int(130 + 125 * t), int(130 + 49 * t), int(130 - 130 * t))
        if mode == "gece":
            sun_color = QColor(140, 145, 155)

        cx, cy = 16.0, 16.0
        r_circle = 5.5 + 2.5 * t
        ray_inner = r_circle + 2.5
        ray_outer = ray_inner + 4.0 + 1.5 * t
        outline_color = QColor(0, 0, 0, 130)

        ray_outline_pen = QPen(outline_color)
        ray_outline_pen.setWidthF(2.6)
        ray_outline_pen.setCapStyle(Qt.RoundCap)
        ray_pen = QPen(sun_color)
        ray_pen.setWidthF(1.6)
        ray_pen.setCapStyle(Qt.RoundCap)

        painter.setPen(Qt.NoPen)
        painter.setBrush(outline_color)
        painter.drawEllipse(QPointF(cx, cy), r_circle + 0.9, r_circle + 0.9)
        painter.setBrush(sun_color)
        painter.drawEllipse(QPointF(cx, cy), r_circle, r_circle)

        for active_pen in (ray_outline_pen, ray_pen):
            painter.setPen(active_pen)
            painter.setBrush(Qt.NoBrush)
            for i in range(6):
                angle = math.radians(i * 60)
                painter.drawLine(QPointF(cx + ray_inner * math.cos(angle), cy + ray_inner * math.sin(angle)),
                                 QPointF(cx + ray_outer * math.cos(angle), cy + ray_outer * math.sin(angle)))

        painter.end()
        return QIcon(pixmap)

    def quit_app(self):
        save_settings(self.settings)
        if hasattr(self, 'local_server'):
            self.local_server.close()
            QLocalServer.removeServer(self.server_key)
        self.window.cleanup_overlays()
        self.window.hardware_worker.stop()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec_())


if __name__ == "__main__":
    setup_logging()
    tray_app = SystemTrayApp()
    tray_app.run()
