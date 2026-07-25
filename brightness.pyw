import sys
import os
import winreg
import logging
import math
import queue
from datetime import datetime
import screen_brightness_control as sbc
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QSlider, QLabel, QPushButton, QSystemTrayIcon, QMenu, QAction)
from PyQt5.QtCore import Qt, QTimer, QThread, QObject, QEvent
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QCursor

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
                if task:
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


class TrayWheelFilter(QObject):
    def __init__(self, window):
        super().__init__()
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            delta = event.angleDelta().y()
            if delta > 0:
                self.window.adjust_all_brightness(+5)
            elif delta < 0:
                self.window.adjust_all_brightness(-5)
            return True
        return super().eventFilter(obj, event)


class BrightnessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setFixedSize(340, 260)
        
        self.tray_icon = None
        
        self.hardware_worker = HardwareWorker()
        self.hardware_worker.start()

        self.setStyleSheet("""
            QWidget {
                background-color: #202020;
                color: white;
                border-radius: 10px;
                font-family: 'Segoe UI', sans-serif;
            }
            QSlider::groove:horizontal {
                border-radius: 4px;
                height: 8px;
                background: #3a3a3a;
            }
            QSlider::handle:horizontal {
                background: #0078D7;
                width: 16px;
                height: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #1e90ff;
            }
            QSlider::disabled {
                opacity: 0.5;
            }
            QPushButton {
                background-color: #333333;
                border-radius: 6px;
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
        
        self.target_value = 0
        self.fade_timer = QTimer()
        self.fade_timer.setInterval(40)
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

        preset_layout.addWidget(self.auto_btn)
        preset_layout.addWidget(self.gece_btn)
        preset_layout.addWidget(self.gunduz_btn)
        self.main_layout.addLayout(preset_layout)

        self.main_layout.addSpacing(10)

        self.monitors_layout = QVBoxLayout()
        self.main_layout.addLayout(self.monitors_layout)
        self.setLayout(self.main_layout)

        self.refresh_monitors()

    def adjust_all_brightness(self, delta):
        if self.current_mode != "custom":
            self.current_mode = "custom"
            self.update_button_styles()
        for mon, slider, val_lbl in self.monitor_controls:
            new_val = max(0, min(100, slider.value() + delta))
            slider.setValue(new_val)

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
        active_style = "background-color: #0078D7; border-color: #005a9e; font-weight: bold; color: white;"
        default_style = "background-color: #333333; border-color: #3d3d3d; font-weight: 500; color: white;"

        self.auto_btn.setStyleSheet(active_style if self.current_mode == "auto" else default_style)
        self.gece_btn.setStyleSheet(active_style if self.current_mode == "gece" else default_style)
        self.gunduz_btn.setStyleSheet(active_style if self.current_mode == "gunduz" else default_style)
        self.update_tooltip()

    def calculate_auto_brightness(self):
        return get_offline_solar_brightness(lat=39.6484, lon=27.8826, tz_offset=3)

    def apply_auto_mode(self):
        self.current_mode = "auto"
        self.update_button_styles()
        target = self.calculate_auto_brightness()
        logging.info(f"Güneş Modu Aktif -> Hedef Parlaklık: %{target}")
        self.apply_preset(target)

    def on_preset_click(self, mode, target_val):
        self.current_mode = mode
        self.update_button_styles()
        self.apply_preset(target_val)

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

        for mon in monitors:
            try:
                current_bright = sbc.get_brightness(display=mon)[0]
            except Exception as e:
                logging.warning(f"'{mon}' parlaklığı okunamadı, varsayılan %50 atandı: {e}")
                current_bright = 50

            header_layout = QHBoxLayout()
            lbl = QLabel(mon)
            lbl.setStyleSheet("color: #a0a0a0; font-size: 12px; font-weight: 500;")
            
            val_lbl = QLabel(f"%{current_bright}")
            val_lbl.setStyleSheet("color: #0078D7; font-size: 12px; font-weight: bold;")
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
            
            self.monitors_layout.addLayout(header_layout)
            self.monitors_layout.addWidget(slider)
            self.monitors_layout.addSpacing(5)

        self.update_tooltip()

    def on_manual_slider_interaction(self):
        if self.current_mode != "custom":
            self.current_mode = "custom"
            self.update_button_styles()

    def showEvent(self, event):
        super().showEvent(event)
        if self.current_mode == "auto":
            auto_target = self.calculate_auto_brightness()
            if self.target_value != auto_target:
                self.apply_preset(auto_target)

        for mon, slider, val_lbl in self.monitor_controls:
            try:
                bright = sbc.get_brightness(display=mon)[0]
                slider.blockSignals(True)
                slider.setValue(bright)
                val_lbl.setText(f"%{bright}")
                slider.blockSignals(False)
            except Exception as e:
                logging.warning(f"Pencere açılırken '{mon}' güncellenemedi: {e}")
                
        self.update_tooltip()

    def on_slider_changed(self, monitor, value, val_label):
        val_label.setText(f"%{value}")
        self.update_tooltip()
        self.hardware_worker.add_task(monitor, value)

    def apply_preset(self, target_value):
        self.target_value = target_value
        self.fade_timer.start(40)

    def fade_step(self):
        all_done = True
        step_size = 5
        
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
        self.hardware_worker.stop()
        super().closeEvent(event)


class SystemTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        self.server_key = "ParlaklikKontrolApp_SingleInstance_Key"
        self.check_single_instance()

        self.window = BrightnessWindow()
        self.app_name = "ParlaklikKontrol_App"
        
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.create_icon())
        self.tray.setVisible(True)
        
        self.window.tray_icon = self.tray
        self.window.update_tooltip()
        
        self.wheel_filter = TrayWheelFilter(self.window)
        self.app.installEventFilter(self.wheel_filter)

        self.tray.activated.connect(self.on_tray_click)

        self.menu = QMenu()
        
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
        except WindowsError:
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

    def create_icon(self):
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        painter.setBrush(QColor("#0078D7"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(4, 4, 24, 24)
        
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(11, 11, 10, 10)
        
        painter.end()
        return QIcon(pixmap)

    def quit_app(self):
        if hasattr(self, 'local_server'):
            self.local_server.close()
            QLocalServer.removeServer(self.server_key)
        self.window.hardware_worker.stop()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec_())

if __name__ == "__main__":
    tray_app = SystemTrayApp()
    tray_app.run()