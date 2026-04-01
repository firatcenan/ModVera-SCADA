import time
import logging
from pymodbus.client import ModbusTcpClient

logger = logging.getLogger("scada.modbus")


class ModbusConnection:
    """
    Kalıcı Modbus TCP bağlantısı yöneticisi.
    - Tek bağlantı üzerinden tekrarlı okuma (#2)
    - Otomatik yeniden bağlanma (#15)
    - Güvenli kaynak yönetimi (#6)
    """

    def __init__(self, ip, port, slave_id, timeout=5, max_retries=3):
        self.ip = ip
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = None
        self._connected = False

    def connect(self):
        """Bağlantı kur veya mevcut bağlantıyı kontrol et."""
        try:
            if self.client and self._connected:
                # Basit bir socket kontrolü eklenebilir ama pymodbus client.connect() bunu yapar
                if self.client.is_socket_open():
                    return True

            # Mevcut ama kopuk client varsa temizle
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass

            self.client = ModbusTcpClient(self.ip, port=self.port, timeout=self.timeout)
            if self.client.connect():
                self._connected = True
                logger.info(f"Modbus bağlantısı kuruldu: {self.ip}:{self.port}")
                return True
            else:
                self._connected = False
                logger.warning(f"Modbus bağlantısı kurulamadı: {self.ip}:{self.port}")
                return False
        except Exception as e:
            self._connected = False
            logger.error(f"Bağlantı hatası ({self.ip}:{self.port}): {e}")
            return False

    def disconnect(self):
        """Bağlantıyı güvenle kapat."""
        try:
            if self.client:
                self.client.close()
                logger.info("Modbus bağlantısı kapatıldı.")
        except Exception as e:
            logger.error(f"Bağlantı kapatma hatası: {e}")
        finally:
            self._connected = False
            self.client = None

    def _reconnect(self):
        """Otomatik yeniden bağlanma (exponential backoff)."""
        self.disconnect()
        for attempt in range(1, self.max_retries + 1):
            wait = min(2 ** attempt, 10)  # 2, 4, 8... max 10 sn
            logger.warning(f"Yeniden bağlanma denemesi {attempt}/{self.max_retries} ({wait}s bekleniyor)...")
            time.sleep(wait)
            if self.connect():
                logger.info(f"Yeniden bağlantı başarılı (deneme {attempt}).")
                return True
        logger.error("Tüm yeniden bağlanma denemeleri başarısız.")
        return False

    @property
    def is_connected(self):
        return self._connected

    def read_data(self, function_code, start_address, count, _retry=0):
        """
        Modbus verisi oku. Bağlantı koparsa otomatik yeniden bağlan.
        Returns: (values, error_string)
        """
        if not self._connected:
            if not self.connect():
                return None, "Bağlantı kurulamadı."

        try:
            response = None

            # Fonksiyon kodu çözümleme (Daha esnek hale getirildi: "1", "1 (Read Coils)", "Read Coils" hepsi kabul edilir)
            f_code_str = str(function_code).strip()
            
            if f_code_str.startswith("1") or "Read Coils" in f_code_str:
                response = self.client.read_coils(
                    address=start_address, count=count, slave=self.slave_id
                )
                if response and not response.isError():
                    # pymodbus coils/inputs bit listesi döndürür
                    return list(response.bits[:count]), None

            elif f_code_str.startswith("2") or "Discrete Inputs" in f_code_str:
                response = self.client.read_discrete_inputs(
                    address=start_address, count=count, slave=self.slave_id
                )
                if response and not response.isError():
                    return list(response.bits[:count]), None

            elif f_code_str.startswith("3") or "Holding Registers" in f_code_str:
                response = self.client.read_holding_registers(
                    address=start_address, count=count, slave=self.slave_id
                )
                if response and not response.isError():
                    return list(response.registers[:count]), None

            elif f_code_str.startswith("4") or "Input Registers" in f_code_str:
                response = self.client.read_input_registers(
                    address=start_address, count=count, slave=self.slave_id
                )
                if response and not response.isError():
                    return list(response.registers[:count]), None
            else:
                return None, f"Geçersiz fonksiyon kodu: {function_code}"

            # Hata yanıtı döndü - bağlantı sorunlu olabilir
            if response and response.isError():
                logger.warning(f"Modbus hata yanıtı: {response}")
                if _retry < self.max_retries:
                    if self._reconnect():
                        return self.read_data(function_code, start_address, count, _retry=_retry + 1)
                return None, f"Cihaz hata yanıtı: {response}"

            return None, "Bilinmeyen okuma hatası."

        except Exception as e:
            logger.error(f"Okuma hatası: {e}")
            self._connected = False
            if _retry < self.max_retries:
                if self._reconnect():
                    return self.read_data(function_code, start_address, count, _retry=_retry + 1)
            return None, f"Bağlantı Hatası: {str(e)}"

    def write_data(self, function_code, address, value):
        """
        Modbus verisi yaz (Control).
        function_code: "5 (Write Single Coil)" veya "6 (Write Single Register)"
        """
        if not self._connected:
            if not self.connect():
                return False, "Bağlantı kurulamadı."

        try:
            response = None
            if "5" in function_code or "Coil" in function_code:
                # Coil yazma (True/False)
                val = bool(value)
                response = self.client.write_coil(address=address, value=val, slave=self.slave_id)
            elif "6" in function_code or "Register" in function_code:
                # Holding Register yazma (int)
                val = int(value)
                response = self.client.write_register(address=address, value=val, slave=self.slave_id)
            else:
                return False, f"Geçersiz yazma fonksiyon kodu: {function_code}"

            if response and not response.isError():
                logger.info(f"Modbus Yazma Başarılı: Adr {address} -> {value}")
                return True, None
            else:
                return False, f"Cihaz yazma hatası döndürdü: {response}"

        except Exception as e:
            logger.error(f"Yazma hatası: {e}")
            self._connected = False
            return False, f"Bağlantı Hatası: {str(e)}"


# Geriye uyumluluk için eski fonksiyon (tek seferlik bağlantı)
def read_modbus_data(ip, port, slave_id, function_code, start_address, count):
    """Eski API - geriye uyumluluk. Yeni kod ModbusConnection sınıfını kullanmalı."""
    conn = ModbusConnection(ip, port, slave_id)
    try:
        if conn.connect():
            return conn.read_data(function_code, start_address, count)
        else:
            return None, "Hedef makine ile TCP bağlantısı kurulamadı (IP veya Port kontrol edin)."
    finally:
        conn.disconnect()


def group_addresses(tags, max_gap=10):
    """
    Tag listesini fonksiyon koduna, slave id'ye ve adres yakınlığına göre gruplandırır.
    tags: [(address, func_code, slave_id, name), ...]
    Returns: list of groups -> {'f_code', 's_id', 'start_addr', 'count', 'tags': [(addr, name), ...]}
    """
    if not tags:
        return []

    # 1. Gruplandırma (Function Code ve Slave ID'ye göre)
    primary_groups = {}
    for addr, f_code, s_id, name in tags:
        key = (f_code, s_id)
        if key not in primary_groups:
            primary_groups[key] = []
        primary_groups[key].append((addr, name))

    final_groups = []

    # 2. Adres yakınlığına göre alt gruplara böl (Blok okuma için)
    for (f_code, s_id), tag_items in primary_groups.items():
        # Adrese göre sırala
        tag_items.sort(key=lambda x: x[0])
        
        if not tag_items:
            continue

        current_sub_group = [tag_items[0]]
        
        for i in range(1, len(tag_items)):
            prev_addr = tag_items[i-1][0]
            curr_addr = tag_items[i][0]
            
            # Eğer adresler arası fark max_gap'ten küçükse aynı gruba dahil et
            if curr_addr - prev_addr <= max_gap:
                current_sub_group.append(tag_items[i])
            else:
                # Yeni bir alt grup başlat
                start_addr = current_sub_group[0][0]
                count = (current_sub_group[-1][0] - start_addr) + 1
                final_groups.append({
                    'f_code': f_code,
                    's_id': s_id,
                    'start_addr': start_addr,
                    'count': count,
                    'tags': current_sub_group
                })
                current_sub_group = [tag_items[i]]
        
        # Son grubu ekle
        if current_sub_group:
            start_addr = current_sub_group[0][0]
            count = (current_sub_group[-1][0] - start_addr) + 1
            final_groups.append({
                'f_code': f_code,
                's_id': s_id,
                'start_addr': start_addr,
                'count': count,
                'tags': current_sub_group
            })

    return final_groups
