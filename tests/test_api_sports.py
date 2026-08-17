import http.client
import os
from pathlib import Path

from dotenv import load_dotenv

# Leia ARC projekti juurkaust
ROOT = Path(__file__).resolve().parents[1]

# Lae .env fail
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("API_SPORTS_KEY")

print("Project root:", ROOT)
print(".env exists:", (ROOT / ".env").exists())
print("API key loaded:", API_KEY is not None)

if not API_KEY:
    raise RuntimeError("API_SPORTS_KEY not found in .env")

conn = http.client.HTTPSConnection("v1.basketball.api-sports.io")

headers = {
    "x-apisports-key": API_KEY
}

conn.request("GET", "/status", headers=headers)

response = conn.getresponse()

print("Status:", response.status)

data = response.read().decode("utf-8")

print(data[:1000])