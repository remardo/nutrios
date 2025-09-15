import os

# Centralized configuration values shared across components.

ADMIN_API_BASE = os.getenv("ADMIN_API_BASE", "http://localhost:8000")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "supersecret")

