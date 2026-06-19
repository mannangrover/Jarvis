"""
Download Vosk model for wake word detection.
Downloads vosk-model-en-us-0.22-lgraph (~200 MB) — runs automatically on first deploy.
"""

import urllib.request
import zipfile
import os
import sys

MODEL_NAME = "vosk-model-en-us-0.22-lgraph"
MODEL_ZIP  = MODEL_NAME + ".zip"
MODEL_URL  = f"https://alphacephei.com/vosk/models/{MODEL_ZIP}"

def download():
    if os.path.exists(MODEL_NAME):
        print(f"[OK] Model already exists: {MODEL_NAME}")
        return

    print(f"Downloading {MODEL_ZIP} (~200 MB) ...")

    def progress(count, block_size, total_size):
        if total_size > 0:
            pct = int(count * block_size * 100 / total_size)
            mb  = count * block_size / 1_000_000
            sys.stdout.write(f"\r  {pct}%  {mb:.1f} MB downloaded")
            sys.stdout.flush()

    urllib.request.urlretrieve(MODEL_URL, MODEL_ZIP, reporthook=progress)
    print(f"\n[OK] Downloaded {MODEL_ZIP}")

    print("[..] Extracting ...")
    with zipfile.ZipFile(MODEL_ZIP, "r") as z:
        z.extractall(".")
    os.remove(MODEL_ZIP)

    print(f"[OK] Model ready: {MODEL_NAME}/")

if __name__ == "__main__":
    download()
