"""Общие настройки client.py, control.py и signaling_server.py."""
from pathlib import Path

# ===== IP И ПОРТ SIGNALING-СЕРВЕРА: ИЗМЕНИТЕ ЗДЕСЬ НА ОБОИХ ПК =====
SIGNALING_IP = "147.45.109.248"
SIGNALING_PORT = 8765
# ================================================================

SERVER_BIND_IP = "0.0.0.0"
BASE_DIR = Path(__file__).resolve().parent
SIGNALING_CERT = BASE_DIR / "signaling_cert.pem"
SIGNALING_KEY = BASE_DIR / "signaling_key.pem"  # Только на сервере!

# STUN помогает найти прямой маршрут. TURN намеренно не используется:
# экран, команды и звук никогда не идут через relay/signaling-сервер.
STUN_URLS = ["stun:stun.l.google.com:19302"]
CONNECT_TIMEOUT = 60
CONSENT_TIMEOUT = 60
VIDEO_FPS = 15
VIDEO_MAX_WIDTH = 1280
MONITOR_INDEX = 1  # Первый монитор MSS; 0 (все мониторы) не поддерживается.
AUDIO_RATE = 48000
AUDIO_SAMPLES = 960  # 20 мс
AUDIO_INPUT_DEVICE = None  # None = системный микрофон по умолчанию
AUDIO_OUTPUT_DEVICE = None  # None = системные динамики по умолчанию
