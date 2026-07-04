import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- environment-driven config: dev-friendly defaults, override in prod ------
# (docker-compose.yml sets DEBUG=0, a real SECRET_KEY, ALLOWED_HOSTS, REDIS_URL)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",") if h.strip()
]

INSTALLED_APPS = [
    "daphne",                      # serves ASGI under runserver
    "django.contrib.staticfiles",
    "channels",
    "party",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

# No models in v1 — no database needed.
DATABASES = {}

# Set REDIS_URL (e.g. redis://redis:6379/0) to run multi-process: it switches
# both the Channels layer and the room-state store (party/store.py) to Redis.
# Unset = in-memory everything, single process, zero deps — perfect for dev.
REDIS_URL = os.environ.get("REDIS_URL", "")
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }

# TLS terminates at the reverse proxy (Caddy in docker-compose); trust its
# forwarded-proto header so Django knows the request was secure.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Optional TURN relay for WebRTC across restrictive NATs (Phase 3/5). Leave
# unset for STUN-only (works same-LAN / friendly NATs). Example:
#   TURN_URL=turn:turn.example.com:3478 TURN_USERNAME=u TURN_CREDENTIAL=p
TURN_URL = os.environ.get("TURN_URL", "")
TURN_USERNAME = os.environ.get("TURN_USERNAME", "")
TURN_CREDENTIAL = os.environ.get("TURN_CREDENTIAL", "")
