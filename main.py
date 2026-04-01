import os
import csv
import json
import threading
import time
import hashlib
import subprocess
import smtplib
import logging
import sys
from datetime import datetime
from email.message import EmailMessage
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import socket
import customtkinter as ctk

os.environ.setdefault("MPLBACKEND", "TkAgg")

import database
import modbus_client
import config_manager
import pystray
from PIL import Image
from pystray import MenuItem as item

# Logging (#9)
config_manager.setup_logging()
logger = logging.getLogger("scada.app")

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def safe_remove_file(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning(f"GeÃ§ici dosya silinemedi ({path}): {exc}")

# CTk ayarları
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

_instance_socket = None

def is_already_running(port=65432):
    """Check if another instance is already running using a socket."""
    global _instance_socket
    try:
        _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_socket.bind(("127.0.0.1", port))
        return False
    except socket.error:
        return True


class LoginWindow(ctk.CTk):
    """Giriş ekranı (#17)."""
    def __init__(self):
        super().__init__()
        self.title("Modvera - Giriş")
        self.geometry("400x320")
        self.resizable(False, False)
        self.user_info = None

        frame = ctk.CTkFrame(self, corner_radius=15)
        frame.pack(expand=True, fill="both", padx=30, pady=30)

        ctk.CTkLabel(frame, text="🔐 Modvera", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(20, 5))
        ctk.CTkLabel(frame, text="Giriş Yapın", font=ctk.CTkFont(size=14), text_color="gray").pack(pady=(0, 15))

        self.user_var = tk.StringVar(value="admin")
        ctk.CTkEntry(frame, textvariable=self.user_var, placeholder_text="Kullanıcı Adı", width=250).pack(pady=5)

        self.pass_var = tk.StringVar()
        self.pass_entry = ctk.CTkEntry(frame, textvariable=self.pass_var, placeholder_text="Şifre", show="*", width=250)
        self.pass_entry.pack(pady=5)
        self.pass_entry.bind("<Return>", lambda e: self.try_login())

        self.err_lbl = ctk.CTkLabel(frame, text="", text_color="#ef4444", font=ctk.CTkFont(size=12))
        self.err_lbl.pack(pady=2)

        ctk.CTkButton(frame, text="Giriş Yap", width=250, height=38, fg_color="#3b82f6",
                      hover_color="#2563eb", command=self.try_login).pack(pady=(5, 15))

    def try_login(self):
        user = self.user_var.get().strip()
        pw = self.pass_var.get()
        if not user or not pw:
            self.err_lbl.configure(text="Kullanıcı adı ve şifre gereklidir.")
            return
        result = database.authenticate_user(user, pw)
        if result:
            self.user_info = {"username": result[0], "role": result[1]}
            logger.info(f"Giriş başarılı: {result[0]} ({result[1]})")
            self.destroy()
        else:
            self.err_lbl.configure(text="Hatalı kullanıcı adı veya şifre!")
            logger.warning(f"Başarısız giriş denemesi: {user}")


class ModbusLoggerApp(ctk.CTk):
    def __init__(self, user_info=None):
        super().__init__()
        self.title("Modvera - Veri İzleme ve Kontrol Merkezi")
        self.geometry("1280x720")
        self.user_info = user_info or {"username": "admin", "role": "admin"}

        # Grid configuration: [Left Panel] [Sash] [Tabs Area]
        self.grid_columnconfigure(0, weight=0) # Left panel
        self.grid_columnconfigure(1, weight=0) # Sash
        self.grid_columnconfigure(2, weight=1) # Tabs Area
        self.grid_rowconfigure(1, weight=1)

        self.is_logging = threading.Event()  # Thread-safe (#4)
        self.log_thread = None
        self.modbus_conn = None  # Persistent connection (#2)
        self.service_process = None
        self.service_mode = None

        # Sayfalama (#8)
        self.page_offset = 0
        self.page_limit = 500
        self.total_records = 0
        
        # Sayfalama (Alarm Geçmişi)
        self.alarm_page_offset = 0
        self.alarm_page_limit = 500
        self.alarm_total_records = 0

        # Alarm listesi (#11)
        self.active_alarms = []
        
        # UI Durum Takibi (Titremeyi önlemek için)
        self._last_sys_status = None
        self._last_conn_status = None

        # Modbus Değişkenleri (Initialization before apply_config)
        self.ip_var = tk.StringVar(value="188.38.164.83")
        self.port_var = tk.IntVar(value=502)
        self.slave_var = tk.IntVar(value=1)
        self.func_var = ctk.StringVar(value="1 (Read Coils)")
        self.start_addr_var = tk.IntVar(value=0)
        self.count_var = tk.IntVar(value=1)
        self.interval_var = tk.StringVar(value="1")
        self.live_rate_var = tk.StringVar(value="1.0")
        
        # Canlı İzleme Sayfalama
        self.live_page_offset = 0
        self.live_page_limit = 50 # Her sayfada 50 tag
        self.live_data_buffer = [] # Tüm canlı verileri tutar

        database.init_db()
        self.aliases = {}
        self.config = config_manager.load_config()
        self.devices = config_manager.load_devices()
        self.active_profile_code = config_manager.normalize_func_code(self.config.get("func_code"))

        # Treeview Style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#1f2937", foreground="white",
                        fieldbackground="#1f2937", rowheight=25, borderwidth=0)
        style.map("Treeview", background=[('selected', '#3b82f6')])
        style.configure("Treeview.Heading", background="#111827", foreground="white",
                        relief="flat", font=("Arial", 10, "bold"))
        style.map("Treeview.Heading", background=[('active', '#374151')])

        self.create_widgets()
        self.apply_config()
        self.navigate("Log & Raporlar")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.tray_icon = None
        self.setup_tray()
        
        # Otomatik başlat (PC açıldığında arka plan servisi aktif olduğu için UI'da da direkt başlasın)
        self.after(500, self.start_logging)

    def on_sash_drag(self, event):
        """Handle sash dragging to resize left panel"""
        new_width = event.x_root - self.winfo_rootx()
        # Constrain between 200px and 80% of window width
        max_w = int(self.winfo_width() * 0.8)
        if 200 < new_width < max_w:
            self.grid_columnconfigure(0, minsize=new_width)
            self.left_panel.configure(width=new_width)
            self.update_idletasks() # Force UI refresh during drag

    def navigate(self, name):
        self.tabview.set(name)

    # === TRAY (SİSTEM TEPSİSİ) DESTEĞİ ===

    def setup_tray(self):
        """Sistem tepsisi ikonunu hazırlar."""
        try:
            icon_path = resource_path("icon.png")
            image = Image.open(icon_path)
            menu = (item('Göster', self.show_window), item('Çıkış', self.quit_app))
            self.tray_icon = pystray.Icon("modvera", image, "Modvera Veri İzleme", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            logger.error(f"Tray ikon hatası: {e}")

    def show_window(self):
        """Pencereyi tepsiden geri getirir."""
        if self.tray_icon:
            self.after(0, self.deiconify)
            self.after(0, self.focus_force)

    def hide_window(self):
        """Pencereyi gizler."""
        self.withdraw()
        if self.tray_icon:
            self.tray_icon.notify("Uygulama arka planda çalışmaya devam ediyor.", "Modvera")

    def quit_app(self):
        """Uygulamadan tamamen çıkar."""
        self.save_current_config()
        self.is_logging.clear()
        if self.modbus_conn:
            self.modbus_conn.disconnect()
        database.close_db()
        if self.tray_icon:
            self.tray_icon.stop()
        self.quit()
        os._exit(0)

    def on_closing(self):
        """Kapatma tuşuna basıldığında tepsiye küçült."""
        self.hide_window()

    @staticmethod
    def _coerce_int(value, default, minimum=None):
        try:
            parsed = int(value)
        except (TypeError, ValueError, tk.TclError):
            parsed = default
        if minimum is not None:
            parsed = max(minimum, parsed)
        return parsed

    @staticmethod
    def _coerce_float_string(value, default, minimum=None):
        try:
            parsed = float(value)
        except (TypeError, ValueError, tk.TclError):
            parsed = float(default)
        if minimum is not None:
            parsed = max(minimum, parsed)
        return f"{parsed:g}"

    def get_live_rate_seconds(self):
        return float(self._coerce_float_string(self.live_rate_var.get(), 1.0, minimum=0.2))

    def _load_profile_fields(self, func_code):
        profile = config_manager.get_polling_profile(self.config, func_code)
        if profile:
            self.slave_var.set(profile.get("slave_id", self.slave_var.get()))
            self.start_addr_var.set(profile.get("start_addr", self.start_addr_var.get()))
            self.count_var.set(profile.get("count", self.count_var.get()))

    def save_current_config(self, profile_code=None, active_func_choice=None):
        active_label = config_manager.get_function_label(active_func_choice or self.func_var.get())
        target_profile_code = config_manager.normalize_func_code(profile_code or active_label)
        config_dict = {
            **self.config,
            "ip": self.ip_var.get().strip() or "188.38.164.83",
            "port": self._coerce_int(self.port_var.get(), 502, minimum=1),
            "slave_id": self._coerce_int(self.slave_var.get(), 1, minimum=1),
            "func_code": active_label,
            "start_addr": self._coerce_int(self.start_addr_var.get(), 0, minimum=0),
            "count": self._coerce_int(self.count_var.get(), 1, minimum=1),
            "log_interval": self._coerce_float_string(self.interval_var.get(), 1.0, minimum=0.1),
            "live_rate": self._coerce_float_string(self.live_rate_var.get(), 1.0, minimum=0.2)
        }
        config_dict = config_manager.upsert_polling_profile(
            config_dict,
            target_profile_code,
            slave_id=config_dict["slave_id"],
            start_addr=config_dict["start_addr"],
            count=config_dict["count"],
            enabled=True,
        )
        self.config = config_manager.normalize_config(config_dict)
        self.ip_var.set(config_dict["ip"])
        self.port_var.set(config_dict["port"])
        self.slave_var.set(config_dict["slave_id"])
        self.start_addr_var.set(config_dict["start_addr"])
        self.count_var.set(config_dict["count"])
        self.interval_var.set(config_dict["log_interval"])
        self.live_rate_var.set(config_dict["live_rate"])
        config_manager.save_config(self.config)

    def apply_config(self):
        self.config = config_manager.normalize_config(self.config)
        c = self.config
        self.ip_var.set(c.get("ip", "188.38.164.83"))
        self.port_var.set(c.get("port", 502))
        self.func_var.set(c.get("func_code", "1 (Read Coils)"))
        self.interval_var.set(c.get("log_interval", "1"))
        self.live_rate_var.set(c.get("live_rate", "1.0"))
        self.active_profile_code = config_manager.normalize_func_code(self.func_var.get())
        self._load_profile_fields(self.active_profile_code)
        self.load_aliases(self.active_profile_code)

    def create_widgets(self):
        # === ÜST BAR (Status & Login Info) ===
        top_bar = ctk.CTkFrame(self, height=45, corner_radius=0, fg_color="#111827")
        top_bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        top_bar.grid_columnconfigure(2, weight=1)
        top_bar.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(top_bar, text="Sistem:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(15, 5), pady=10)
        self.sys_status_led = ctk.CTkLabel(top_bar, text="DURDU", text_color="white", fg_color="#ef4444", corner_radius=5, width=70)
        self.sys_status_led.grid(row=0, column=1, padx=5, pady=10)

        # Saat ve Alarm alanı (ortada)
        self.center_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
        self.center_frame.grid(row=0, column=2, columnspan=4, sticky="")

        self.clock_lbl = ctk.CTkLabel(self.center_frame, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#3b82f6")
        self.clock_lbl.pack()

        self.alarm_banner = ctk.CTkLabel(self.center_frame, text="", font=ctk.CTkFont(size=13, weight="bold"),
                                         text_color="white", fg_color="#dc2626", corner_radius=8, height=28)
        self.alarm_banner.pack_forget()
        self._alarm_flash_id = None
        self._alarm_reset_id = None
        self.update_clock()

        # Kullanıcı bilgisi
        user_text = f"👤 {self.user_info['username']} ({self.user_info['role']})"
        ctk.CTkLabel(top_bar, text=user_text, font=ctk.CTkFont(size=11)).grid(row=0, column=6, padx=10, pady=10)

        ctk.CTkLabel(top_bar, text="Log Yolu:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=7, padx=(10, 5), pady=10)
        ctk.CTkButton(top_bar, text="📁 Gözat", width=80, height=24, fg_color="#4b5563",
                      hover_color="#374151", command=self.open_log_folder).grid(row=0, column=8, padx=(0, 15), pady=10)

        self.conn_alarm_led = ctk.CTkLabel(top_bar, text="BEKLİYOR", text_color="white", fg_color="#6b7280", corner_radius=5, width=90)
        self.conn_alarm_led.grid(row=0, column=9, padx=(0, 20), pady=10)

        # === SOL PANEL: AYARLAR VE KONTROLLER (#14) ===
        init_w = 600 # Starting with a more balanced width after user's manual change
        self.left_panel = ctk.CTkFrame(self, width=init_w, corner_radius=0)
        self.grid_columnconfigure(0, minsize=init_w)
        self.left_panel.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.left_panel.grid_propagate(False) # Keep width fixed while dragging

        # === SASH: RESIZER ===
        self.sash = ctk.CTkFrame(self, width=6, corner_radius=0, fg_color="#1e293b", cursor="sb_h_double_arrow")
        self.sash.grid(row=1, column=1, sticky="ns")
        self.sash.bind("<B1-Motion>", self.on_sash_drag)
        self.sash.bind("<Enter>", lambda e: self.sash.configure(fg_color="#3b82f6"))
        self.sash.bind("<Leave>", lambda e: self.sash.configure(fg_color="#1e293b"))
        
        # Scrollable container for settings
        scroll_settings = ctk.CTkScrollableFrame(self.left_panel, label_text="Bağlantı Ayarları", fg_color="transparent")
        scroll_settings.pack(expand=True, fill="both", padx=10, pady=10)

        # Ayar alanları
        row = 0
        ctk.CTkLabel(scroll_settings, text="IP Adresi:").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.ip_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Port:").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.port_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Slave ID:").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.slave_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Fonksiyon:").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkComboBox(scroll_settings, variable=self.func_var, values=[
            "1 (Read Coils)", "2 (Read Discrete Inputs)",
            "3 (Read Holding Registers)", "4 (Read Input Registers)"
        ], command=self.on_func_change).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Baslangıç:").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.start_addr_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Adet (Count):").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.count_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        ctk.CTkLabel(scroll_settings, text="Log Aralığı (dk):").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.interval_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1
        ctk.CTkLabel(scroll_settings, text="Canlı İzleme (sn):").grid(row=row, column=0, padx=5, pady=5, sticky="w")
        ctk.CTkEntry(scroll_settings, textvariable=self.live_rate_var).grid(row=row, column=1, padx=5, pady=5)
        row += 1

        # Operasyonel Butonlar
        btn_frame = ctk.CTkFrame(scroll_settings, fg_color="transparent")
        btn_frame.grid(row=row, column=0, columnspan=2, pady=20)
        
        self.start_btn = ctk.CTkButton(btn_frame, text="🚀 BAŞLAT", width=120, height=35, fg_color="#2da44e", 
                                      hover_color="#238636", command=self.start_logging)
        self.start_btn.pack(side="left", padx=5)
        
        self.stop_btn = ctk.CTkButton(btn_frame, text="🛑 DURDUR", width=120, height=35, fg_color="#cf222e", 
                                     hover_color="#a41e27", state="disabled", command=self.stop_logging)
        self.stop_btn.pack(side="left", padx=5)
        row += 1

        # Ek İşlemler
        row += 1
        ctk.CTkButton(scroll_settings, text="📈 Trend Grafik", width=250, fg_color="#3b82f6",
                      command=self.open_trend_graph).grid(row=row, column=0, columnspan=2, pady=5)
        row += 1
        ctk.CTkButton(scroll_settings, text="🔔 Alarm Yöneticisi", width=250, fg_color="#d97706",
                      command=self.open_alarm_manager).grid(row=row, column=0, columnspan=2, pady=5)
        row += 1
        ctk.CTkButton(scroll_settings, text="🏷 Adres İsimlendirme", width=250, fg_color="#4b5563",
                      command=self.open_alias_manager).grid(row=row, column=0, columnspan=2, pady=5)
        row += 1
        ctk.CTkButton(scroll_settings, text="Kalici Izleme Profilleri", width=250, fg_color="#0f766e",
                      hover_color="#115e59", command=self.open_polling_profile_manager).grid(row=row, column=0, columnspan=2, pady=5)
        row += 1
        ctk.CTkButton(scroll_settings, text="🗄 Veritabanı Yönetimi", width=250, fg_color="#6366f1",
                      command=self.open_db_manager).grid(row=row, column=0, columnspan=2, pady=5)
        row += 1

        self.status_lbl = ctk.CTkLabel(scroll_settings, text="Durum: Hazır", text_color="gray")
        self.status_lbl.grid(row=row, column=0, columnspan=2, pady=10)
        row += 1

        # Canlı İzleme (Ham Veri)
        ctk.CTkLabel(scroll_settings, text="Canlı İzleme (Ham Veri):", font=ctk.CTkFont(weight="bold")).grid(row=row, column=0, columnspan=2, pady=(10, 0))
        row += 1
        self.live_tree = ttk.Treeview(scroll_settings, columns=("Addr", "Val"), show="headings", height=5)
        self.live_tree.heading("Addr", text="Adres")
        self.live_tree.heading("Val", text="Değer")
        self.live_tree.column("Addr", width=120)
        self.live_tree.column("Val", width=120)
        self.live_tree.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        row += 1

        # Canlı İzleme Sayfalama Butonları
        live_page_frame = ctk.CTkFrame(scroll_settings, fg_color="transparent")
        live_page_frame.grid(row=row, column=0, columnspan=2, pady=5)
        
        ctk.CTkButton(live_page_frame, text="◀", width=50, command=self.live_prev_page).pack(side="left", padx=5)
        self.live_page_lbl = ctk.CTkLabel(live_page_frame, text="Sayfa: 1/1")
        self.live_page_lbl.pack(side="left", padx=10)
        ctk.CTkButton(live_page_frame, text="▶", width=50, command=self.live_next_page).pack(side="left", padx=5)
        row += 1

        # === SAĞ PANEL: TABVIEW (#15) ===
        self.tabview = ctk.CTkTabview(self, corner_radius=10)
        self.tabview.grid(row=1, column=2, sticky="nsew", padx=10, pady=10)

        # Sekmeleri oluştur
        self.tab_reports = self.tabview.add("Log & Raporlar")
        self.tab_alarm_history = self.tabview.add("Alarm Geçmişi")

        # Initialize Tabs
        self.setup_reports_tab()
        self.setup_alarm_history_tab()

        # Navigation callback
        self.tabview.configure(command=self._on_tab_change)

    def _on_tab_change(self):
        pass


    def setup_alarm_history_tab(self):
        self.tab_alarm_history.grid_rowconfigure(1, weight=1)
        self.tab_alarm_history.grid_columnconfigure(0, weight=1)

        alarm_top_frame = ctk.CTkFrame(self.tab_alarm_history, fg_color="transparent")
        alarm_top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        ctk.CTkLabel(alarm_top_frame, text="Başlangıç:").pack(side="left", padx=5)
        self.alarm_start_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 00:00"))
        ctk.CTkEntry(alarm_top_frame, textvariable=self.alarm_start_date_var, width=140).pack(side="left", padx=5)

        ctk.CTkLabel(alarm_top_frame, text="Bitiş:").pack(side="left", padx=5)
        self.alarm_end_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 23:59"))
        ctk.CTkEntry(alarm_top_frame, textvariable=self.alarm_end_date_var, width=140).pack(side="left", padx=5)

        ctk.CTkButton(alarm_top_frame, text="Kayıtları Getir", width=120, 
                      command=self.refresh_alarm_history).pack(side="left", padx=(15, 5))
        ctk.CTkButton(alarm_top_frame, text="CSV'ye Aktar", width=110, fg_color="#d97706",
                      hover_color="#b45309", command=self.export_alarm_csv).pack(side="left", padx=5)

        ctk.CTkLabel(alarm_top_frame, text="Alıcı E-posta:").pack(side="left", padx=(15, 5))
        self.alarm_email_var = tk.StringVar(value="")
        ctk.CTkEntry(alarm_top_frame, textvariable=self.alarm_email_var, width=150, placeholder_text="mail@a.com").pack(side="left", padx=5)
        ctk.CTkButton(alarm_top_frame, text="E-posta Gönder", width=110, fg_color="#6366f1",
                      hover_color="#4f46e5", command=self.send_alarm_email).pack(side="left", padx=5)

        self.alarm_tree = ttk.Treeview(self.tab_alarm_history, 
                                       columns=("Time", "Addr", "Func", "Val", "Min", "Max", "Msg"), 
                                       show="headings")
        self.alarm_tree.heading("Time", text="Zaman")
        self.alarm_tree.heading("Addr", text="Adres")
        self.alarm_tree.heading("Func", text="Fonks")
        self.alarm_tree.heading("Val", text="Değer")
        self.alarm_tree.heading("Min", text="Min")
        self.alarm_tree.heading("Max", text="Max")
        self.alarm_tree.heading("Msg", text="Mesaj")

        for col in ("Time", "Addr", "Func", "Val", "Min", "Max"):
            self.alarm_tree.column(col, width=80, anchor=tk.CENTER)
        self.alarm_tree.column("Msg", width=250, anchor="w")
        self.alarm_tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))

        # Alarm Scroll & Buttons
        alarm_side_nav = ctk.CTkFrame(self.tab_alarm_history, fg_color="transparent")
        alarm_side_nav.grid(row=1, column=1, sticky="ns", padx=(5, 10), pady=(0, 10))

        ctk.CTkButton(alarm_side_nav, text="▲", width=35, height=40, font=("Arial", 14, "bold"),
                      fg_color="#374151", hover_color="#4b5563", 
                      command=lambda: self.scroll_alarm_treeview(-1)).pack(pady=2)
        
        alarm_scroll = ctk.CTkScrollbar(alarm_side_nav, orientation="vertical", command=self.alarm_tree.yview, width=20)
        alarm_scroll.pack(fill="y", expand=True, pady=5)
        self.alarm_tree.configure(yscrollcommand=alarm_scroll.set)

        ctk.CTkButton(alarm_side_nav, text="▼", width=35, height=40, font=("Arial", 14, "bold"),
                      fg_color="#374151", hover_color="#4b5563", 
                      command=lambda: self.scroll_alarm_treeview(1)).pack(pady=2)

        # Alarm Sayfalama (Eklendi)
        alarm_page_frame = ctk.CTkFrame(self.tab_alarm_history, fg_color="transparent", height=30)
        alarm_page_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 5))

        self.alarm_page_lbl = ctk.CTkLabel(alarm_page_frame, text="", font=ctk.CTkFont(size=11))
        self.alarm_page_lbl.pack(side="left", padx=10)

        ctk.CTkButton(alarm_page_frame, text="Sonraki ▶", width=80, height=28, fg_color="#374151",
                      command=self.alarm_next_page).pack(side="right", padx=5)
        ctk.CTkButton(alarm_page_frame, text="◀ Önceki", width=80, height=28, fg_color="#374151",
                      command=self.alarm_prev_page).pack(side="right", padx=5)
        ctk.CTkButton(alarm_page_frame, text="Son ≫", width=60, height=28, fg_color="#374151",
                      command=self.alarm_last_page).pack(side="right", padx=5)
        ctk.CTkButton(alarm_page_frame, text="≪ İlk", width=60, height=28, fg_color="#374151",
                      command=self.alarm_first_page).pack(side="right", padx=5)


    def setup_reports_tab(self):
        self.tab_reports.grid_rowconfigure(1, weight=1)
        self.tab_reports.grid_columnconfigure(0, weight=1)
        top_query_frame = ctk.CTkFrame(self.tab_reports, fg_color="transparent")
        top_query_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        ctk.CTkLabel(top_query_frame, text="Başlangıç:").grid(row=0, column=0, padx=5, pady=5)
        self.start_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 00:00"))
        ctk.CTkEntry(top_query_frame, textvariable=self.start_date_var, width=140).grid(row=0, column=1, padx=5, pady=5)

        ctk.CTkLabel(top_query_frame, text="Bitiş:").grid(row=0, column=2, padx=5, pady=5)
        self.end_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 23:59"))
        ctk.CTkEntry(top_query_frame, textvariable=self.end_date_var, width=140).grid(row=0, column=3, padx=5, pady=5)

        ctk.CTkButton(top_query_frame, text="Verileri Getir", width=100, command=self.query_data).grid(row=0, column=4, padx=5, pady=5)
        ctk.CTkButton(top_query_frame, text="CSV'ye Aktar", width=100, fg_color="#d97706",
                      hover_color="#b45309", command=self.export_csv).grid(row=0, column=5, padx=5, pady=5)

        ctk.CTkLabel(top_query_frame, text="Alıcı E-posta:").grid(row=0, column=6, padx=(15, 5), pady=5)
        self.email_var = tk.StringVar(value="")
        ctk.CTkEntry(top_query_frame, textvariable=self.email_var, width=180, placeholder_text="mail@a.com").grid(row=0, column=7, padx=5, pady=5)
        ctk.CTkButton(top_query_frame, text="E-posta Gönder", width=100, fg_color="#6366f1",
                      hover_color="#4f46e5", command=self.send_email).grid(row=0, column=8, padx=5, pady=5)

        # Filtre & CSV Import
        ctk.CTkLabel(top_query_frame, text="Filtre:").grid(row=0, column=9, padx=(10, 5), pady=5)
        self.func_filter_var = ctk.StringVar(value="Hepsi")
        ctk.CTkComboBox(top_query_frame, variable=self.func_filter_var, values=[
            "Hepsi", "1 (Read Coils)", "2 (Read Discrete Inputs)",
            "3 (Read Holding Registers)", "4 (Read Input Registers)"
        ], width=160, command=lambda _: self.query_data()).grid(row=0, column=10, padx=5, pady=5)
        ctk.CTkLabel(top_query_frame, text="Tag:").grid(row=0, column=11, padx=(10, 5), pady=5)
        self.tag_filter_var = tk.StringVar(value="")
        ctk.CTkEntry(top_query_frame, textvariable=self.tag_filter_var, width=150, placeholder_text="BASINC / Addr: 5").grid(row=0, column=12, padx=5, pady=5)

        ctk.CTkButton(top_query_frame, text="📥 CSV İçe Aktar", width=110, fg_color="#10b981",
                      hover_color="#059669", command=self.import_csv).grid(row=0, column=13, padx=5, pady=5)

        # Sayfalama bilgisi ve butonları (#8)
        page_frame = ctk.CTkFrame(self.tab_reports, fg_color="transparent", height=30)
        page_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 5))

        self.page_lbl = ctk.CTkLabel(page_frame, text="", font=ctk.CTkFont(size=11))
        self.page_lbl.pack(side="left", padx=10)

        ctk.CTkButton(page_frame, text="Sonraki ▶", width=80, height=28, fg_color="#374151",
                      command=self.next_page).pack(side="right", padx=5)
        ctk.CTkButton(page_frame, text="◀ Önceki", width=80, height=28, fg_color="#374151",
                      command=self.prev_page).pack(side="right", padx=5)
        ctk.CTkButton(page_frame, text="Son ≫", width=60, height=28, fg_color="#374151",
                      command=self.last_page).pack(side="right", padx=5)
        ctk.CTkButton(page_frame, text="≪ İlk", width=60, height=28, fg_color="#374151",
                      command=self.first_page).pack(side="right", padx=5)

        # Treeview
        self.tree = ttk.Treeview(self.tab_reports, columns=("Timestamp", "Address", "Value", "Function"), show="headings")
        self.tree.heading("Timestamp", text="Tarih & Saat")
        self.tree.heading("Address", text="Adres / İsim")
        self.tree.heading("Value", text="Ölçülen Değer")
        self.tree.heading("Function", text="Tip (Fonksiyon)")
        self.tree.column("Timestamp", width=160, anchor=tk.CENTER)
        self.tree.column("Address", width=180, anchor=tk.W)
        self.tree.column("Value", width=100, anchor=tk.CENTER)
        self.tree.column("Function", width=160, anchor=tk.W)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 5))

        # Scrollbar
        side_nav = ctk.CTkFrame(self.tab_reports, fg_color="transparent")
        side_nav.grid(row=1, column=1, sticky="ns", padx=(5, 10), pady=(0, 5))

        ctk.CTkButton(side_nav, text="▲", width=35, height=40, font=("Arial", 14, "bold"),
                      fg_color="#374151", hover_color="#4b5563", command=lambda: self.scroll_treeview(-1)).pack(pady=2)
        scrollbar = ctk.CTkScrollbar(side_nav, command=self.tree.yview, width=20)
        scrollbar.pack(fill="y", expand=True, pady=5)
        self.tree.configure(yscrollcommand=scrollbar.set)
        ctk.CTkButton(side_nav, text="▼", width=35, height=40, font=("Arial", 14, "bold"),
                      fg_color="#374151", hover_color="#4b5563", command=lambda: self.scroll_treeview(1)).pack(pady=2)

    # === CORE METHODS ===

    def refresh_alarm_history(self):
        self.alarm_page_offset = 0
        self._fetch_alarm_page()

    def alarm_next_page(self):
        if self.alarm_page_offset + self.alarm_page_limit < self.alarm_total_records:
            self.alarm_page_offset += self.alarm_page_limit
            self._fetch_alarm_page()

    def alarm_prev_page(self):
        if self.alarm_page_offset > 0:
            self.alarm_page_offset = max(0, self.alarm_page_offset - self.alarm_page_limit)
            self._fetch_alarm_page()

    def alarm_first_page(self):
        self.alarm_page_offset = 0
        self._fetch_alarm_page()

    def alarm_last_page(self):
        if self.alarm_total_records > 0:
            self.alarm_page_offset = ((self.alarm_total_records - 1) // self.alarm_page_limit) * self.alarm_page_limit
            self._fetch_alarm_page()

    def _fetch_alarm_page(self):
        # Treeview temizle
        for item in self.alarm_tree.get_children():
            self.alarm_tree.delete(item)
        
        start_time = self.alarm_start_date_var.get().strip()
        end_time = self.alarm_end_date_var.get().strip()
        
        if len(start_time) == 16: start_time += ":00"
        if len(end_time) == 16: end_time += ":59"

        try:
            results, self.alarm_total_records = database.get_alarm_history(
                limit=self.alarm_page_limit, offset=self.alarm_page_offset,
                start_time=start_time, end_time=end_time
            )
            for row in results:
                self.alarm_tree.insert("", "end", values=row)

            page_num = (self.alarm_page_offset // self.alarm_page_limit) + 1
            total_pages = max(1, (self.alarm_total_records + self.alarm_page_limit - 1) // self.alarm_page_limit)
            self.alarm_page_lbl.configure(text=f"Sayfa {page_num}/{total_pages} — Toplam {self.alarm_total_records} alarm")
            
            if not results and self.alarm_page_offset == 0:
                self.log_message("Belirtilen aralıkta alarm kaydı bulunamadı.", "#f59e0b")
            else:
                self.log_message(f"Alarmlar getirildi: {self.alarm_total_records} kayıt.", "#10b981")

        except Exception as e:
            self.log_message(f"Alarm Sorgu Hatası: {str(e)}", "#ef4444")
            logger.error(f"Alarm sorgu hatası: {e}")

    def load_aliases(self, func_code=None):
        if func_code is None:
            func_code = self.func_var.get().split(" ")[0]
        self.aliases = database.get_all_aliases(func_code)

    def export_alarm_csv(self):
        records = [self.alarm_tree.item(row_id)['values'] for row_id in self.alarm_tree.get_children()]
        if not records:
            messagebox.showwarning("Boş Tablo", "Aktarılacak veri yok.")
            return
        file_path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV Dosyaları", "*.csv")], title="Alarm Geçmişini Kaydet",
            initialfile=f"alarm_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        if file_path:
            csv_file = None
            png_file = None
            try:
                with open(file_path, mode='w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["Zaman", "Adres", "Fonks", "Değer", "Min", "Max", "Mesaj"])
                    for record in records: writer.writerow(record)
                messagebox.showinfo("Başarılı", "Alarm kayıtları aktarıldı.")
            except Exception as e: messagebox.showerror("Hata", str(e))

    def scroll_alarm_treeview(self, amount):
        children = len(self.alarm_tree.get_children())
        step = max(0.005, 1.0 / max(1, children)) * abs(amount)
        current = self.alarm_tree.yview()[0]
        new_pos = current + (step if amount > 0 else -step)
        self.alarm_tree.yview_moveto(max(0, min(1, new_pos)))

    def on_func_change(self, choice):
        new_code = config_manager.normalize_func_code(choice)
        previous_code = self.active_profile_code
        if previous_code and previous_code != new_code:
            self.save_current_config(profile_code=previous_code, active_func_choice=previous_code)

        self.active_profile_code = new_code
        self._load_profile_fields(new_code)
        self.load_aliases(new_code)
        self.save_current_config(profile_code=new_code, active_func_choice=choice)
        self.log_message(f"Okuma tipi değişti: {choice}", "#3b82f6")

    def update_clock(self):
        self.clock_lbl.configure(text=datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
        self.after(1000, self.update_clock)

    def open_log_folder(self):
        folder_path = os.path.dirname(os.path.abspath(database.DB_NAME))
        if os.name == 'nt':
            os.startfile(folder_path)
        else:
            subprocess.run(['xdg-open', folder_path])

    def update_live_view_from_batch(self, updates):
        """Buffer'daki tüm verileri günceller ve mevcut sayfayı ekrana basar."""
        self.live_data_buffer = updates
        self.refresh_live_tree()

    def refresh_live_tree(self):
        """Sadece mevcut sayfadaki verileri Treeview'a basar veya günceller."""
        start = self.live_page_offset
        end = start + self.live_page_limit
        page_data = self.live_data_buffer[start:end]
        
        # Mevcut IID'leri al
        existing_items = self.live_tree.get_children()
        
        # Veri sayısı değişmişse veya sayfa değişmişse temizle (basitlik için)
        if len(existing_items) != len(page_data):
            for i in existing_items:
                self.live_tree.delete(i)
            existing_items = []

        for idx, (name, val) in enumerate(page_data):
            display_val = val
            if isinstance(val, bool):
                display_val = "ON" if val else "OFF"
            
            if idx < len(existing_items):
                # Mevcut öğeyi güncelle (Titremeyi önler)
                item_id = existing_items[idx]
                # Sadece değer değişmişse güncelleme yap
                if self.live_tree.item(item_id, "values") != [str(name), str(display_val)]:
                    self.live_tree.item(item_id, values=(name, display_val))
            else:
                # Yeni öğe ekle
                self.live_tree.insert("", "end", values=(name, display_val))
            
        total_pages = max(1, (len(self.live_data_buffer) + self.live_page_limit - 1) // self.live_page_limit)
        curr_page = (self.live_page_offset // self.live_page_limit) + 1
        
        # Etiketi güncelle (Thread-safe olduğundan emin olalım)
        page_text = f"Sayfa: {curr_page}/{total_pages}"
        if self.live_page_lbl.cget("text") != page_text:
            self.live_page_lbl.configure(text=page_text)

    def live_next_page(self):
        if self.live_page_offset + self.live_page_limit < len(self.live_data_buffer):
            self.live_page_offset += self.live_page_limit
            self.refresh_live_tree()

    def live_prev_page(self):
        if self.live_page_offset > 0:
            self.live_page_offset = max(0, self.live_page_offset - self.live_page_limit)
            self.refresh_live_tree()

    def set_top_status(self, is_running, alarm_status=None, has_alarm=False, watchdog_issues=None):
        # Sadece durum değiştiğinde güncelleme yap (Titremeyi önler)
        if is_running != self._last_sys_status:
            if is_running:
                self.sys_status_led.configure(text="ÇALIŞIYOR", fg_color="#10b981")
            else:
                self.sys_status_led.configure(text="DURDU", fg_color="#ef4444")
                self.conn_alarm_led.configure(text="BEKLİYOR", fg_color="#6b7280")
                self._last_conn_status = "BEKLİYOR"
            self._last_sys_status = is_running

        # "ALARM" yazısı sadece aktif alarm varken görünsün, yoksa bağlantı durumunu göster.
        if is_running:
            if has_alarm or watchdog_issues:
                if self._last_conn_status != "ALARM":
                    self.conn_alarm_led.configure(text="⚠ ALARM", fg_color="#ef4444")
                    self._last_conn_status = "ALARM"
                if watchdog_issues:
                    # Watchdog alarmı varsa status bara yaz
                    msg = watchdog_issues[0] if isinstance(watchdog_issues, list) else str(watchdog_issues)
                    self.log_message(msg, "#ef4444")
            else:
                if alarm_status and (alarm_status != self._last_conn_status or self._last_conn_status == "ALARM"):
                    if alarm_status == "OK":
                        self.conn_alarm_led.configure(text="BAĞLANTI OK", fg_color="#10b981")
                    elif alarm_status == "ERROR":
                        self.conn_alarm_led.configure(text="BAĞLANTI KOPTU", fg_color="#ef4444")
                    self._last_conn_status = alarm_status

    def log_message(self, msg, color="#e5e5e5"):
        self.after(0, lambda: self.status_lbl.configure(text=msg, text_color=color))

    # === LOGGING LOOP ===

    def live_view_loop(self):
        """Servisten gelen canlı verileri (JSON) okur ve UI'ı günceller."""
        logger.info("Live view thread started.")
        live_data_path = os.path.join(config_manager.PROJECT_ROOT, "live_data.json")
        health_path = os.path.join(config_manager.PROJECT_ROOT, "service_health.json")
        
        while self.is_logging.is_set():
            try:
                watchdog_issues = []
                system_running = self.is_service_running()

                # Önce sağlık durumunu oku
                if os.path.exists(health_path):
                    try:
                        with open(health_path, "r", encoding="utf-8") as f:
                            health_pack = json.load(f)
                            wd = health_pack.get("watchdog", {})
                            if wd.get("status") == "alarm":
                                watchdog_issues = wd.get("active_issues", ["Servis Watchdog Alarmı!"])
                            
                            sh = health_pack.get("startup_health", {})
                            if sh.get("status") == "alarm":
                                watchdog_issues.append("Servis Açılış Sağlık Alarmı!")
                    except (json.JSONDecodeError, OSError):
                        pass

                if os.path.exists(live_data_path):
                    try:
                        with open(live_data_path, "r", encoding="utf-8") as f:
                            data_pack = json.load(f)
                    except json.JSONDecodeError:
                        time.sleep(0.2)
                        continue
                    
                    last_update = data_pack.get("last_update", 0)
                    all_updates = data_pack.get("data", [])
                    
                    # Veri çok eskiyse uyar
                    if time.time() - last_update > 30:
                        self.after(0, lambda: self.log_message("Servis verisi gecikiyor (30sn+).", "#d97706"))
                        self.after(0, lambda: self.set_top_status(system_running, "ERROR", False, watchdog_issues))
                    else:
                        if all_updates:
                            # UI Güncelle
                            self.after(0, lambda u=all_updates: self.update_live_view_from_batch([(row[0], row[1]) for row in u]))
                            self.after(0, lambda: self.log_message(f"Canlı Veriler (Servisten) - {datetime.fromtimestamp(last_update).strftime('%H:%M:%S')}", "#10b981"))
                            self.after(0, lambda: self.set_top_status(system_running, "OK", False, watchdog_issues))
                else:
                    self.after(0, lambda: self.log_message("Servis bekleniyor (live_data.json yok)...", "#3b82f6"))
                    self.after(0, lambda: self.set_top_status(system_running, "BEKLİYOR", False, watchdog_issues))

            except Exception as e:
                logger.error(f"Live view loop error: {e}")
                self.after(0, lambda: self.log_message(f"Görünüm Hatası: {e}", "#ef4444"))

            time.sleep(self.get_live_rate_seconds())
            
    def is_service_running(self):
        """Arka plan servisinin çalışıp çalışmadığını kontrol eder."""
        try:
            if self.service_process and self.service_process.poll() is None:
                return True
            if os.name == 'nt':
                output = subprocess.check_output(
                    'tasklist /FI "IMAGENAME eq modvera_service.exe"',
                    shell=True
                ).decode(errors="ignore")
                if "modvera_service.exe" in output:
                    return True
                return bool(self.get_python_service_pids())
            result = subprocess.run(['pgrep', '-f', 'logger_service.py'], capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False

    def get_service_command(self):
        project_dir = config_manager.PROJECT_ROOT
        exe_path = os.path.join(project_dir, "modvera_service.exe")
        py_path = os.path.join(project_dir, "logger_service.py")

        if getattr(sys, 'frozen', False):
            if os.path.exists(exe_path):
                return "exe", [exe_path]
            if os.path.exists(py_path):
                return "python", [sys.executable, py_path]
        else:
            if os.path.exists(py_path):
                return "python", [sys.executable, py_path]
            if os.path.exists(exe_path):
                return "exe", [exe_path]
        return None, None

    def get_python_service_pids(self):
        if os.name == 'nt':
            script = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -like '*logger_service.py*' } | "
                "Select-Object -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return []
            return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]

        result = subprocess.run(['pgrep', '-f', 'logger_service.py'], capture_output=True, text=True)
        if result.returncode != 0:
            return []
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]

    def start_service(self):
        """Arka plan servisini başlatır."""
        try:
            if self.is_service_running():
                return True
            
            mode, command = self.get_service_command()
            if not command:
                logger.error("Service executable/script not found.")
                return False

            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.service_process = subprocess.Popen(command, creationflags=creation_flags)
            self.service_mode = mode

            if mode == "exe":
                logger.info("Servis EXE üzerinden başlatıldı.")
            else:
                logger.info("Servis Python üzerinden başlatıldı.")

            return True
        except Exception as e:
            logger.error(f"Servis başlatılamadı: {e}")
            return False

    def stop_service(self):
        """Arka plan servisini durdurur."""
        try:
            if self.service_process and self.service_process.poll() is None:
                self.service_process.terminate()
                try:
                    self.service_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.service_process.kill()
            self.service_process = None
            self.service_mode = None

            if os.name == 'nt':
                subprocess.run('taskkill /F /IM modvera_service.exe', shell=True, capture_output=True)
                for pid in self.get_python_service_pids():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        text=True
                    )
            else:
                subprocess.run(['pkill', '-f', 'logger_service.py'], capture_output=True)
            logger.info("Servis durdurma komutu gönderildi.")
        except Exception as e:
            logger.error(f"Servis durdurma hatası: {e}")

    def _start_alarm_flash(self, count=0):
        if count >= 10:
            return
        color = "#dc2626" if count % 2 == 0 else "#991b1b"
        try:
            self.alarm_banner.configure(fg_color=color)
        except Exception:
            return
        self._alarm_flash_id = self.after(500, self._start_alarm_flash, count + 1)

    def _clear_alarm_banner(self):
        try:
            self.alarm_banner.pack_forget()
            if self._alarm_flash_id:
                self.after_cancel(self._alarm_flash_id)
                self._alarm_flash_id = None
        except Exception:
            pass

    def start_logging(self):
        # Ayarları kaydet (Servis bu dosyadan okur)
        self.save_current_config()
        self.is_logging.set()
        
        # Servisi başlat
        if self.start_service():
            self.set_top_status(True, "BEKLİYOR")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            
            # Canlı izleme thread'ini başlat (Sadece UI güncelleme için)
            self.log_thread = threading.Thread(target=self.live_view_loop, daemon=True)
            self.log_thread.start()
        else:
            messagebox.showerror("Hata", "Arka plan servisi başlatılamadı!")
            self.is_logging.clear()

    def stop_logging(self):
        self.is_logging.clear()
        self.stop_service()
        self.set_top_status(False)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.log_message("Sistem ve Servis Durduruldu.", "#ef4444")
        logger.info("Servis durdurma emri verildi.")

    # === QUERY & PAGINATION (#8) ===

    def query_data(self):
        self.page_offset = 0
        self._fetch_page()

    def next_page(self):
        if self.page_offset + self.page_limit < self.total_records:
            self.page_offset += self.page_limit
            self._fetch_page()

    def prev_page(self):
        if self.page_offset > 0:
            self.page_offset = max(0, self.page_offset - self.page_limit)
            self._fetch_page()

    def first_page(self):
        self.page_offset = 0
        self._fetch_page()

    def last_page(self):
        if self.total_records > 0:
            self.page_offset = ((self.total_records - 1) // self.page_limit) * self.page_limit
            self._fetch_page()

    def _fetch_page(self):
        start = self.start_date_var.get().strip()
        end = self.end_date_var.get().strip()
        func_filter = self.func_filter_var.get()
        tag_filter = self.tag_filter_var.get().strip()

        if len(start) == 16:
            start += ":00"
        if len(end) == 16:
            end += ":59"

        for row in self.tree.get_children():
            self.tree.delete(row)

        try:
            results, self.total_records = database.query_logs(
                start,
                end,
                func_filter,
                self.page_limit,
                self.page_offset,
                tag_filter=tag_filter,
            )
            func_map = {"1": "Read Coils", "2": "Read Discrete Inputs",
                        "3": "Read Holding Regs", "4": "Read Input Regs"}

            if not results and self.page_offset == 0:
                msg = "Bu tarih aralığında"
                if func_filter != "Hepsi":
                    msg += f" ve '{func_filter}' tipinde"
                if tag_filter:
                    msg += f" ve '{tag_filter}' etiketiyle"
                msg += " kayıtlı veri bulunmuyor."
                self.log_message(msg, "#f59e0b")
                self.page_lbl.configure(text="0 kayıt")
                return

            for r in results:
                display_vals = list(r)
                code = str(r[3]) if r[3] else "3"
                
                # Değer formatlama (Coil/DI ise ON/OFF göster)
                if code in ("1", "2"):
                    try:
                        val_num = float(r[2])
                        display_vals[2] = "ON" if val_num > 0.5 else "OFF"
                    except (TypeError, ValueError):
                        pass
                
                display_vals[3] = func_map.get(code, f"Tip {code}")
                self.tree.insert("", "end", values=display_vals)

            page_num = (self.page_offset // self.page_limit) + 1
            total_pages = max(1, (self.total_records + self.page_limit - 1) // self.page_limit)
            self.page_lbl.configure(text=f"Sayfa {page_num}/{total_pages} — Toplam {self.total_records} kayıt")
            if tag_filter:
                self.log_message(f"Sorgu Tamamlandı: {self.total_records} kayıt ({func_filter}, tag='{tag_filter}').", "#10b981")
            else:
                self.log_message(f"Sorgu Tamamlandı: {self.total_records} kayıt ({func_filter}).", "#10b981")
        except Exception as e:
            self.log_message(f"Sorgu Hatası: {str(e)}", "#ef4444")
            logger.error(f"Sorgu hatası: {e}")

    def scroll_treeview(self, amount):
        """Dinamik kaydırma (#10)."""
        children = len(self.tree.get_children())
        step = max(0.005, 1.0 / max(1, children)) * abs(amount)
        current = self.tree.yview()[0]
        new_pos = current + (step if amount > 0 else -step)
        self.tree.yview_moveto(max(0, min(1, new_pos)))

    # === CSV EXPORT & IMPORT ===

    def export_csv(self):
        records = [self.tree.item(row_id)['values'] for row_id in self.tree.get_children()]
        if not records:
            messagebox.showwarning("Boş Tablo", "Aktarılacak veri yok.")
            return
        file_path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV Dosyaları", "*.csv")], title="CSV Olarak Kaydet",
            initialfile=f"modbus_veri_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        if file_path:
            try:
                with open(file_path, mode='w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["Tarih & Saat", "Adres", "Ölçülen Değer", "Tip"])
                    for record in records:
                        writer.writerow(record)
                messagebox.showinfo("Başarılı", "Tablo verileri aktarıldı.")
                logger.info(f"CSV export: {file_path}")
            except Exception as e:
                messagebox.showerror("Hata", str(e))

    def import_csv(self):
        """CSV dosyasından veritabanına veri yükle (#16)."""
        file_path = filedialog.askopenfilename(filetypes=[("CSV Dosyaları", "*.csv")], title="CSV Dosyası Seç")
        if not file_path:
            return
        try:
            count = 0
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter=';')
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 3:
                        ts = row[0].strip() if row[0] else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        name = row[1].strip()
                        val = float(row[2])
                        fc = row[3].strip() if len(row) > 3 else "3"
                        database.insert_log(name, val, fc, timestamp=ts)
                        count += 1
            messagebox.showinfo("Başarılı", f"{count} kayıt içe aktarıldı.")
            logger.info(f"CSV import: {count} kayıt ({file_path})")
            self.query_data()
        except Exception as e:
            messagebox.showerror("İçe Aktarma Hatası", str(e))

    # === E-POSTA (#3 - .env'den oku) ===

    def send_email(self):
        records = [self.tree.item(row_id)['values'] for row_id in self.tree.get_children()]
        if not records:
            messagebox.showwarning("Boş Tablo", "Gönderilecek veri yok.")
            return
        raw_recipients = self.email_var.get().strip()
        if not raw_recipients:
            messagebox.showwarning("Geçersiz E-posta", "Lütfen en az bir alıcı e-posta adresi girin.")
            return

        import re
        recipients_list = [r.strip() for r in re.split(r'[,;]+', raw_recipients) if r.strip()]
        valid_recipients = [r for r in recipients_list if "@" in r]
        if not valid_recipients:
            messagebox.showwarning("Geçersiz E-posta", "Lütfen geçerli formatta bir e-posta adresi girin.")
            return

        smtp = config_manager.get_smtp_config()
        if not smtp["email"] or not smtp["password"]:
            messagebox.showwarning("Ayar Eksikliği", ".env dosyasına SENDER_EMAIL ve SENDER_PASS bilgilerini girin.")
            return

        try:
            temp_filename = f"modbus_veri_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(temp_filename, mode='w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Tarih & Saat", "Adres", "Ölçülen Değer", "Tip"])
                for record in records:
                    writer.writerow(record)

            msg = EmailMessage()
            msg['Subject'] = 'Modbus Data Logger Raporu'
            msg['From'] = smtp["email"]
            msg['To'] = ", ".join(valid_recipients)
            msg.set_content('Merhaba,\n\nModbus geçmiş verileri ekteki CSV dosyasındadır.\n\nİyi çalışmalar.')

            with open(temp_filename, 'rb') as f:
                msg.add_attachment(f.read(), maintype='text', subtype='csv', filename=temp_filename)

            if smtp["port"] == 465:
                server = smtplib.SMTP_SSL(smtp["server"], smtp["port"], timeout=15)
            else:
                server = smtplib.SMTP(smtp["server"], smtp["port"], timeout=15)
            try:
                if smtp["port"] != 465:
                    server.starttls()
                server.login(smtp["email"], smtp["password"])
                server.send_message(msg)
            finally:
                server.quit()

            messagebox.showinfo("Başarılı", "E-posta gönderildi!")
            logger.info(f"E-posta gönderildi: {valid_recipients}")
        except smtplib.SMTPAuthenticationError:
            messagebox.showerror("Kimlik Doğrulama Hatası", ".env dosyasındaki e-posta/şifre bilgileri hatalı.")
        except TimeoutError:
            messagebox.showerror("Zaman Aşımı", f"Sunucuya ({smtp['server']}:{smtp['port']}) bağlanılamadı.")
        except Exception as e:
            messagebox.showerror("Hata", f"E-posta gönderilemedi: {e}")
        finally:
            if 'temp_filename' in locals():
                safe_remove_file(temp_filename)

    def send_alarm_email(self):
        records = [self.alarm_tree.item(row_id)['values'] for row_id in self.alarm_tree.get_children()]
        if not records:
            messagebox.showwarning("Boş Tablo", "Gönderilecek veri yok.")
            return
        raw_recipients = self.alarm_email_var.get().strip()
        if not raw_recipients:
            messagebox.showwarning("Geçersiz E-posta", "Lütfen en az bir alıcı e-posta adresi girin.")
            return

        import re
        recipients_list = [r.strip() for r in re.split(r'[,;]+', raw_recipients) if r.strip()]
        valid_recipients = [r for r in recipients_list if "@" in r]
        if not valid_recipients:
            messagebox.showwarning("Geçersiz E-posta", "Lütfen geçerli formatta bir e-posta adresi girin.")
            return

        smtp = config_manager.get_smtp_config()
        if not smtp["email"] or not smtp["password"]:
            messagebox.showwarning("Ayar Eksikliği", ".env dosyasına SENDER_EMAIL ve SENDER_PASS bilgilerini girin.")
            return

        try:
            temp_filename = f"alarm_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(temp_filename, mode='w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Zaman", "Adres", "Fonks", "Değer", "Min", "Max", "Mesaj"])
                for record in records:
                    writer.writerow(record)

            msg = EmailMessage()
            msg['Subject'] = 'Modbus Alarm Geçmişi Raporu'
            msg['From'] = smtp["email"]
            msg['To'] = ", ".join(valid_recipients)
            msg.set_content('Merhaba,\n\nModbus alarm geçmişi verileri ekteki CSV dosyasındadır.\n\nİyi çalışmalar.')

            with open(temp_filename, 'rb') as f:
                msg.add_attachment(f.read(), maintype='text', subtype='csv', filename=temp_filename)

            if smtp["port"] == 465:
                server = smtplib.SMTP_SSL(smtp["server"], smtp["port"], timeout=15)
            else:
                server = smtplib.SMTP(smtp["server"], smtp["port"], timeout=15)
            try:
                if smtp["port"] != 465:
                    server.starttls()
                server.login(smtp["email"], smtp["password"])
                server.send_message(msg)
            finally:
                server.quit()

            messagebox.showinfo("Başarılı", "E-posta gönderildi!")
            logger.info(f"E-posta gönderildi: {valid_recipients}")
        except smtplib.SMTPAuthenticationError:
            messagebox.showerror("Kimlik Doğrulama Hatası", ".env dosyasındaki e-posta/şifre bilgileri hatalı.")
        except TimeoutError:
            messagebox.showerror("Zaman Aşımı", f"Sunucuya ({smtp['server']}:{smtp['port']}) bağlanılamadı.")
        except Exception as e:
            messagebox.showerror("Hata", f"E-posta gönderilemedi: {e}")
        finally:
            if 'temp_filename' in locals():
                safe_remove_file(temp_filename)

    # === TREND GRAFİK (#12) ===

    def open_trend_graph(self):
        graph_win = ctk.CTkToplevel(self)
        graph_win.title("Trend Grafik [YENI]")
        graph_win.geometry("800x600")
        graph_win.attributes("-topmost", True)

        top = ctk.CTkFrame(graph_win, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 0))

        bottom_controls = ctk.CTkFrame(graph_win, fg_color="transparent")
        bottom_controls.pack(fill="x", padx=10, pady=(5, 10))

        ctk.CTkLabel(top, text="Adres/İsim:").pack(side="left", padx=5)
        reg_var = tk.StringVar()
        ctk.CTkEntry(top, textvariable=reg_var, width=150, placeholder_text="Addr: 0").pack(side="left", padx=5)

        ctk.CTkLabel(top, text="Başlangıç:").pack(side="left", padx=(10, 3))
        graph_start_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 00:00"))
        ctk.CTkEntry(top, textvariable=graph_start_var, width=130).pack(side="left", padx=3)

        ctk.CTkLabel(top, text="Bitiş:").pack(side="left", padx=(10, 3))
        graph_end_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 23:59"))
        ctk.CTkEntry(top, textvariable=graph_end_var, width=130).pack(side="left", padx=3)

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        fig = Figure(figsize=(7, 4), dpi=100, facecolor="#1f2937")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#111827")
        ax.tick_params(colors="white")
        ax.xaxis.label.set_color("white")
        ax.yaxis.label.set_color("white")
        ax.title.set_color("white")
        for spine in ax.spines.values():
            spine.set_color("#374151")

        canvas = FigureCanvasTkAgg(fig, master=graph_win)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=5)

        # Hover tooltip annotation
        annot = ax.annotate("", xy=(0, 0), xytext=(15, 15), textcoords="offset points",
                            bbox=dict(boxstyle="round,pad=0.5", fc="#1e3a5f", ec="#3b82f6", alpha=0.95),
                            fontsize=9, color="white", zorder=100)
        annot.set_visible(False)

        # Veri referansları (draw_graph sonrası doldurulur)
        graph_data = {"timestamps": [], "values": [], "line": None}

        def on_hover(event):
            if event.inaxes != ax or graph_data["line"] is None:
                if annot.get_visible():
                    annot.set_visible(False)
                    canvas.draw_idle()
                return

            line = graph_data["line"]
            contains, ind = line.contains(event)
            if contains:
                idx = ind["ind"][0]
                x, y = line.get_data()
                annot.xy = (x[idx], y[idx])
                full_ts = graph_data["timestamps"][idx]
                # Gün.Ay.Yıl Saat:Dk:Sn formatında göster
                try:
                    from datetime import datetime as dt
                    parsed = dt.strptime(full_ts, "%Y-%m-%d %H:%M:%S")
                    display_ts = parsed.strftime("%d.%m.%Y %H:%M:%S")
                except ValueError:
                    display_ts = full_ts
                annot.set_text(f"📅 {display_ts}\n📊 Değer: {graph_data['values'][idx]}")
                annot.set_visible(True)
                canvas.draw_idle()
            else:
                if annot.get_visible():
                    annot.set_visible(False)
                    canvas.draw_idle()

        fig.canvas.mpl_connect("motion_notify_event", on_hover)

        def draw_graph():
            input_text = reg_var.get().strip()
            if not input_text:
                messagebox.showwarning("Uyarı", "Adres/İsim girin.", parent=graph_win)
                return

            start = graph_start_var.get().strip()
            end = graph_end_var.get().strip()
            if len(start) == 16:
                start += ":00"
            if len(end) == 16:
                end += ":59"

            # Akıllı adres/isim çözümleme
            resolved_names = database.resolve_register_names(input_text)
            if not resolved_names:
                messagebox.showinfo("Bilgi", f"'{input_text}' ile eşleşen kayıt bulunamadı.", parent=graph_win)
                return

            # Tüm eşleşen isimlerden verileri topla
            all_data = []
            used_name = resolved_names[0]
            for rname in resolved_names:
                d = database.get_register_history(rname, start_time=start, end_time=end)
                if d:
                    all_data.extend(d)
                    if len(d) > 0:
                        used_name = rname

            # Zamana göre sırala
            all_data.sort(key=lambda x: x[0])

            if not all_data:
                messagebox.showinfo("Bilgi", f"'{input_text}' için bu tarih aralığında veri bulunamadı.", parent=graph_win)
                return

            data = all_data
            ax.clear()

            timestamps = [d[0] for d in data]
            values = [d[1] for d in data]
            x_indices = list(range(len(values)))

            # Veriyi çiz (x ekseni sayısal index)
            line, = ax.plot(x_indices, values, color="#3b82f6", linewidth=2, marker="o", markersize=4)
            graph_data["timestamps"] = timestamps
            graph_data["values"] = values
            graph_data["line"] = line

            # X ekseninde sadece seyrek etiket göster (max ~8 etiket)
            max_labels = 8
            step = max(1, len(x_indices) // max_labels)
            tick_positions = x_indices[::step]
            tick_labels = []
            for i in tick_positions:
                try:
                    from datetime import datetime as dt
                    parsed = dt.strptime(timestamps[i], "%Y-%m-%d %H:%M:%S")
                    tick_labels.append(parsed.strftime("%H:%M"))
                except (IndexError, TypeError, ValueError):
                    tick_labels.append(timestamps[i][-5:])

            ax.set_xticks(tick_positions)
            ax.set_xticklabels(tick_labels)

            ax.set_title(f"Trend: {used_name}", color="white")
            ax.set_xlabel("Zaman (detay için fare ile üzerine gelin)", color="#9ca3af", fontsize=9)
            ax.set_ylabel("Değer", color="white")
            ax.tick_params(colors="white", labelsize=9)

            # Hover annotation'ı yeniden ekle
            nonlocal annot
            annot = ax.annotate("", xy=(0, 0), xytext=(15, 15), textcoords="offset points",
                                bbox=dict(boxstyle="round,pad=0.5", fc="#1e3a5f", ec="#3b82f6", alpha=0.95),
                                fontsize=9, color="white", zorder=100)
            annot.set_visible(False)

            fig.tight_layout()
            canvas.draw()

        ctk.CTkButton(top, text="Çiz", width=60, command=draw_graph).pack(side="left", padx=5)

        def download_png():
            if not graph_data["values"]:
                messagebox.showwarning("Uyarı", "Önce grafik çizmelisiniz.", parent=graph_win)
                return
            file_path = filedialog.asksaveasfilename(defaultextension=".png",
                filetypes=[("PNG Image", "*.png")], title="Grafiği Kaydet",
                initialfile=f"trend_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
                parent=graph_win)
            if file_path:
                try:
                    fig.savefig(file_path, facecolor=fig.get_facecolor())
                    messagebox.showinfo("Başarılı", "Grafik PNG olarak kaydedildi.", parent=graph_win)
                except Exception as e:
                    messagebox.showerror("Hata", str(e), parent=graph_win)

        def export_csv_trend():
            if not graph_data["values"]:
                messagebox.showwarning("Uyarı", "Önce grafik çizmelisiniz.", parent=graph_win)
                return
            file_path = filedialog.asksaveasfilename(defaultextension=".csv",
                filetypes=[("CSV Dosyaları", "*.csv")], title="Verileri Kaydet",
                initialfile=f"trend_veri_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                parent=graph_win)
            if file_path:
                try:
                    with open(file_path, mode='w', newline='', encoding='utf-8-sig') as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow(["Tarih & Saat", "Değer"])
                        for ts, val in zip(graph_data["timestamps"], graph_data["values"]):
                            writer.writerow([ts, val])
                    messagebox.showinfo("Başarılı", "Veriler aktarıldı.", parent=graph_win)
                except Exception as e:
                    messagebox.showerror("Hata", str(e), parent=graph_win)

        def send_email_trend():
            if not graph_data["values"]:
                messagebox.showwarning("Uyarı", "Önce grafik çizmelisiniz.", parent=graph_win)
                return
            
            # Alıcı istemek için basit bir dialog
            email_dialog = ctk.CTkInputDialog(text="Alıcı E-posta Adresi:", title="E-posta Gönder")
            recipient = email_dialog.get_input()
            if not recipient or "@" not in recipient:
                if recipient is not None:
                    messagebox.showwarning("Hata", "Geçersiz e-posta adresi.", parent=graph_win)
                return

            smtp = config_manager.get_smtp_config()
            if not smtp["email"] or not smtp["password"]:
                messagebox.showwarning("Ayar Eksikliği", ".env dosyasına SENDER_EMAIL ve SENDER_PASS bilgilerini girin.", parent=graph_win)
                return

            try:
                # Geçici dosyalar
                csv_file = f"trend_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                png_file = f"trend_plot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                
                with open(csv_file, mode='w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["Tarih & Saat", "Değer"])
                    for ts, val in zip(graph_data["timestamps"], graph_data["values"]):
                        writer.writerow([ts, val])
                
                fig.savefig(png_file, facecolor=fig.get_facecolor())

                msg = EmailMessage()
                msg['Subject'] = f"Trend Raporu: {reg_var.get()}"
                msg['From'] = smtp["email"]
                msg['To'] = recipient
                msg.set_content(f"Merhaba,\n\n{reg_var.get()} için trend verileri ve grafik ektedir.\n\nİyi çalışmalar.")

                for fpath in [csv_file, png_file]:
                    with open(fpath, 'rb') as f:
                        content = f.read()
                        ctype = "text/csv" if fpath.endswith(".csv") else "image/png"
                        main_t, sub_t = ctype.split('/')
                        msg.add_attachment(content, maintype=main_t, subtype=sub_t, filename=os.path.basename(fpath))

                if smtp["port"] == 465:
                    server = smtplib.SMTP_SSL(smtp["server"], smtp["port"], timeout=15)
                else:
                    server = smtplib.SMTP(smtp["server"], smtp["port"], timeout=15)
                
                try:
                    if smtp["port"] != 465: server.starttls()
                    server.login(smtp["email"], smtp["password"])
                    server.send_message(msg)
                finally:
                    server.quit()

                for fpath in [csv_file, png_file]:
                    safe_remove_file(fpath)

                messagebox.showinfo("Başarılı", "E-posta başarıyla gönderildi!", parent=graph_win)
            except Exception as e:
                messagebox.showerror("Hata", f"E-posta gönderilemedi: {e}", parent=graph_win)

        ctk.CTkButton(bottom_controls, text="📊 PNG İndir", width=120, fg_color="#4b5563", command=download_png).pack(side="left", padx=5)

        ctk.CTkButton(bottom_controls, text="Excel (CSV) Aktar", width=140, fg_color="#059669", command=export_csv_trend).pack(side="left", padx=5)
        ctk.CTkButton(bottom_controls, text="📩 Mail Gönder", width=120, fg_color="#2563eb", command=send_email_trend).pack(side="left", padx=5)

    # === ALARM YÖNETİCİSİ (#11) ===

    def open_alarm_manager(self):
        win = ctk.CTkToplevel(self)
        win.title("Alarm Yöneticisi")
        win.geometry("500x450")
        win.attributes("-topmost", True)
        win.grab_set()

        frm = ctk.CTkFrame(win)
        frm.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(frm, text="Adres / İsim:").grid(row=0, column=0, padx=5, pady=5)
        addr_v = tk.StringVar(value="")
        ctk.CTkEntry(frm, textvariable=addr_v, width=120, placeholder_text="0 veya İsim").grid(row=0, column=1, padx=5)

        ctk.CTkLabel(frm, text="Min:").grid(row=0, column=2, padx=5)
        min_v = tk.StringVar(value="0")
        ctk.CTkEntry(frm, textvariable=min_v, width=60).grid(row=0, column=3, padx=5)

        ctk.CTkLabel(frm, text="Max:").grid(row=0, column=4, padx=5)
        max_v = tk.StringVar(value="100")
        ctk.CTkEntry(frm, textvariable=max_v, width=60).grid(row=0, column=5, padx=5)

        def add_alarm():
            try:
                input_text = addr_v.get().strip()
                if not input_text:
                    return
                
                fc = self.func_var.get().split(" ")[0]
                target_addr = None
                
                # Eğer sayıysa direkt kullan
                if input_text.isdigit():
                    target_addr = int(input_text)
                else:
                    # İsimse veritabanından adresi bul
                    resolved = database.resolve_register_names(input_text)
                    if resolved:
                        import re
                        match = re.search(r'Addr:\s*(\d+)', resolved[0])
                        if match:
                            target_addr = int(match.group(1))
                        else:
                            all_aliases = database.get_all_aliases(fc)
                            for addr, name in all_aliases.items():
                                if name.lower() == input_text.lower():
                                    target_addr = addr
                                    break
                    
                if target_addr is None:
                    self.log_message(f"Hata: '{input_text}' adresi bulunamadı!", "#ef4444")
                    return

                database.set_alarm(target_addr, fc, float(min_v.get()), float(max_v.get()), description="")
                refresh()
                self.log_message(f"Alarm kaydedildi: Adres {target_addr}", "#10b981")
            except ValueError:
                self.log_message("Hata: Geçersiz sayı formatı!", "#ef4444")

        def edit_alarm():
            sel = tree.selection()
            if sel:
                item = tree.item(sel[0])
                vals = item['values']
                # "0 (Sıcaklık)" formatından sadece "0" kısmını çek
                original_addr = str(vals[0]).split(" ")[0]
                addr_v.set(original_addr)
                min_v.set(str(vals[1]))
                max_v.set(str(vals[2]))
            else:
                self.log_message("Lütfen düzenlemek için bir alarm seçin.", "#f59e0b")

        def del_alarm():
            sel = tree.selection()
            if sel:
                item = tree.item(sel[0])
                fc = self.func_var.get().split(" ")[0]
                addr = int(str(item['values'][0]).split(" ")[0])
                database.delete_alarm(addr, fc)
                refresh()
                self.log_message("Alarm silindi.", "#ef4444")

        ctk.CTkButton(frm, text="Ekle/Güncelle", width=100, fg_color="#2da44e", command=add_alarm).grid(row=0, column=6, padx=5)

        tree = ttk.Treeview(win, columns=("Addr", "Min", "Max", "Aktif"), show="headings", height=12)
        tree.heading("Addr", text="Adres / Tanım")
        tree.heading("Min", text="Min")
        tree.heading("Max", text="Max")
        tree.heading("Aktif", text="Aktif")
        tree.column("Addr", width=150)
        tree.column("Min", width=80)
        tree.column("Max", width=80)
        tree.column("Aktif", width=60)
        tree.pack(fill="both", expand=True, padx=10, pady=5)

        btn_frm = ctk.CTkFrame(win, fg_color="transparent")
        btn_frm.pack(pady=10)
        
        ctk.CTkButton(btn_frm, text="Seçileni Düzenle", fg_color="#3b82f6", command=edit_alarm).pack(side="left", padx=5)
        ctk.CTkButton(btn_frm, text="Seçileni Sil", fg_color="#ef4444", command=del_alarm).pack(side="left", padx=5)

        def refresh():
            for r in tree.get_children():
                tree.delete(r)
            fc = self.func_var.get().split(" ")[0]
            aliases = database.get_all_aliases(fc)
            for a in database.get_alarms(fc):
                addr = a[1]
                name = aliases.get(addr, "")
                display_addr = f"{addr} ({name})" if name else str(addr)
                tree.insert("", "end", values=(display_addr, a[3], a[4], "Evet" if a[5] else "Hayır"))
        refresh()

    # === DB YÖNETİCİSİ (#18) ===

    def _load_service_health_snapshot(self):
        health_path = os.path.join(config_manager.PROJECT_ROOT, "service_health.json")
        if not os.path.exists(health_path):
            return None
        try:
            with open(health_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Servis saglik ozeti okunamadi: {exc}")
            return None

    @staticmethod
    def _format_health_time(timestamp):
        if not timestamp:
            return "-"
        try:
            return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return "-"

    def _show_service_health_summary(self, parent_win):
        snapshot = self._load_service_health_snapshot()
        if not snapshot:
            messagebox.showwarning("Servis Sagligi", "service_health.json bulunamadi.", parent=parent_win)
            return

        startup = snapshot.get("startup_health") or {}
        watchdog = snapshot.get("watchdog") or {}
        function_lines = []
        for code, payload in sorted((snapshot.get("functions") or {}).items()):
            if not payload:
                continue
            label = payload.get("label") or config_manager.get_function_label(code)
            function_lines.append(
                f"{label}: anlik nokta={payload.get('cycle_live_points', 0)}, "
                f"son gorulme={self._format_health_time(payload.get('last_seen_at'))}, "
                f"son DB yazimi={self._format_health_time(payload.get('last_db_log_at'))}, "
                f"live={payload.get('live_status', '-')}, db={payload.get('db_status', '-')}"
            )

        summary = [
            f"Guncelleme: {self._format_health_time(snapshot.get('updated_at'))}",
            f"Servis baslangici: {self._format_health_time(snapshot.get('service_started_at'))}",
            f"Canli nokta sayisi: {snapshot.get('live_point_count', 0)}",
            f"Son live snapshot: {self._format_health_time(snapshot.get('last_live_snapshot_at'))}",
            f"Son DB flush: {self._format_health_time(snapshot.get('last_db_flush_at'))}",
            f"Acilis sagligi: {startup.get('status', '-')}",
            f"Runtime watchdog: {watchdog.get('status', '-')}",
        ]
        thresholds = watchdog.get("thresholds") or {}
        if thresholds:
            summary.append(
                f"Watchdog esikleri: live={int(thresholds.get('live_sec', 0))} sn / db={int(thresholds.get('db_sec', 0))} sn"
            )
        if snapshot.get("last_error"):
            summary.append(f"Son hata: {snapshot['last_error']}")
        if watchdog.get("active_issues"):
            summary.append("")
            summary.append("Aktif watchdog alarmlari:")
            summary.extend(watchdog["active_issues"])
        if function_lines:
            summary.append("")
            summary.extend(function_lines)

        messagebox.showinfo("Servis Saglik Ozeti", "\n".join(summary), parent=parent_win)

    def open_polling_profile_manager(self):
        self.save_current_config(profile_code=self.active_profile_code, active_func_choice=self.func_var.get())
        self.config = config_manager.load_config()

        win = ctk.CTkToplevel(self)
        win.title("Kalici Izleme Profilleri")
        win.geometry("620x360")
        win.attributes("-topmost", True)
        win.grab_set()

        ctk.CTkLabel(
            win,
            text="Servis, PC acildiginda bu profilleri birlikte tarar ve loglar.",
            justify="left",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(15, 10))

        grid = ctk.CTkFrame(win)
        grid.pack(fill="both", expand=True, padx=20, pady=10)

        headers = ["Aktif", "Fonksiyon", "Slave", "Baslangic", "Count"]
        for col, header in enumerate(headers):
            ctk.CTkLabel(grid, text=header, font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=col, padx=8, pady=(8, 12), sticky="w"
            )

        profile_vars = {}
        for row_index, (code, label) in enumerate(config_manager.FUNCTION_LABELS.items(), start=1):
            profile = config_manager.get_polling_profile(self.config, code)
            enabled_var = tk.BooleanVar(value=profile.get("enabled", False))
            slave_var = tk.IntVar(value=profile.get("slave_id", 1))
            start_var = tk.IntVar(value=profile.get("start_addr", 0))
            count_var = tk.IntVar(value=profile.get("count", 1))
            profile_vars[code] = {
                "enabled": enabled_var,
                "slave_id": slave_var,
                "start_addr": start_var,
                "count": count_var,
            }

            ctk.CTkCheckBox(grid, text="", variable=enabled_var, width=24).grid(
                row=row_index, column=0, padx=8, pady=6
            )
            ctk.CTkLabel(grid, text=label, anchor="w").grid(
                row=row_index, column=1, padx=8, pady=6, sticky="w"
            )
            ctk.CTkEntry(grid, textvariable=slave_var, width=80).grid(
                row=row_index, column=2, padx=8, pady=6
            )
            ctk.CTkEntry(grid, textvariable=start_var, width=100).grid(
                row=row_index, column=3, padx=8, pady=6
            )
            ctk.CTkEntry(grid, textvariable=count_var, width=100).grid(
                row=row_index, column=4, padx=8, pady=6
            )

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=20, pady=(0, 15))

        def apply_scada_defaults():
            for code, payload in profile_vars.items():
                payload["enabled"].set(code in {"1", "3"})
                if code in {"1", "3"}:
                    payload["slave_id"].set(1)
                    payload["start_addr"].set(0)
                    payload["count"].set(max(20, self._coerce_int(payload["count"].get(), 1, minimum=1)))

        def save_profiles():
            enabled_codes = [code for code, payload in profile_vars.items() if payload["enabled"].get()]
            if not enabled_codes:
                messagebox.showwarning("Uyari", "En az bir polling profili aktif olmalidir.", parent=win)
                return

            config_dict = {
                **self.config,
                "ip": self.ip_var.get().strip() or "188.38.164.83",
                "port": self._coerce_int(self.port_var.get(), 502, minimum=1),
                "slave_id": self._coerce_int(self.slave_var.get(), 1, minimum=1),
                "func_code": config_manager.get_function_label(self.func_var.get()),
                "start_addr": self._coerce_int(self.start_addr_var.get(), 0, minimum=0),
                "count": self._coerce_int(self.count_var.get(), 1, minimum=1),
                "log_interval": self._coerce_float_string(self.interval_var.get(), 1.0, minimum=0.1),
                "live_rate": self._coerce_float_string(self.live_rate_var.get(), 1.0, minimum=0.2),
                "startup_health_timeout_sec": self.config.get("startup_health_timeout_sec", 90),
            }

            for code, payload in profile_vars.items():
                config_dict = config_manager.upsert_polling_profile(
                    config_dict,
                    code,
                    slave_id=self._coerce_int(payload["slave_id"].get(), 1, minimum=1),
                    start_addr=self._coerce_int(payload["start_addr"].get(), 0, minimum=0),
                    count=self._coerce_int(payload["count"].get(), 1, minimum=1),
                    enabled=bool(payload["enabled"].get()),
                )

            self.config = config_manager.normalize_config(config_dict)
            if not self.config["polling_profiles"][self.active_profile_code]["enabled"]:
                self.active_profile_code = enabled_codes[0]
                self.func_var.set(config_manager.get_function_label(self.active_profile_code))

            self._load_profile_fields(self.active_profile_code)
            config_manager.save_config(self.config)
            self.log_message("Kalici polling profilleri kaydedildi.", "#10b981")
            messagebox.showinfo("Basarili", "Kalici polling profilleri guncellendi.", parent=win)
            win.destroy()

        ctk.CTkButton(
            btn_bar,
            text="SCADA Varsayilani (1+3 / 20)",
            fg_color="#1d4ed8",
            hover_color="#1e40af",
            command=apply_scada_defaults,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_bar,
            text="Kaydet",
            fg_color="#059669",
            hover_color="#047857",
            command=save_profiles,
        ).pack(side="right")

    def open_db_manager(self):
        win = ctk.CTkToplevel(self)
        win.title("Veritabani Yonetimi")
        win.geometry("460x520")
        win.attributes("-topmost", True)

        stats = database.get_db_stats()
        normalization_plan = database.get_register_name_normalization_plan()
        pending_groups = len(normalization_plan)
        pending_rows = sum(item["total_rows"] for item in normalization_plan)
        enabled_profiles = config_manager.get_polling_profiles(self.config)
        service_health = self._load_service_health_snapshot()
        profile_summary = ", ".join(
            f"{config_manager.get_function_label(profile['func_code'])}: {profile['start_addr']}-{profile['start_addr'] + profile['count'] - 1}"
            for profile in enabled_profiles
        ) or "-"
        health_brief = "-"
        if service_health:
            startup = service_health.get("startup_health") or {}
            health_brief = (
                f"{startup.get('status', '-')} / live={service_health.get('live_point_count', 0)} / "
                f"guncelleme={self._format_health_time(service_health.get('updated_at'))}"
            )

        info = f"Toplam Kayit: {stats['total_logs']}\nDosya Boyutu: {stats['file_size_mb']} MB\n"
        info += f"En Eski: {stats['oldest'] or '-'}\nEn Yeni: {stats['newest'] or '-'}\n"
        info += f"Bekleyen Tag Normalizasyonu: {pending_groups} grup / {pending_rows} satir\n"
        info += f"Aktif Polling Profilleri: {profile_summary}\n"
        info += f"Servis Sagligi: {health_brief}"
        ctk.CTkLabel(win, text=info, justify="left", font=ctk.CTkFont(size=13)).pack(padx=20, pady=15)

        ctk.CTkButton(win, text="Veritabanini Yedekle", fg_color="#3b82f6",
                      command=lambda: self._do_backup(win)).pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(win, text="Yedek Dosyasini Goruntule", fg_color="#10b981",
                      hover_color="#059669", command=lambda: self._open_backup_viewer(win)).pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(win, text="Yedekten Veri Aktar", fg_color="#d97706",
                      hover_color="#b45309", command=lambda: self._do_import_from_backup(win)).pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(win, text="Tag Isimlerini Normalize Et", fg_color="#0f766e",
                      hover_color="#115e59", command=lambda: self._normalize_historical_tags(win)).pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(win, text="Servis Saglik Ozetini Goster", fg_color="#475569",
                      hover_color="#334155", command=lambda: self._show_service_health_summary(win)).pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(win, text="Kalici Profilleri Ac", fg_color="#1d4ed8",
                      hover_color="#1e40af", command=self.open_polling_profile_manager).pack(padx=20, pady=5, fill="x")

        ctk.CTkLabel(win, text="Eski kayitlari sil (gun):").pack(padx=20, pady=(15, 5))
        days_var = tk.IntVar(value=90)
        ctk.CTkEntry(win, textvariable=days_var, width=100).pack()

        def do_cleanup():
            if messagebox.askyesno("Onay", f"{days_var.get()} gunden eski kayitlar silinecek. Devam?", parent=win):
                deleted = database.cleanup_old_logs(days_var.get())
                messagebox.showinfo("Tamamlandi", f"{deleted} kayit silindi.", parent=win)
                win.destroy()
                self.open_db_manager()

        ctk.CTkButton(win, text="Eski Kayitlari Temizle", fg_color="#ef4444",
                      hover_color="#dc2626", command=do_cleanup).pack(padx=20, pady=10, fill="x")

    def _normalize_historical_tags(self, parent_win):
        plan = database.get_register_name_normalization_plan()
        if not plan:
            messagebox.showinfo("Bilgi", "Normalize edilecek tarihsel tag varyasyonu bulunamadi.", parent=parent_win)
            return

        total_groups = len(plan)
        total_rows = sum(item["total_rows"] for item in plan)
        preview_lines = []
        for item in plan[:5]:
            variants = ", ".join(f"{variant['name']} ({variant['count']})" for variant in item["variants"])
            preview_lines.append(f"{item['canonical_name']} <= {variants}")
        preview = "\n".join(preview_lines)
        if total_groups > 5:
            preview += f"\n... +{total_groups - 5} grup daha"

        prompt = (
            f"{total_groups} grup ve {total_rows} satir normalize edilecek.\n\n"
            f"Onizleme:\n{preview}\n\n"
            "Guvenlik icin once otomatik yedek alinacak. Devam edilsin mi?"
        )
        if not messagebox.askyesno("Tag Normalizasyonu", prompt, parent=parent_win):
            return

        backup_path = database.backup_db()
        if not backup_path and not messagebox.askyesno(
            "Yedek Alinamadi",
            "Otomatik yedek alinamadi. Yine de normalizasyona devam etmek istiyor musunuz?",
            parent=parent_win,
        ):
            return

        result = database.normalize_historical_register_names()
        messagebox.showinfo(
            "Tamamlandi",
            f"{result['updated_rows']} satir ve {result['groups']} grup duzeltildi."
            + (f"\nYedek: {backup_path}" if backup_path else ""),
            parent=parent_win,
        )
        self.query_data()
        parent_win.destroy()
        self.open_db_manager()

    def _do_backup(self, parent_win):
        path = database.backup_db()
        if path:
            messagebox.showinfo("Yedekleme", f"Yedek alındı:\n{path}", parent=parent_win)
        else:
            messagebox.showerror("Hata", "Yedekleme başarısız.", parent=parent_win)

    def _do_import_from_backup(self, parent_win):
        file_path = filedialog.askopenfilename(
            title="Aktarılacak Yedek Veritabanını Seçin",
            filetypes=[("SQLite Database", "*.db"), ("All Files", "*.*")],
            parent=parent_win
        )
        if not file_path:
            return
        
        if messagebox.askyesno("Veri Aktarma", f"'{os.path.basename(file_path)}' içindeki tüm veriler mevcut veritabanına eklenecektir.\n\nDevam etmek istiyor musunuz?", parent=parent_win):
            success, msg = database.import_logs_from_external_db(file_path)
            if success:
                messagebox.showinfo("Başarılı", msg, parent=parent_win)
                # Tabloları yenilemek için gerekirse query_data çağrılabilir
                self.query_data()
            else:
                messagebox.showerror("Hata", msg, parent=parent_win)

    def _open_backup_viewer(self, parent_win):
        file_path = filedialog.askopenfilename(
            title="Yedek Veritabanı Seçin",
            filetypes=[("SQLite Database", "*.db"), ("All Files", "*.*")],
            parent=parent_win
        )
        if not file_path:
            return
        
        # Seçilen dosyayı ayrı bir pencerede aç
        BackupViewerWindow(self, file_path)

    # === ALIAS MANAGER ===

    def open_alias_manager(self):
        alias_win = ctk.CTkToplevel(self)
        alias_win.title("Adres İsimlendirme")
        alias_win.geometry("400x500")
        alias_win.attributes("-topmost", True)
        alias_win.grab_set()

        frm_top = ctk.CTkFrame(alias_win)
        frm_top.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(frm_top, text="Adres:").grid(row=0, column=0, padx=5, pady=5)
        addr_var = tk.IntVar(value=0)
        ctk.CTkEntry(frm_top, textvariable=addr_var, width=80).grid(row=0, column=1, padx=5, pady=5)

        ctk.CTkLabel(frm_top, text="İsim:").grid(row=0, column=2, padx=5, pady=5)
        name_var = tk.StringVar(value="")
        ctk.CTkEntry(frm_top, textvariable=name_var, width=120).grid(row=0, column=3, padx=5, pady=5)

        def clear_inputs():
            addr_var.set(0)
            name_var.set("")
            for item in tree.selection():
                tree.selection_remove(item)

        def add_alias():
            try:
                addr = addr_var.get()
                name = name_var.get().strip()
                if not name:
                    messagebox.showwarning("Uyarı", "İsim boş olamaz.", parent=alias_win)
                    return
                func_code = self.func_var.get().split(" ")[0]
                database.set_alias(addr, func_code, name)
                self.aliases[addr] = name
                refresh_list()
                clear_inputs()
            except Exception as e:
                messagebox.showerror("Hata", str(e), parent=alias_win)

        def delete_alias():
            selected = tree.selection()
            if not selected:
                return
            item = tree.item(selected[0])
            addr = int(item['values'][0])
            func_code = self.func_var.get().split(" ")[0]
            database.delete_alias(addr, func_code)
            if addr in self.aliases:
                del self.aliases[addr]
            refresh_list()
            clear_inputs()

        btn_frame = ctk.CTkFrame(frm_top, fg_color="transparent")
        btn_frame.grid(row=1, column=0, columnspan=4, pady=10)
        ctk.CTkButton(btn_frame, text="Temizle", command=clear_inputs, width=70, fg_color="gray").pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Kaydet", command=add_alias, width=80, fg_color="#2da44e").pack(side="left", padx=5)

        frm_mid = ctk.CTkFrame(alias_win)
        frm_mid.pack(fill="both", expand=True, padx=10, pady=5)

        tree = ttk.Treeview(frm_mid, columns=("Address", "Name"), show="headings")
        tree.heading("Address", text="Adres No")
        tree.heading("Name", text="Özel İsim")
        tree.column("Address", width=80, anchor=tk.CENTER)
        tree.column("Name", width=250, anchor=tk.W)
        tree.pack(fill="both", expand=True, side="left")

        scroll = ttk.Scrollbar(frm_mid, command=tree.yview)
        scroll.pack(fill="y", side="right")
        tree.configure(yscrollcommand=scroll.set)

        def refresh_list():
            for row in tree.get_children():
                tree.delete(row)
            for addr, name in sorted(self.aliases.items()):
                tree.insert("", "end", values=(addr, name))

        def on_select(event):
            selected = tree.selection()
            if selected:
                item = tree.item(selected[0])
                addr_var.set(int(item['values'][0]))
                name_var.set(item['values'][1])

        tree.bind("<<TreeviewSelect>>", on_select)

        frm_bot = ctk.CTkFrame(alias_win, fg_color="transparent")
        frm_bot.pack(fill="x", padx=10, pady=10)
        ctk.CTkButton(frm_bot, text="Seçileni Sil", command=delete_alias, fg_color="#ef4444").pack(side="left")

        refresh_list()


class BackupViewerWindow(ctk.CTkToplevel):
    """Yedek veritabanı dosyalarını görüntülemek için özel pencere."""
    def __init__(self, parent, db_path):
        super().__init__(parent)
        self.db_path = db_path
        self.title(f"Yedek İzleyici: {os.path.basename(db_path)}")
        self.geometry("1000x650")
        
        self.page_offset = 0
        self.page_limit = 500
        self.total_records = 0

        # İstatistikler
        stats = database.get_db_stats_external(db_path)
        
        # Filtre Paneli
        top_frame = ctk.CTkFrame(self)
        top_frame.pack(fill="x", padx=10, pady=10)
        
        # SADECE OKUMA BANNERI
        ctk.CTkLabel(top_frame, text="⚠ SADECE GÖRÜNTÜLEME MODU (READ-ONLY)", 
                      font=ctk.CTkFont(size=14, weight="bold"),
                      text_color="white", fg_color="#ea580c", corner_radius=5).pack(fill="x", pady=(0, 10))
        
        stats_text = f"Dosya: {os.path.basename(db_path)} | Toplam Kayıt: {stats['total_logs'] if stats else '?'}"
        ctk.CTkLabel(top_frame, text=stats_text, font=ctk.CTkFont(weight="bold")).pack(side="top", pady=5)
        
        filter_frame = ctk.CTkFrame(top_frame, fg_color="transparent")
        filter_frame.pack(fill="x", pady=5)

        self.start_date_var = tk.StringVar(value="2020-01-01 00:00")
        self.end_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d 23:59"))
        self.func_filter_var = ctk.StringVar(value="Hepsi")

        ctk.CTkLabel(filter_frame, text="Başlangıç:").pack(side="left", padx=5)
        ctk.CTkEntry(filter_frame, textvariable=self.start_date_var, width=130).pack(side="left", padx=5)

        ctk.CTkLabel(filter_frame, text="Bitiş:").pack(side="left", padx=5)
        ctk.CTkEntry(filter_frame, textvariable=self.end_date_var, width=130).pack(side="left", padx=5)

        ctk.CTkLabel(filter_frame, text="Filtre:").pack(side="left", padx=5)
        ctk.CTkComboBox(filter_frame, variable=self.func_filter_var, values=[
            "Hepsi", "1 (Read Coils)", "2 (Read Discrete Inputs)",
            "3 (Read Holding Registers)", "4 (Read Input Registers)"
        ], width=150).pack(side="left", padx=5)

        self.query_btn = ctk.CTkButton(filter_frame, text="Verileri Getir", width=100, command=self.query_data)
        self.query_btn.pack(side="left", padx=10)
        ctk.CTkButton(filter_frame, text="CSV'ye Aktar", width=100, fg_color="#d97706", command=self.export_csv).pack(side="left", padx=2)

        # Tablo
        self.tree_frame = ctk.CTkFrame(self)
        self.tree_frame.pack(expand=True, fill="both", padx=10, pady=(0, 10))

        self.tree = ttk.Treeview(self.tree_frame, columns=("Timestamp", "Address", "Value", "Function"), show="headings")
        self.tree.heading("Timestamp", text="Tarih & Saat")
        self.tree.heading("Address", text="Adres / İsim")
        self.tree.heading("Value", text="Ölçülen Değer")
        self.tree.heading("Function", text="Tip (Fonksiyon)")
        self.tree.column("Timestamp", width=160, anchor=tk.CENTER)
        self.tree.column("Address", width=180, anchor=tk.W)
        self.tree.column("Value", width=100, anchor=tk.CENTER)
        self.tree.column("Function", width=160, anchor=tk.W)
        self.tree.pack(side="left", expand=True, fill="both")

        sb = ctk.CTkScrollbar(self.tree_frame, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        # Sayfalama
        page_frame = ctk.CTkFrame(self, fg_color="transparent", height=40)
        page_frame.pack(fill="x", padx=10, pady=5)
        
        self.page_lbl = ctk.CTkLabel(page_frame, text="")
        self.page_lbl.pack(side="left", padx=10)

        ctk.CTkButton(page_frame, text="Sonraki ▶", width=80, command=self.next_page).pack(side="right", padx=5)
        ctk.CTkButton(page_frame, text="◀ Önceki", width=80, command=self.prev_page).pack(side="right", padx=5)

        self.query_data()
        self.focus_force()

    def query_data(self):
        self.query_btn.configure(state="disabled", text="Yükleniyor...")
        self.update_idletasks()
        
        for item in self.tree.get_children(): self.tree.delete(item)
        start = self.start_date_var.get()
        end = self.end_date_var.get()
        func = self.func_filter_var.get()
        
        results, self.total_records = database.query_logs_external(
            self.db_path, start, end, func, self.page_limit, self.page_offset
        )
        for row in results:
            self.tree.insert("", "end", values=row)
            
        page_num = (self.page_offset // self.page_limit) + 1
        total_pages = max(1, (self.total_records + self.page_limit - 1) // self.page_limit)
        self.page_lbl.configure(text=f"Sayfa {page_num}/{total_pages} — Toplam {self.total_records} kayıt")
        self.query_btn.configure(state="normal", text="Verileri Getir")

    def next_page(self):
        if self.page_offset + self.page_limit < self.total_records:
            self.page_offset += self.page_limit
            self.query_data()

    def prev_page(self):
        if self.page_offset > 0:
            self.page_offset = max(0, self.page_offset - self.page_limit)
            self.query_data()

    def export_csv(self):
        records = [self.tree.item(row_id)['values'] for row_id in self.tree.get_children()]
        if not records: return
        file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")])
        if file_path:
            import csv
            with open(file_path, mode='w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Tarih", "Adres", "Değer", "Fonksiyon"])
                for record in records: writer.writerow(record)
            messagebox.showinfo("Başarılı", "Dışa aktarma tamamlandı.")


if __name__ == "__main__":
    if is_already_running():
        # Tkinter window to show error since main app hasn't started
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Modvera", "Uygulama zaten çalışıyor!\nLütfen sistem tepsisini (sağ alt köşe) kontrol edin.")
        root.destroy()
        sys.exit(0)

    try:
        database.init_db()
        # Login ekranı (#17)
        login = LoginWindow()
        login.mainloop()
        
        if login.user_info:
            app = ModbusLoggerApp(user_info=login.user_info)
            app.mainloop()
    except Exception as e:
        import traceback
        with open("crash_log.txt", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        print(f"HATA: {e}")
    finally:
        if _instance_socket:
            _instance_socket.close()
