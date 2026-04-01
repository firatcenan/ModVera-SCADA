"""
ai_panel.py — Modvera AI Analiz Paneli
main.py icindeki uygun yere import edip AiAnalysisPanel sinifini kullanin.

KULLANIM (main.py icinde):
    from ai_panel import AiAnalysisPanel

    # Bir yerde (ornegin menu veya toolbar'da) buton:
    btn = ctk.CTkButton(parent, text="AI Analiz", command=self._open_ai_panel)

    def _open_ai_panel(self):
        # live_readings: canli_veri listesi [(tag, deger, fc), ...]
        # Bunu mevcut live_data.json'dan okuyun:
        readings = self._get_live_readings()
        AiAnalysisPanel(self, readings)

    def _get_live_readings(self):
        import json, os, config_manager
        path = os.path.join(config_manager.PROJECT_ROOT, "live_data.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return [tuple(item) for item in data.get("data", [])]
        except Exception:
            return []
"""

import customtkinter as ctk
import ai_analyst


class AiAnalysisPanel(ctk.CTkToplevel):
    """
    Bagimsiz bir pencere olarak acar.
    Acilinca otomatik analiz baslatir.
    """

    def __init__(self, parent, readings):
        super().__init__(parent)
        self.title("AI Sistem Analizi")
        self.geometry("680x560")
        self.resizable(True, True)
        self.grab_set()  # Modal gibi davran

        self._readings = readings
        self._build_ui()
        self._start_analysis()

    def _build_ui(self):
        # Baslik
        ctk.CTkLabel(
            self,
            text="AI Sistem Analizi",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(16, 4), padx=20, anchor="w")

        ctk.CTkLabel(
            self,
            text=f"Analiz edilen tag sayisi: {len(self._readings)}",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        ).pack(padx=20, anchor="w")

        # Durum etiketi
        self._status_label = ctk.CTkLabel(
            self,
            text="Analiz yapiliyor, lutfen bekleyin...",
            font=ctk.CTkFont(size=13),
            text_color="#3B8BD4",
        )
        self._status_label.pack(pady=(12, 4), padx=20, anchor="w")

        # Rapor kutusu
        self._text_box = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(size=13),
            wrap="word",
            state="disabled",
        )
        self._text_box.pack(fill="both", expand=True, padx=20, pady=(4, 8))

        # Buton satiri
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 16))

        self._retry_btn = ctk.CTkButton(
            btn_frame,
            text="Yenile",
            width=110,
            command=self._start_analysis,
            state="disabled",
        )
        self._retry_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame,
            text="Kapat",
            width=110,
            fg_color="gray40",
            hover_color="gray30",
            command=self.destroy,
        ).pack(side="left")

    def _start_analysis(self):
        self._set_text("")
        self._status_label.configure(
            text="Analiz yapiliyor, lutfen bekleyin...",
            text_color="#3B8BD4",
        )
        self._retry_btn.configure(state="disabled")

        ai_analyst.analyze_async(
            self._readings,
            on_result=self._on_result,
            on_error=self._on_error,
        )

    def _on_result(self, rapor):
        # Arka plandan GUI guncellemesi icin after() kullan
        self.after(0, lambda: self._show_result(rapor))

    def _on_error(self, hata):
        self.after(0, lambda: self._show_error(hata))

    def _show_result(self, rapor):
        self._status_label.configure(
            text="Analiz tamamlandi.",
            text_color="#1D9E75",
        )
        self._set_text(rapor)
        self._retry_btn.configure(state="normal")

    def _show_error(self, hata):
        self._status_label.configure(
            text="Hata olustu.",
            text_color="#E24B4A",
        )
        self._set_text(f"HATA:\n\n{hata}")
        self._retry_btn.configure(state="normal")

    def _set_text(self, text):
        self._text_box.configure(state="normal")
        self._text_box.delete("1.0", "end")
        if text:
            self._text_box.insert("1.0", text)
        self._text_box.configure(state="disabled")
