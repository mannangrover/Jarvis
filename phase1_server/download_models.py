"""
Download Vosk model for offline "jarvis" wake word detection.
Run this once before starting wakeword_server.py.

Downloads: vosk-model-small-en-us-0.15  (~50 MB)
"""

import urllib.request
import zipfile
import os
import sys

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_ZIP  = MODEL_NAME + ".zip"
MODEL_URL  = f"https://alphacephei.com/vosk/models/{MODEL_ZIP}"

def download():
    if os.path.exists(MODEL_NAME):
        print(f"[OK] Model already exists: {MODEL_NAME}")
        return

    print(f"Downloading {MODEL_ZIP} (~50 MB) ...")
    print("This only needs to run once.\n")

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
    print("\nYou can now run:  python wakeword_server.py")

if __name__ == "__main__":
    download()
