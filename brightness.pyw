import sys
import os
import winreg
import logging
import math
import queue
from datetime import datetime
import screen_brightness_control as sbc
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QSlider, QLabel, QPushButton, QSystemTrayIcon, QMenu, QAction, QFrame)
from PyQt5.QtCore import Qt, QTimer, QThread
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QCursor, QPaintEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def get_offline_solar_brightness(lat=39.6484, lon=27.8826, tz_offset=3):
    now = datetime.now()
    day_of_year = now.timetuple().tm_yday
    hour = now.hour
    minute = now.minute
    second = now.second

    gamma = (2 * math.pi / 365) * (day_of_year - 1 + (hour - 12) / 24)

    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                      - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))

    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))

    time_offset = eqtime + 4 * lon - 60 * tz_offset
    tst = hour * 60 + minute + second / 60.0 + time_offset

    ha_deg = (tst / 4.0) - 180.0
    ha_rad = math.radians(ha_deg)

    lat_rad = math.radians(lat)

    sin_alpha = (math.sin(lat_rad) * math.sin(decl) +
                 math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad))
    sin_alpha = max(-1.0, min(1.0, sin_alpha))
    alpha_deg = math.degrees(math.asin(sin_alpha))

    if alpha_deg <= -6.0:
        brightness = 0
    elif alpha_deg >= 45.0:
        brightness = 100
    else:
        norm = (alpha_deg - (-6.0)) / (45.0 - (-6.0))
        brightness = int(round(100.0 * math.sin(norm * (math.pi / 2.0))))

    return max(0, min(100, brightness))


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


class HardwareWorker(QThread):
    def __init__(self):
        super().__init__()
        self.task_queue = queue.Queue()
        self.running = True

    def add_task(self, monitor, value):
        self.task_queue.put((monitor, value))

    def run(self):
        while self.running:
            try:
                task = self.task_queue.get(timeout=0.2)
                monitor, value = task
                latest_tasks = {monitor: value}
                while not self.task_queue.empty():
                    try:
                        m, v = self.task_queue.get_nowait()
                        latest_tasks[m] = v
                    except queue.Empty:
                        break

                for m, v in latest_tasks.items():
                    try:
                        sbc.set_brightness(v, display=m)
                    except Exception as e:
                        logging.warning(f"'{m}' donanım parlaklık ayarı başarısız: {e}")
            except queue.Empty:
                continue

    def stop(self):
        self.running = False
        self.wait()


class BrightnessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.setFixedSize(340, 310)
        
        self.tray_icon = None
        self.night_light_overlays = []
        self.night_light_enabled = False
        
        self.hardware_worker = HardwareWorker()
        self.hardware_worker.start()

        self.setStyleSheet("""
            QWidget {
                background-color: #202020;
                color: white;
                border-radius: 10px;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#MonitorCard {
                background-color: #2a2a2a;
                border-radius: 8px;
                padding: 10px;
            }
            QFrame#MonitorCard QLabel {
                background-color: transparent;
            }
            QFrame#MonitorCard QSlider {
                background-color: transparent;
            }
            QSlider::groove:horizontal {
                border-radius: 4px;
                height: 8px;
                background: #3a3a3a;
            }
            QSlider::sub-page:horizontal {
                background: #0078D7;
                border-radius: 4px;
                height: 8px;
            }
            QSlider::handle:horizontal {
                background: #0078D7;
                width: 18px;
                height: 18px;
                margin: -5px 0;
                border-radius: 9px;
                border: 2px solid #0078D7;
            }
            QSlider::handle:horizontal:hover {
                background: #1e90ff;
            }
            QSlider::disabled {
                opacity: 0.5;
            }
            QPushButton {
                background-color: #333333;
                border-radius: 8px;
                padding: 6px 10px;
                font-weight: 500;
                font-size: 12px;
                color: white;
                border: 1px solid #3d3d3d;
            }
            QPushButton:hover { background-color: #444444; }
            QPushButton:pressed { background-color: #1c1c1c; }
        """)

        self.monitor_controls = []
        self.current_mode = "auto"
        self.on_update_callback = None
        
        self.target_value = 0
        self.fade_timer = QTimer()
        self.fade_timer.setInterval(100)
        self.fade_timer.timeout.connect(self.fade_step)
        
        self.hourly_check_timer = QTimer()
        self.hourly_check_timer.setInterval(60000)
        self.hourly_check_timer.timeout.connect(self._check_auto_mode)
        self.hourly_check_timer.start()

        self.initUI()
        self.apply_auto_mode()

    def initUI(self):
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(15, 15, 15, 15)

        preset_layout = QHBoxLayout()
        preset_layout.setSpacing(6)

        self.auto_btn = QPushButton("🌅 Güneş Modu")
        self.auto_btn.clicked.connect(self.apply_auto_mode)

        self.gece_btn = QPushButton("🌙 Gece")
        self.gece_btn.clicked.connect(lambda: self.on_preset_click("gece", 0))

        self.gunduz_btn = QPushButton("☀️ Gündüz")
        self.gunduz_btn.clicked.connect(lambda: self.on_preset_click("gunduz", 100))

        preset_layout.addWidget(self.auto_btn, 1)
        preset_layout.addWidget(self.gece_btn, 1)
        preset_layout.addWidget(self.gunduz_btn, 1)
        self.main_layout.addLayout(preset_layout)

        self.main_layout.addSpacing(10)
        
        night_light_layout = QVBoxLayout()
        night_light_layout.setSpacing(4)
        self.night_light_btn = QPushButton("🔥 Gece Işığı")
        self.night_light_btn.setCheckable(True)
        self.night_light_btn.clicked.connect(self.toggle_night_light)
        
        self.night_light_slider = QSlider(Qt.Horizontal)
        self.night_light_slider.setRange(0, 100)
        self.night_light_slider.setValue(15)
        self.night_light_slider.setEnabled(False)
        self.night_light_slider.valueChanged.connect(self.update_night_light_opacity)
        
        night_light_layout.addWidget(self.night_light_btn)
        night_light_layout.addWidget(self.night_light_slider)
        self.main_layout.addLayout(night_light_layout)

        self.main_layout.addSpacing(10)

        self.monitors_layout = QVBoxLayout()
        self.main_layout.addLayout(self.monitors_layout)
        self.setLayout(self.main_layout)

        self.refresh_monitors()

    def update_tooltip(self):
        if not self.tray_icon:
            return
            
        mode_names = {
            "auto": "Güneş Modu",
            "gece": "Gece Modu",
            "gunduz": "Gündüz Modu",
            "custom": "Özel Mod"
        }
        mode_str = mode_names.get(self.current_mode, "Özel Mod")
        
        lines = [f"Parlaklık Kontrolü ({mode_str})"]
        if self.monitor_controls:
            for mon, slider, _ in self.monitor_controls:
                lines.append(f"• {mon}: %{slider.value()}")
        else:
            lines.append("• Monitör Bulunamadı")
            
        self.tray_icon.setToolTip("\n".join(lines))

    def update_button_styles(self):
        active_style = "background-color: #0078D7; border-color: #005a9e; border-radius: 8px; font-weight: bold; color: white;"
        default_style = "background-color: #333333; border-color: #3d3d3d; border-radius: 8px; font-weight: 500; color: white;"

        self.auto_btn.setStyleSheet(active_style if self.current_mode == "auto" else default_style)
        self.gece_btn.setStyleSheet(active_style if self.current_mode == "gece" else default_style)
        self.gunduz_btn.setStyleSheet(active_style if self.current_mode == "gunduz" else default_style)
        self.update_tooltip()

    def calculate_auto_brightness(self):
        return get_offline_solar_brightness(lat=39.6484, lon=27.8826, tz_offset=3)

    def toggle_night_light(self):
        self.night_light_enabled = self.night_light_btn.isChecked()
        self.night_light_slider.setEnabled(self.night_light_enabled)
        
        if self.night_light_enabled:
            self.night_light_btn.setStyleSheet("background-color: #FF8C00; border-color: #e67e22; border-radius: 8px; font-weight: bold; color: white;")
            if not self.night_light_overlays:
                for screen in QApplication.screens():
                    overlay = NightLightOverlay(screen)
                    overlay.set_opacity(self.night_light_slider.value())
                    self.night_light_overlays.append(overlay)
                    overlay.show_overlay()
            else:
                for overlay in self.night_light_overlays:
                    overlay.show_overlay()
            self.raise_()
            self.activateWindow()
        else:
            self.night_light_btn.setStyleSheet("")
            for overlay in self.night_light_overlays:
                overlay.hide_overlay()
                
    def update_night_light_opacity(self, value):
        if not self.night_light_overlays:
            return
        for overlay in self.night_light_overlays:
            overlay.set_opacity(value)
        self.raise_()
        self.activateWindow()

    def apply_auto_mode(self):
        self.current_mode = "auto"
        self.update_button_styles()
        target = self.calculate_auto_brightness()
        logging.info(f"Güneş Modu Aktif -> Hedef Parlaklık: %{target}")
        self.apply_preset(target)
        
        if self.night_light_enabled:
            self.night_light_btn.setChecked(False)
            self.toggle_night_light()
            
        if self.on_update_callback:
            self.on_update_callback()

    def on_preset_click(self, mode, target_val):
        self.current_mode = mode
        self.update_button_styles()
        self.apply_preset(target_val)
        
        if mode == "gece" and not self.night_light_enabled:
            self.night_light_btn.setChecked(True)
            self.toggle_night_light()
        elif mode == "gunduz" and self.night_light_enabled:
            self.night_light_btn.setChecked(False)
            self.toggle_night_light()
            
        if self.on_update_callback:
            self.on_update_callback()

    def _check_auto_mode(self):
        if self.current_mode == "auto":
            target = self.calculate_auto_brightness()
            if self.target_value != target:
                logging.info(f"Güneş açısı değişti, parlaklık güncelleniyor: %{target}")
                self.apply_preset(target)

    def refresh_monitors(self):
        while self.monitors_layout.count():
            item = self.monitors_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub_item = item.layout().takeAt(0)
                    if sub_item.widget():
                        sub_item.widget().deleteLater()

        self.monitor_controls.clear()

        try:
            monitors = sbc.list_monitors()
        except Exception as e:
            logging.error(f"Monitör listesi alınamadı: {e}")
            monitors = []

        if not monitors:
            lbl = QLabel("⚠️ Monitör Bulunamadı")
            lbl.setStyleSheet("color: #ff6b6b; font-size: 13px; font-weight: 500;")
            lbl.setAlignment(Qt.AlignCenter)
            self.monitors_layout.addWidget(lbl)
            self.update_tooltip()
            return

        for i, mon in enumerate(monitors, start=1):
            try:
                current_bright = sbc.get_brightness(display=mon)[0]
            except Exception as e:
                logging.warning(f"'{mon}' parlaklığı okunamadı, varsayılan %50 atandı: {e}")
                current_bright = 50

            display_name = mon if mon and str(mon).strip() and "none" not in str(mon).lower() else f"Monitör {i}"

            frame = QFrame()
            frame.setObjectName("MonitorCard")
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(0, 0, 0, 0)
            frame_layout.setSpacing(8)

            header_layout = QHBoxLayout()
            lbl = QLabel(display_name)
            lbl.setStyleSheet("color: #a0a0a0; font-size: 12px; font-weight: 500;")
            
            val_lbl = QLabel(f"%{current_bright}")
            val_lbl.setStyleSheet("color: #3399FF; font-size: 14px; font-weight: bold;")
            val_lbl.setAlignment(Qt.AlignRight)

            header_layout.addWidget(lbl)
            header_layout.addStretch()
            header_layout.addWidget(val_lbl)
            
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(current_bright)
            
            slider.valueChanged.connect(lambda val, m=mon, vl=val_lbl: self.on_slider_changed(m, val, vl))
            slider.sliderPressed.connect(self.on_manual_slider_interaction)

            self.monitor_controls.append((mon, slider, val_lbl))
            
            frame_layout.addLayout(header_layout)
            frame_layout.addWidget(slider)
            
            self.monitors_layout.addWidget(frame)
            self.monitors_layout.addSpacing(8)

        self.update_tooltip()
        if self.on_update_callback:
            self.on_update_callback()

    def on_manual_slider_interaction(self):
        if self.current_mode != "custom":
            self.current_mode = "custom"
            self.update_button_styles()
            if self.on_update_callback:
                self.on_update_callback()

    def showEvent(self, event):
        super().showEvent(event)
        
        try:
            current_monitors = sbc.list_monitors()
        except Exception:
            current_monitors = []
            
        saved_monitors = [m for m, _, _ in self.monitor_controls]
        if current_monitors != saved_monitors:
            self.refresh_monitors()
        else:
            for mon, slider, val_lbl in self.monitor_controls:
                try:
                    bright = sbc.get_brightness(display=mon)[0]
                    slider.blockSignals(True)
                    slider.setValue(bright)
                    val_lbl.setText(f"%{bright}")
                    slider.blockSignals(False)
                except Exception as e:
                    logging.warning(f"Pencere açılırken '{mon}' güncellenemedi: {e}")

        if self.current_mode == "auto":
            auto_target = self.calculate_auto_brightness()
            if self.target_value != auto_target:
                self.apply_preset(auto_target)
                
        self.update_tooltip()

    def on_slider_changed(self, monitor, value, val_label):
        val_label.setText(f"%{value}")
        self.update_tooltip()
        self.hardware_worker.add_task(monitor, value)
        if self.on_update_callback:
            self.on_update_callback()

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

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        
    def cleanup_overlays(self):
        for overlay in self.night_light_overlays:
            overlay.close()
        self.night_light_overlays.clear()


class SystemTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        self.server_key = "ParlaklikKontrolApp_SingleInstance_Key"
        self.check_single_instance()

        self.window = BrightnessWindow()
        self.app_name = "ParlaklikKontrol_App"
        
        self.tray = QSystemTrayIcon()
        self.tray.setVisible(True)
        
        self.window.tray_icon = self.tray
        self.window.update_tooltip()

        # Animasyon durumu
        self._rotation = 0.0
        self._last_brightness = -1
        self._last_rotation = -1.0
        self._last_mode = None

        self._anim_timer = QTimer()
        self._anim_timer.setInterval(50)  # 20 FPS
        self._anim_timer.timeout.connect(self._tick_icon)
        
        self.window.on_update_callback = self.check_timer_state
        self.check_timer_state()

        self.tray.activated.connect(self.on_tray_click)

        self.menu = QMenu()
        
        self.refresh_action = QAction("Monitörleri Yenile", self.menu)
        self.refresh_action.triggered.connect(self.window.refresh_monitors)
        self.menu.addAction(self.refresh_action)
        
        self.menu.addSeparator()

        self.startup_action = QAction("Windows ile Başlat", self.menu, checkable=True)
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
        screen = QApplication.screenAt(QCursor.pos())
        if not screen:
            screen = QApplication.primaryScreen()
        
        geo = screen.availableGeometry()
        x = geo.right() - self.window.width() - 10
        y = geo.bottom() - self.window.height() - 10
        
        self.window.move(x, y)
        self.window.show()
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
            registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(registry_key, self.app_name)
            winreg.CloseKey(registry_key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def toggle_startup(self, state):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
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
            winreg.CloseKey(registry_key)
        except Exception as e:
            logging.error(f"Başlangıç ayarı değiştirilirken hata oluştu: {e}")

    def check_timer_state(self):
        if self.window.current_mode == "auto":
            if not self._anim_timer.isActive():
                self._anim_timer.start()
        else:
            if self._anim_timer.isActive():
                self._anim_timer.stop()
        self._tick_icon()

    def _tick_icon(self):
        mode = self.window.current_mode

        # Güneş modunda yavaş döner (60 sn = 1 tam tur → 50ms'de 0.3°)
        if mode == "auto":
            self._rotation = (self._rotation + 0.3) % 360.0
        # Diğer modlarda donup kalır

        # Mevcut ortalama parlaklığı al
        if self.window.monitor_controls:
            avg_brightness = sum(s.value() for _, s, _ in self.window.monitor_controls) / len(self.window.monitor_controls)
        else:
            avg_brightness = 50

        if mode == self._last_mode and avg_brightness == self._last_brightness and self._rotation == self._last_rotation:
            return
            
        self._last_mode = mode
        self._last_brightness = avg_brightness
        self._last_rotation = self._rotation

        self.tray.setIcon(self.create_icon(avg_brightness, self._rotation, mode))

    def create_icon(self, brightness=75, rotation=0.0, mode="auto"):
        from PyQt5.QtCore import QPointF
        from PyQt5.QtGui import QPen
        import math as _math

        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # Parlaklık 0-100 → renk: koyu gri (#555) → altın (#FFB300)
        t = max(0.0, min(1.0, brightness / 100.0))
        r = int(85  + (255 - 85)  * t)
        g = int(85  + (179 - 85)  * t)
        b = int(85  + (0   - 85)  * t)
        sun_color = QColor(r, g, b)

        # Gece modunda çok soluk göster
        if mode == "gece":
            sun_color = QColor(60, 60, 60)

        pen = QPen(sun_color)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        cx, cy = 16.0, 16.0
        # Daire boyutu da parlaklıkla hafifçe büyür (4.5 - 6.5)
        r_circle = 4.5 + 2.0 * t
        painter.drawEllipse(QPointF(cx, cy), r_circle, r_circle)

        # Işın uzunluğu da parlaklıkla uzar (iç: 7-9.5, dış: 10.5-14)
        ray_inner = 7.0 + 2.5 * t
        ray_outer = 10.5 + 3.5 * t
        for i in range(8):
            angle = _math.radians(rotation + i * 45)
            x1 = cx + ray_inner * _math.cos(angle)
            y1 = cy + ray_inner * _math.sin(angle)
            x2 = cx + ray_outer * _math.cos(angle)
            y2 = cy + ray_outer * _math.sin(angle)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        painter.end()
        return QIcon(pixmap)

    def quit_app(self):
        if hasattr(self, 'local_server'):
            self.local_server.close()
            QLocalServer.removeServer(self.server_key)
        self.window.cleanup_overlays()
        self.window.hardware_worker.stop()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec_())

if __name__ == "__main__":
    tray_app = SystemTrayApp()
    tray_app.run()