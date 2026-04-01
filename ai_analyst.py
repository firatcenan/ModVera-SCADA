"""
ai_analyst.py — Modvera AI Analiz Modulu
Butona basilinca canli tag verilerini Claude API'ye gonderir,
Turkce genel durum raporu alir ve GUI'de gosterir.
"""

import json
import os
import threading
import urllib.request
import urllib.error

import config_manager

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-20250514"
API_KEY_ENV    = "ANTHROPIC_API_KEY"


def _get_api_key():
    """API anahtarini once .env'den, sonra ortam degiskenindem oku."""
    key = os.getenv(API_KEY_ENV, "").strip()
    return key if key else None


def _build_prompt(readings):
    """
    readings: [(tag_adi, deger, fonksiyon_kodu), ...]
    """
    lines = []
    for name, val, fc in readings:
        fc_label = config_manager.FUNCTION_LABELS.get(str(fc).split()[0], f"FC{fc}")
        lines.append(f"  - {name}: {val}  [{fc_label}]")

    tag_block = "\n".join(lines) if lines else "  (Veri yok)"

    return (
        "Sen endustriyel bir SCADA sisteminin yapay zeka analistisisin. "
        "Asagida su anki canli tag degerleri verilmistir.\n\n"
        f"CANLI TAG DEGERLERI:\n{tag_block}\n\n"
        "Lutfen asagidaki basliklar altinda TURKCE kisa bir genel durum raporu yaz:\n"
        "1. GENEL DURUM: Sistem genel olarak nasil? (1-2 cumle)\n"
        "2. DIKKAT GEREKEN NOKTALAR: Anormal gorunen, yuksek/dusuk olan degerler varsa listele.\n"
        "3. NORMAL CALISANLAR: Sorunsuz gorunen taglar.\n"
        "4. TAVSIYE: Operatore kisa bir tavsiye.\n\n"
        "Raporu sade, net ve teknik olmayan bir dille yaz. "
        "Maksimum 250 kelime."
    )


def _call_claude_api(prompt, api_key, timeout=30):
    """
    Claude API'yi cagir, metin yaniti dondur.
    Hata durumunda (str, True) -> (hata_mesaji, True) seklinde dondurur.
    Basarida (rapor_metni, False) dondurur.
    """
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            return text.strip(), False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            msg = body
        return f"API Hatasi ({e.code}): {msg}", True
    except urllib.error.URLError as e:
        return f"Baglanti hatasi: {e.reason}", True
    except Exception as e:
        return f"Beklenmeyen hata: {e}", True


def analyze_async(readings, on_result, on_error):
    """
    Analizi arka planda calistir (GUI donmemesi icin).

    readings : [(tag_adi, deger, fonksiyon_kodu), ...]
    on_result: basarida cagrilir -> on_result(rapor_str)
    on_error : hatada cagrilir  -> on_error(hata_str)
    """
    api_key = _get_api_key()
    if not api_key:
        on_error(
            "ANTHROPIC_API_KEY bulunamadi.\n"
            "Proje klasorundeki .env dosyasina su satiri ekleyin:\n"
            "ANTHROPIC_API_KEY=sk-ant-..."
        )
        return

    if not readings:
        on_error("Analiz edilecek canli veri yok.\nOnce servisi baslatin.")
        return

    def _worker():
        prompt = _build_prompt(readings)
        rapor, is_err = _call_claude_api(prompt, api_key)
        if is_err:
            on_error(rapor)
        else:
            on_result(rapor)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def analyze_now(readings, timeout=30):
    """
    Senkron versiyon (test/CLI icin).
    Dondurur: (rapor_str, hata_var_mi: bool)
    """
    api_key = _get_api_key()
    if not api_key:
        return "ANTHROPIC_API_KEY bulunamadi.", True
    if not readings:
        return "Canli veri yok.", True
    prompt = _build_prompt(readings)
    return _call_claude_api(prompt, api_key, timeout=timeout)
