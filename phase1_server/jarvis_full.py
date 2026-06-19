"""
JARVIS Full Pipeline Server
============================
Flow:
  1. ESP32 streams audio continuously
  2. Vosk detects wake word "jarvis"
  3. Server records next 5 seconds (your command)
  4. faster-whisper transcribes speech to text
  5. Groq AI (llama-3.3-70b) generates reply
  6. Reply printed + shown on dashboard

Setup:
    pip install vosk faster-whisper openai python-dotenv soundfile numpy flask
    python jarvis_full.py
"""

import os
import re
import time
import json
import asyncio
import threading
import datetime
import requests as req
import numpy as np
import soundfile as sf
import miniaudio
import edge_tts
from playsound import playsound
from flask import Flask, request, jsonify
from vosk import Model, KaldiRecognizer
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==============================================================
# CONFIG
# ==============================================================
MODEL_PATH      = "vosk-model-en-us-0.22-lgraph"
SAMPLE_RATE     = 16000
RECORD_SECONDS  = 7        # max recording time after "jarvis"
SILENCE_SECONDS = 1.5      # stop early after this much silence
SILENCE_RMS     = 300      # RMS below this = silence
STARTUP_SKIP    = 0.3      # skip only 0.3s — just enough to drop "jarvis" itself
COOLDOWN_S      = 2.0      # min seconds between wake word triggers
GROQ_MODEL      = "llama-3.3-70b-versatile"
TTS_VOICE       = "en-GB-RyanNeural"   # British male — very Jarvis-like
TTS_RATE        = "+5%"
TTS_PITCH       = "-8Hz"
SYSTEM_PROMPT   = (
    "You are Jarvis, a witty and helpful voice assistant. "
    "Keep replies to 1-2 sentences, no markdown, no bullet points. "
    "Respond as if speaking aloud."
)
MEMORY_LIMIT    = 6          # number of past exchanges to remember
WEATHER_CITY    = "Delhi"    # default city for weather queries

# Special phrases → instant hardcoded responses (no AI call needed)
SPECIAL_PHRASES = [
    (["wake up", "daddy"],  "Hello sir. The vibes just got a whole lot better."),
    (["wake up", "home"],   "Hello sir. The vibes just got a whole lot better."),
]

# ESP32 audio — pull model (ESP32 polls for audio)
SPK_RATE       = 16000
pending_audio  = b""       # PCM bytes waiting for ESP32 to pick up
audio_lock     = threading.Lock()
current_eye    = 0          # eye state for ESP32 to poll

# Conversation memory
conversation_history = []   # list of {"role": "user"/"assistant", "content": "..."}
history_lock         = threading.Lock()

# ==============================================================
# STATES
# ==============================================================
IDLE       = "idle"
RECORDING  = "recording"
PROCESSING = "processing"

state           = IDLE
command_buffer  = []
vosk_lock       = threading.Lock()
record_start    = 0.0
silence_start   = 0.0     # when silence began (for VAD stop)
chunk_count     = 0
last_rms        = 0.0
last_peak       = 0
detect_count    = 0
last_detected   = 0.0
last_transcript = ""
last_reply      = ""
is_speaking     = False    # mutes wake word detection while Jarvis talks

# ==============================================================
# LOAD MODELS
# ==============================================================
print()
print("=" * 50)
print("  JARVIS Full Pipeline — Loading models...")
print("=" * 50)

if not os.path.exists(MODEL_PATH):
    print(f"[ERROR] Vosk model missing. Run: python download_models.py")
    exit(1)

print("[1/3] Loading Vosk wake word model...")
vosk_model = Model(MODEL_PATH)
recognizer = KaldiRecognizer(vosk_model, SAMPLE_RATE, '["jarvis", "[unk]"]')
recognizer.SetWords(False)
print("[OK]  Vosk ready | keyword=jarvis")

print("[2/3] Whisper STT -> Groq API (whisper-large-v3, cloud)")
print("[OK]  Whisper ready (cloud)")

print("[3/3] Connecting to Groq AI...")
if not os.getenv("GROQ_API_KEY"):
    print("[ERROR] GROQ_API_KEY not set in .env file!")
    exit(1)
groq = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)
print(f"[OK]  Groq ready | model={GROQ_MODEL}")

print()
print("=" * 50)
print("  JARVIS is READY — say 'Jarvis' to activate")
print("  Dashboard: http://localhost:5000/")
print("=" * 50)
print()

# ==============================================================
# FLASK APP
# ==============================================================
app = Flask(__name__)


# ==============================================================
# HELPERS
# ==============================================================
def transcribe(audio_arr: np.ndarray) -> str:
    # Check if there's enough speech energy — skip if mostly silence
    rms = float(np.sqrt(np.mean(audio_arr.astype(np.float32) ** 2)))
    print(f"[STT]  Audio RMS={rms:.0f}")
    if rms < 1500:
        return ""

    sf.write("command_audio.wav", audio_arr, SAMPLE_RATE, subtype="PCM_16")

    # Use Groq's whisper-large-v3 API — far more accurate than local small model
    with open("command_audio.wav", "rb") as f:
        result = groq.audio.transcriptions.create(
            model="whisper-large-v3",
            file=f,
            language="en",
            response_format="text",
            prompt="Jarvis, what is the weather today? Tell me a joke. Set a timer for five minutes. What time is it?",
        )
    return result.strip() if isinstance(result, str) else result.text.strip()


def get_weather(city: str = WEATHER_CITY) -> str:
    try:
        r = req.get(f"https://wttr.in/{city}?format=3", timeout=5)
        if r.status_code == 200:
            return f"Current weather: {r.text.strip()}"
        return "Sorry, I couldn't fetch the weather right now."
    except Exception:
        return "Sorry, I couldn't reach the weather service."


def handle_timer(text: str) -> str | None:
    """Detect timer requests and start a countdown thread. Returns spoken confirmation or None."""
    match = re.search(r'(\d+)\s*(second|minute|hour|sec|min|hr)s?', text, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit   = match.group(2).lower()
    secs   = amount * (60 if unit.startswith('min') else 3600 if unit.startswith('h') else 1)

    def _ring():
        time.sleep(secs)
        speak(f"Sir, your {amount} {unit} timer is done.")

    threading.Thread(target=_ring, daemon=True).start()
    label = f"{amount} {unit}{'s' if amount > 1 else ''}"
    return f"Timer set for {label}, sir."


def check_builtin_command(text: str) -> str | None:
    """Return instant answer for common commands, or None to fall through to AI."""
    t = text.lower()

    # Time
    if "time" in t and any(w in t for w in ["what", "current", "tell me", "know the"]):
        now = datetime.datetime.now().strftime("%I:%M %p")
        return f"It's {now}, sir."

    # Date
    if any(w in t for w in ["what date", "today's date", "what day", "what's today", "today is"]):
        today = datetime.datetime.now().strftime("%A, %B %d %Y")
        return f"Today is {today}, sir."

    # Weather
    if "weather" in t:
        city_match = re.search(r'weather (?:in|at|for) ([a-zA-Z ]+)', t)
        city = city_match.group(1).strip() if city_match else WEATHER_CITY
        return get_weather(city)

    # Timer
    if "timer" in t or "remind me" in t or "set a" in t:
        result = handle_timer(t)
        if result:
            return result

    return None


def ask_groq(text: str) -> str:
    with history_lock:
        messages = (
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + conversation_history[-MEMORY_LIMIT * 2:]
            + [{"role": "user", "content": text}]
        )
    resp = groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        max_tokens=150,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


async def _tts(text: str):
    comm = edge_tts.Communicate(text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await comm.save("reply.mp3")

def speak(text: str):
    global is_speaking, pending_audio
    is_speaking = True
    set_eye(3)  # speaking
    try:
        asyncio.run(_tts(text))

        decoded = miniaudio.decode_file(
            "reply.mp3",
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=2,
            sample_rate=SPK_RATE,
        )
        pcm_bytes = decoded.samples.tobytes()
        print(f"[TTS]  {len(pcm_bytes)//1024}KB PCM ready for ESP32 to pick up")

        with audio_lock:
            pending_audio = pcm_bytes

        # Wait for ESP32 to pick up the audio before going back to idle
        timeout = 30
        while timeout > 0:
            with audio_lock:
                if len(pending_audio) == 0:
                    break
            time.sleep(0.5)
            timeout -= 1

    except Exception as e:
        print(f"[TTS]  Error: {e}")
    finally:
        time.sleep(0.5)
        is_speaking = False
        set_eye(0)


def process_command(audio_arr: np.ndarray):
    global state, last_transcript, last_reply

    try:
        print("[STT]  Transcribing...")
        transcript = transcribe(audio_arr)
        last_transcript = transcript

        if not transcript or len(transcript.strip()) < 2:
            print("[STT]  No speech detected — try again")
            last_reply = "(no speech detected — speak louder/closer)"
            state = IDLE
            return

        print(f"[STT]  You said: \"{transcript}\"")

        t_lower = transcript.lower()
        reply = None

        # 1. Special hardcoded phrases
        for keywords, response in SPECIAL_PHRASES:
            if all(k in t_lower for k in keywords):
                reply = response
                print("[AI]   Special phrase matched")
                break

        # 2. Built-in commands (time / date / weather / timer)
        if reply is None:
            reply = check_builtin_command(t_lower)
            if reply:
                print("[AI]   Built-in command handled")

        # 3. Groq AI with conversation memory
        if reply is None:
            set_eye(2)  # thinking
            print("[AI]   Asking Groq...")
            reply = ask_groq(transcript)
            with history_lock:
                conversation_history.append({"role": "user",     "content": transcript})
                conversation_history.append({"role": "assistant", "content": reply})

        last_reply = reply
        print(f"[AI]   Jarvis: \"{reply}\"")
        print("[TTS]  Speaking...")
        speak(reply)

    except Exception as e:
        err = str(e)
        print(f"[ERROR] {err}")
        import traceback
        traceback.print_exc()
        last_reply = f"(error: {err})"
    finally:
        with vosk_lock:
            recognizer.Reset()  # clear Vosk internal state so it hears "jarvis" fresh
        print()
        print("  Ready — say 'Jarvis' again to activate")
        print()
        state = IDLE


def _finish_recording():
    global state, command_buffer, silence_start
    state         = PROCESSING
    silence_start = 0.0
    audio_arr     = np.array(command_buffer, dtype=np.int16)
    command_buffer = []
    print(f"[REC]  Done — {len(audio_arr)} samples captured")
    threading.Thread(target=process_command, args=(audio_arr,), daemon=True).start()


def on_wake_word():
    global state, command_buffer, record_start, detect_count, last_detected

    now = time.time()
    if now - last_detected < COOLDOWN_S:
        return
    if state != IDLE:
        return

    last_detected  = now
    detect_count  += 1
    state          = RECORDING
    command_buffer = []
    record_start   = now
    silence_start  = 0.0

    threading.Thread(target=set_eye, args=(1,), daemon=True).start()  # listening

    print()
    print("=" * 45)
    print(f"  *** JARVIS DETECTED *** (#{detect_count})")
    print(f"  Recording command for {RECORD_SECONDS}s... speak now!")
    print("=" * 45)


# ==============================================================
# ENDPOINTS
# ==============================================================
@app.route("/audio", methods=["POST"])
def audio():
    global chunk_count, last_rms, last_peak, command_buffer, state

    try:
        raw_bytes   = request.data
        audio_int16 = np.frombuffer(raw_bytes, dtype=np.int16)

        if len(audio_int16) == 0:
            return "EMPTY", 200

        # Diagnostics
        chunk_count += 1
        peak = int(np.max(np.abs(audio_int16)))
        rms  = float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))
        last_peak = peak
        last_rms  = rms

        if chunk_count % 32 == 0:
            health = "GOOD" if rms > 100 else "LOW" if rms > 10 else "SILENT"
            print(f"[AUDIO] chunk={chunk_count:4d}  rms={rms:6.0f}  {health}  state={state}")

        # ── RECORDING: buffer command audio ───────────────────────────
        if state == RECORDING:
            elapsed = time.time() - record_start

            # Skip the startup window (user still finishing "jarvis")
            if elapsed > STARTUP_SKIP:
                command_buffer.extend(audio_int16.tolist())

                # VAD: detect silence to stop early
                if rms < SILENCE_RMS:
                    if silence_start == 0.0:
                        silence_start = time.time()
                    elif time.time() - silence_start >= SILENCE_SECONDS:
                        # Enough silence — command is done
                        _finish_recording()
                        return "OK", 200
                else:
                    silence_start = 0.0  # reset silence timer on speech

            # Hard timeout
            if elapsed >= RECORD_SECONDS:
                _finish_recording()

            return "OK", 200

        # ── IDLE: run wake word detection (full result only, no partials) ──
        if state == IDLE and not is_speaking:
            with vosk_lock:
                if recognizer.AcceptWaveform(raw_bytes):
                    text = json.loads(recognizer.Result()).get("text", "")
                    if "jarvis" in text:
                        on_wake_word()

        return "OK", 200

    except Exception:
        import traceback
        traceback.print_exc()
        return "ERROR", 500


def set_eye(s: int):
    global current_eye
    current_eye = s


@app.route("/get_audio", methods=["GET"])
def get_audio():
    """ESP32 polls this to pick up TTS audio."""
    global pending_audio
    with audio_lock:
        if len(pending_audio) > 0:
            data = pending_audio
            pending_audio = b""
            return data, 200, {"Content-Type": "application/octet-stream"}
    return "", 204


@app.route("/get_eye", methods=["GET"])
def get_eye():
    return str(current_eye), 200


@app.route("/trigger", methods=["GET", "POST"])
def trigger():
    """ESP32 touch pin fallback — also activates recording."""
    print("\n[TOUCH] Touch trigger from ESP32\n")
    on_wake_word()
    return jsonify({"status": "ok"}), 200


@app.route("/diagnostics", methods=["GET"])
def diagnostics():
    health = "GOOD" if last_rms > 100 else "LOW" if last_rms > 10 else "SILENT"
    return jsonify({
        "status":          "ok",
        "state":           state,
        "chunks":          chunk_count,
        "last_rms":        round(last_rms, 1),
        "last_peak":       last_peak,
        "rms_health":      health,
        "detect_count":    detect_count,
        "last_transcript": last_transcript,
        "last_reply":      last_reply,
    })


@app.route("/", methods=["GET"])
def dashboard():
    health = "GOOD" if last_rms > 100 else "LOW" if last_rms > 10 else "SILENT"
    health_color = "#00ff88" if health == "GOOD" else "#ffaa00" if health == "LOW" else "#ff4444"

    state_color = {
        IDLE:       "#555",
        RECORDING:  "#ff8800",
        PROCESSING: "#00aaff",
    }.get(state, "#555")

    state_label = {
        IDLE:       "Listening...",
        RECORDING:  "Recording command...",
        PROCESSING: "Thinking...",
    }.get(state, state)

    elapsed_bar = ""
    if state == RECORDING:
        pct = min(100, int((time.time() - record_start) / RECORD_SECONDS * 100))
        elapsed_bar = f"""
        <div style="margin:20px 0;background:#111;border-radius:8px;height:12px;width:500px;border:1px solid #333">
          <div style="background:#ff8800;height:100%;width:{pct}%;border-radius:8px;transition:width 0.5s"></div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>JARVIS</title>
  <meta http-equiv="refresh" content="1">
  <style>
    body {{ background:#0a0a0a; color:#eee; font-family:monospace; padding:40px; }}
    h1   {{ color:#00aaff; font-size:2.2em; margin-bottom:8px; }}
    .sub {{ color:#444; margin-bottom:30px; }}
    .row {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:16px; }}
    .card {{ background:#111; border:1px solid #222; border-radius:10px;
             padding:20px 28px; min-width:180px; }}
    .label {{ color:#666; font-size:0.8em; text-transform:uppercase; letter-spacing:1px; }}
    .value {{ font-size:2em; font-weight:bold; margin-top:6px; }}
    .speech {{ background:#0a0a1a; border:1px solid #1a1a3a; border-radius:10px;
               padding:20px 28px; margin-top:16px; max-width:700px; }}
    .speech .label {{ color:#4466aa; }}
    .speech .text {{ font-size:1.2em; margin-top:8px; color:#ccddff; }}
    .reply {{ background:#001a0a; border:1px solid #003a1a; }}
    .reply .label {{ color:#00aa55; }}
    .reply .text {{ color:#aaffcc; }}
  </style>
</head>
<body>
  <h1>JARVIS</h1>
  <div class="sub">Wake word + Voice Assistant Pipeline</div>

  <div class="row">
    <div class="card">
      <div class="label">State</div>
      <div class="value" style="color:{state_color}">{state_label}</div>
    </div>
    <div class="card">
      <div class="label">Audio Health</div>
      <div class="value" style="color:{health_color}">{health}</div>
    </div>
    <div class="card">
      <div class="label">Chunks</div>
      <div class="value" style="color:#00aaff">{chunk_count}</div>
    </div>
    <div class="card">
      <div class="label">Activations</div>
      <div class="value" style="color:#ffaa00">#{detect_count}</div>
    </div>
  </div>

  {elapsed_bar}

  <div class="speech">
    <div class="label">You said</div>
    <div class="text">{last_transcript or "— waiting for command —"}</div>
  </div>

  <div class="speech reply">
    <div class="label">Jarvis replied</div>
    <div class="text">{last_reply or "— no reply yet —"}</div>
  </div>

  <p style="color:#333;margin-top:24px;font-size:0.75em">Auto-refreshes every second</p>
</body>
</html>"""
    return html


# ==============================================================
# MAIN
# ==============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
