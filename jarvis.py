# ================================================================
#   J.A.R.V.I.S — Voice backend
#   Wake word -> voice command -> Claude tool-calling brain (with a
#   local Ollama fallback) -> spoken reply
# ================================================================
import sys
import io

# Force UTF-8 for stdout/stderr — without this, Windows defaults to
# cp1252 when output is piped (e.g. by Electron), and any special
# character in a print() statement crashes the whole thread.
try:
    if sys.stdout is not None and sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    if sys.stderr is not None and sys.stderr.encoding != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
# pythonw.exe has no console, so stdout/stderr can be None — guard against that.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import os, re, json, time, queue, random, asyncio, webbrowser, socket, signal
import tempfile, threading, zipfile, urllib.request, shutil, subprocess
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ -- must happen before sms.py/email_watcher.py
                # read their TWILIO_*/GMAIL_*/USER_PHONE_NUMBER config at import time

import sys as _sys
import pyaudio, requests

IS_WINDOWS = _sys.platform == "win32"

if IS_WINDOWS:
    import win32com.client

try:
    import edge_tts; EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    import pygame; PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

try:
    import websockets; WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

try:
    import plaid_service; PLAID_AVAILABLE = True
except ImportError:
    PLAID_AVAILABLE = False

import tools
import brain
import sms
import telegram_bridge
import jarvis_admin
import jarvis_cpu_alerts
import jarvis_improvement
import jarvis_security
import jarvis_email_bot
import jarvis_outlook_bot
import bot_events
import employees
import agent_registry
import dashboard_server
import telegram_common
import email_watcher

try:
    import browser_control
except ImportError:
    browser_control = None


# ================================================================ SETTINGS
OLLAMA_MODEL, OLLAMA_URL = "llama3.2", "http://localhost:11434/api/generate"
WAKE_WORD, SPEECH_RATE, WAKE_COOLDOWN = "jarvis", 2, 2.0
SAMPLE_RATE, CHUNK_SIZE = 16000, 8000
CTX_TIMEOUT, WS_PORT, INLINE_WAIT_SECS = 15.0, 8765, 1.5

JARVIS_VOICE, JARVIS_VOICE_RATE, JARVIS_VOICE_VOL = "en-GB-RyanNeural", "+8%", "+10%"

_WAKE_PHRASES = ["jarvis", "hey jarvis", "hello jarvis", "hi jarvis", "ok jarvis", "okay jarvis"]
_WAKE_RESPONSES = ["Yes sir.", "Sir.", "Yes.", "Standing by sir.", "Of course sir."]
_INLINE_CONFIRMS = ["On it sir.", "Right away sir.", "Of course sir.", "Certainly sir.", "Understood sir."]

VOSK_MODEL_NAME = "vosk-model-en-us-0.22-lgraph"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"


# ================================================================ TRANSCRIPT CLEANUP
_TRANSCRIPT_FIXES = {
    r'\bjarvi\b': 'jarvis', r'\bjarved\b': 'jarvis', r'\bcharvis\b': 'jarvis',
    r'\bharvis\b': 'jarvis', r'\bgervis\b': 'jarvis', r'\bjervis\b': 'jarvis',
    r'\bum\b': '', r'\buh\b': '', r'\ber\b': '',
    # Vosk transcribes spoken URLs as words ("youtube dot com") -- collapse
    # them back into real domains so "go to youtube.com" actually resolves.
    r'\s+dot\s+com\b': '.com', r'\s+dot\s+org\b': '.org', r'\s+dot\s+net\b': '.net',
    r'\s+dot\s+io\b': '.io', r'\s+dot\s+gov\b': '.gov', r'\s+dot\s+edu\b': '.edu',
    r'\s+dot\s+co\b': '.co', r'\s+dot\s+tv\b': '.tv',
}
_FILLER_WORDS = {
    "the", "a", "an", "and", "or", "but", "so", "yes", "no", "um", "uh", "er", "ah", "oh",
    "hey", "hi", "ok", "okay", "please", "like", "just", "now", "then", "there", "here",
}


def _fix_transcript(text):
    result = text.lower().strip()
    for pattern, replacement in _TRANSCRIPT_FIXES.items():
        result = re.sub(pattern, replacement, result)
    return re.sub(r'\s{2,}', ' ', result).strip()


def _contains_wake(text):
    return any(phrase in text for phrase in _WAKE_PHRASES)


def _strip_wake(text):
    for phrase in sorted(_WAKE_PHRASES, key=len, reverse=True):
        if phrase in text:
            idx = text.index(phrase)
            return text[idx + len(phrase):].strip()
    return ""


def _is_meaningful(text):
    words = text.strip().split()
    if len(words) == 0:
        return False
    if len(words) == 1:
        return words[0] not in _FILLER_WORDS and len(words[0]) >= 4
    return True


def _clean_command(text):
    words = text.strip().split()
    while words and words[0] in _FILLER_WORDS:
        words.pop(0)
    while words and words[-1] in _FILLER_WORDS:
        words.pop()
    return " ".join(words)


def ensure_vosk_model():
    if os.path.exists(VOSK_MODEL_NAME):
        return
    print("\n  Downloading Vosk speech model (~128MB), please wait.")
    urllib.request.urlretrieve(VOSK_MODEL_URL, "_vosk_model.zip")
    with zipfile.ZipFile("_vosk_model.zip") as z:
        z.extractall(".")
    os.remove("_vosk_model.zip")
    print("  Speech model ready.\n")


# ================================================================ TTS
# Primary voice is always edge-tts (below) -- these are just the last-resort,
# no-internet fallback, and differ by OS: Windows uses the built-in SAPI
# voice, Linux uses espeak/espeak-ng if installed.
_sapi_speaker = None


def _speak_sapi_fallback(text):
    if _sapi_speaker is None:
        print(f"  [TTS skipped] {text}")
        return True
    interrupt_flag.clear()
    chunks = [c.strip() for c in re.split(r'(?<=[.!?])\s+', text) if c.strip()]
    SAPI_SYNC, SAPI_ASYNC, SAPI_PURGE = 0, 1, 2
    for chunk in chunks:
        if interrupt_flag.is_set():
            _sapi_speaker.Speak("", SAPI_ASYNC | SAPI_PURGE)
            return False
        _sapi_speaker.Speak(chunk, SAPI_SYNC)
        if interrupt_flag.is_set():
            _sapi_speaker.Speak("", SAPI_ASYNC | SAPI_PURGE)
            return False
    return True


def _speak_linux_fallback(text):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        print(f"  [TTS skipped] {text}")
        return True
    interrupt_flag.clear()
    chunks = [c.strip() for c in re.split(r'(?<=[.!?])\s+', text) if c.strip()]
    for chunk in chunks:
        if interrupt_flag.is_set():
            return False
        proc = subprocess.Popen([exe, chunk], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            if interrupt_flag.is_set():
                proc.terminate()
                return False
            time.sleep(0.05)
    return True


def _speak_local_fallback(text):
    return _speak_sapi_fallback(text) if IS_WINDOWS else _speak_linux_fallback(text)


async def _edge_tts_save(text, path):
    communicate = edge_tts.Communicate(text, JARVIS_VOICE, rate=JARVIS_VOICE_RATE, volume=JARVIS_VOICE_VOL)
    await communicate.save(path)


def _prep_tts_text(text):
    result = text.replace(", ", " ").replace(",", " ")
    return re.sub(r'  +', ' ', result).strip()


def _ensure_mixer():
    """(Re)initialize the pygame mixer if it isn't currently running. Needed
    because the mixer binds to whatever the default output device was at
    init time -- if headphones/speakers get unplugged or switched afterward,
    playback can start silently failing or erroring against a dead handle.
    Call this before every playback attempt so a fresh device switch always
    gets picked up rather than wedging audio output permanently."""
    if not PYGAME_AVAILABLE:
        return False
    try:
        if pygame.mixer.get_init() is not None:
            return True
        pygame.mixer.pre_init(44100, -16, 2, 512)
        pygame.mixer.init()
        return True
    except Exception as e:
        print(f"  [Audio Error] Couldn't init mixer: {e}")
        return False


def speak(text):
    if not text:
        return True
    interrupt_flag.clear()
    print(f"\n  [Jarvis]  {text}\n")
    if not _speaking_enabled.is_set():
        # Voice output is off (stop speaking / text mode only) -- still log
        # the reply, just skip TTS. Text-channel delivery (Telegram/SMS)
        # happens separately via handle_command's notify(), unaffected.
        return True
    _ws_broadcast({"type": "speaking", "value": True, "text": text})
    completed = True
    tts_text = _prep_tts_text(text)

    if EDGE_TTS_AVAILABLE and PYGAME_AVAILABLE and _ensure_mixer():
        tmp = os.path.join(tempfile.gettempdir(), f"jarvis_{threading.get_ident()}.mp3")
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_edge_tts_save(tts_text, tmp))
            loop.close()
            pygame.mixer.music.load(tmp)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if interrupt_flag.is_set():
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
                    try: os.unlink(tmp)
                    except: pass
                    completed = False
                    break
                time.sleep(0.025)
            pygame.mixer.music.unload()
            try: os.unlink(tmp)
            except: pass
        except Exception as e:
            print(f"  [EdgeTTS error] {e}")
            # The output device may have changed underneath the mixer (e.g.
            # headphones unplugged mid-speech) -- tear it down so the next
            # speak() call rebuilds it against whatever is now the default
            # device, instead of repeatedly failing against a dead handle.
            try: pygame.mixer.quit()
            except Exception: pass
            completed = _speak_local_fallback(tts_text)
    else:
        completed = _speak_local_fallback(tts_text)

    _ws_broadcast({"type": "speaking", "value": False})
    return completed


# ================================================================ SHARED STATE
audio_queue, text_queue = queue.Queue(), queue.Queue()
interrupt_flag, pipeline_stop = threading.Event(), threading.Event()
_last_wake_ts = 0.0

# Voice input/output toggles -- set via the "stop/start listening",
# "stop/start speaking", and "text mode only" commands (any channel: voice,
# typed, SMS, Telegram). Both default on. When listening is off, the
# recognition thread still drains the mic's audio queue (so the input
# stream itself never blocks/leaks) but never runs it through Vosk, so no
# speech is ever transcribed while "asleep". When speaking is off, speak()
# still prints/logs the reply but skips TTS playback entirely -- replies
# keep flowing over whichever text channel (Telegram/SMS/HUD) triggered
# them, so "text mode only" (both off) is a full switch to text-only.
_listening_enabled, _speaking_enabled = threading.Event(), threading.Event()
_listening_enabled.set()
_speaking_enabled.set()

# Voice, typed, and SMS commands all end up calling handle_command(), which
# touches shared TTS/interrupt state (speak(), interrupt_flag). This keeps
# them from ever running concurrently and corrupting each other.
_command_lock = threading.Lock()


def set_status(text):
    _ws_broadcast({"type": "status", "value": text})


def trigger_wake():
    global _last_wake_ts
    now = time.time()
    if now - _last_wake_ts >= WAKE_COOLDOWN:
        _last_wake_ts = now
        interrupt_flag.set()
        return True
    return False


# ================================================================ WEBSOCKET SERVER
_ws_clients = set()
_ws_loop = None


def _ws_broadcast(data):
    if not WS_AVAILABLE or not _ws_clients or _ws_loop is None:
        return
    asyncio.run_coroutine_threadsafe(_ws_broadcast_async(json.dumps(data)), _ws_loop)


async def _ws_broadcast_async(msg):
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def _on_camera_frame(image_b64):
    """Registered with dashboard_server.register_frame_handler at startup
    -- see that module's comment for why this is a direct call rather than
    a bot_events publish. `image_b64` is None when the phone's camera just
    closed (clears the desktop's feed instead of leaving a frozen frame)."""
    _ws_broadcast({"type": "camera_frame", "image": image_b64})


def _on_hand_gesture(x, y, pinching, active):
    """Registered with dashboard_server.register_gesture_handler at
    startup -- same direct-call reasoning as _on_camera_frame above."""
    _ws_broadcast({"type": "hand_gesture", "x": x, "y": y, "pinching": pinching, "active": active})


def _on_bot_event(event):
    """Bridges bot_events (the shared bus dashboard_server.py, the
    watchdogs, and employees publish to) into the desktop HUD's own
    WebSocket feed -- the phone HUD's camera Q&A (`vision_qa`) and the
    voice-triggered map tools (`show_map`/`show_weather_radar` in
    tools.py), so a voice command actually updates the desktop UI instead
    of only being spoken back. One Jarvis, not separate surfaces that
    don't know what each other did. Never raises -- bot_events.publish
    already isolates subscriber exceptions, but this stays defensive
    since it runs on that shared dispatch path."""
    etype = event.get("type")
    payload = event.get("payload") or {}

    if etype == "vision_qa":
        question, answer = payload.get("question", ""), payload.get("answer", "")
        if not answer:
            return
        _ws_broadcast({"type": "vision_qa", "question": question, "answer": answer})
        _ws_broadcast({"type": "activity", "text": f'Phone camera asked "{question}" -> {answer}'})

    elif etype == "show_map":
        _ws_broadcast({"type": "show_map", **payload})

    elif etype == "show_weather_radar":
        _ws_broadcast({"type": "show_weather_radar", **payload})


async def _ws_handler(websocket):
    global _startup_spoken
    _ws_clients.add(websocket)
    print(f"  [WS] HUD connected ({len(_ws_clients)} client)")
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                t = msg.get("type", "")

                if t == "command":
                    text_queue.put(msg.get("text", ""))

                elif t == "hud_ready":
                    def _on_ready():
                        global _startup_spoken
                        if not _startup_spoken:
                            _startup_spoken = True
                            time.sleep(0.5)
                            speak("Systems online sir. Say Jarvis to begin.")
                    threading.Thread(target=_on_ready, daemon=True).start()

                elif t == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

            except Exception as e:
                print(f"  [WS] Message error: {e}")
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)
        print(f"  [WS] HUD disconnected ({len(_ws_clients)} client)")


_startup_spoken = False


def _ws_server_thread():
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)

    async def _run():
        async with websockets.serve(_ws_handler, "localhost", WS_PORT):
            print(f"  [WS] WebSocket server -> ws://localhost:{WS_PORT}")
            await asyncio.Future()

    _ws_loop.run_until_complete(_run())


def _already_running():
    """Jarvis now runs as a persistent background process (see the
    JarvisBackend scheduled task), independent of whether the HUD window is
    open. The Electron app still tries to spawn jarvis.py itself as a
    dev-mode convenience -- this makes that harmless instead of a port
    conflict or, worse, a second instance double-polling Telegram/SMS and
    replying to every text twice.

    Sets SO_REUSEADDR before binding, matching what asyncio's own server
    (which start_ws_server actually uses, via websockets.serve) already
    does by default on POSIX -- without it, this check is *stricter* than
    the real server: it can report "already running" (EADDRINUSE from a
    just-closed socket still in TIME_WAIT) in a case where the real
    websockets.serve() call would have bound just fine. Confirmed live:
    even with SO_REUSEADDR, a `systemctl restart` can still start the new
    process in the same instant the kernel is still tearing down the old
    one's socket, so this also retries a few times before concluding
    "already running" -- without both fixes, this exited immediately after
    a normal restart, leaving nothing running at all."""
    for attempt in range(5):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("localhost", WS_PORT))
            return False
        except OSError:
            if attempt < 4:
                time.sleep(0.5)
        finally:
            s.close()
    return True


def start_ws_server():
    if not WS_AVAILABLE:
        print("  [WS] 'websockets' package not installed — HUD will not connect.")
        return
    threading.Thread(target=_ws_server_thread, daemon=True, name="WebSocket").start()


# ================================================================ MIC / VOSK THREADS
def mic_thread():
    # Runs as a daemon thread with no supervisor, so an unhandled exception
    # here would silently kill wake-word listening for the rest of the
    # session (e.g. a headset mic being unplugged/switched mid-stream raises
    # OSError from stream.read()). Instead, treat device errors as
    # recoverable: tear down and reopen against whatever the default input
    # device now is, and keep retrying until pipeline_stop is set.
    while not pipeline_stop.is_set():
        p, stream = None, None
        try:
            p = pyaudio.PyAudio()
            stream = p.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                             input=True, frames_per_buffer=CHUNK_SIZE)
            while not pipeline_stop.is_set():
                audio_queue.put(stream.read(CHUNK_SIZE, exception_on_overflow=False))
        except Exception as e:
            if not pipeline_stop.is_set():
                print(f"  [Mic Error] {e} -- reopening microphone")
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if p is not None:
                    p.terminate()
            except Exception:
                pass
        if not pipeline_stop.is_set():
            time.sleep(1)


def recognition_thread():
    from vosk import Model, KaldiRecognizer
    model = Model(VOSK_MODEL_NAME)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    partial_wake_fired = False  # prevents re-firing on the same growing utterance

    while not pipeline_stop.is_set():
        try:
            data = audio_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if not _listening_enabled.is_set():
            # "stop listening" / "text mode only" -- drain the mic queue so
            # the input stream never backs up, but never run it through
            # Vosk, so nothing said while off is ever transcribed.
            continue

        if rec.AcceptWaveform(data):
            partial_wake_fired = False
            raw = json.loads(rec.Result()).get("text", "").strip()
            if not raw:
                continue
            text = _fix_transcript(raw)
            print(f"  [Heard] \"{text}\"")
            text_queue.put(text)
            _ws_broadcast({"type": "transcript", "text": text})
            if _contains_wake(text):
                if trigger_wake():
                    _ws_broadcast({"type": "wake_word"})
        else:
            raw_partial = json.loads(rec.PartialResult()).get("partial", "").strip()
            if len(raw_partial) >= 6 and " " in raw_partial:
                partial = _fix_transcript(raw_partial)
                if not partial_wake_fired and re.search(r'(^|\s)jarvis(\s|$)', partial) and trigger_wake():
                    print(f"  [Interrupt!] \"{raw_partial}\"")
                    partial_wake_fired = True
                    text_queue.put(WAKE_WORD)
                    _ws_broadcast({"type": "wake_word"})


# ================================================================ QUEUE HELPERS
def clear_text_queue():
    while not text_queue.empty():
        try: text_queue.get_nowait()
        except queue.Empty: break


def wait_for_wake_word(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = text_queue.get(timeout=1.0)
            if not _contains_wake(text):
                continue
            inline = _clean_command(_strip_wake(text))
            if inline and _is_meaningful(inline):
                return True, inline
            patience = time.time() + INLINE_WAIT_SECS
            while time.time() < patience:
                try:
                    candidate = text_queue.get(timeout=0.15)
                    remainder = _clean_command(_strip_wake(candidate))
                    if remainder and _is_meaningful(remainder):
                        return True, remainder
                    elif candidate.strip() and not _contains_wake(candidate):
                        cleaned = _clean_command(candidate.strip())
                        if _is_meaningful(cleaned):
                            return True, cleaned
                except queue.Empty:
                    pass
            return True, None
        except queue.Empty:
            pass
    return False, None


def get_command(timeout=7, clear=True, silence_gap=2.0):
    # Vosk finalizes an utterance on any brief pause (a thinking pause,
    # a breath). Returning on the first fragment cuts the user off
    # mid-sentence, so instead we keep accumulating fragments and only
    # finalize once there's been a real silence_gap of quiet.
    if clear:
        clear_text_queue()
    interrupt_flag.clear()
    deadline = time.time() + timeout
    collected = ""
    while time.time() < deadline:
        try:
            text = text_queue.get(timeout=1.0)
            piece = _strip_wake(text) or text.strip()
            if piece:
                collected = f"{collected} {piece}".strip() if collected else piece
                deadline = time.time() + silence_gap
        except queue.Empty:
            if collected:
                return collected
    return collected or None


# ================================================================ OLLAMA (LOCAL AI FALLBACK)
# Used only when the Claude brain is unreachable (offline, not authenticated,
# claude.exe missing). No tool access in this path -- chat only.
_OLLAMA_SYSTEM = (
    "You are J.A.R.V.I.S, a voice assistant currently running in offline "
    "fallback mode with no internet access and no tools. You only know what "
    "the user directly tells you in this message. Never invent facts, "
    "events, numbers, times, or scenarios that were not stated by the user. "
    "If asked to do something that needs a real tool, say plainly you're "
    "offline and can't reach your full capabilities right now. Speak with "
    "calm confidence and quiet wit. Address the user as sir occasionally but "
    "not every sentence. Keep answers under 40 words. Short natural "
    "sentences. Never use bullet points or markdown. Never say your own name."
)


def ask_ollama(question):
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": f"{_OLLAMA_SYSTEM}\n\nUser: {question}\nJarvis:",
            "stream": False,
        }, timeout=30)
        if r.status_code == 200:
            return r.json().get("response", "I couldn't get a response.")
        return "Ollama returned an error."
    except requests.ConnectionError:
        return ("My offline Ollama fallback isn't reachable either sir. That's on top "
                "of Claude having failed for this one command -- I'll retry my main "
                "brain fresh on your next request, so voice commands should keep working.")
    except requests.Timeout:
        return ("My offline fallback timed out sir. I'll still retry my main Claude "
                "brain fresh on your next command.")
    except Exception as e:
        return (f"Something went wrong with my offline fallback: {e} My main Claude "
                 "brain will still be retried on your next command sir.")


# ================================================================ BASIC (instant, zero-latency) COMMANDS
# Everything that doesn't need real reasoning stays here for a snappy,
# no-round-trip reply. Everything else goes to the Claude brain below.
_TIME_TRIGGERS = ["what time is it", "what's the time", "whats the time", "tell me the time", "current time"]
_DATE_TRIGGERS = ["what day is it", "what's the date", "whats the date", "what is the date", "today's date", "todays date"]
_GREETING_TRIGGERS = ["good morning", "good afternoon", "good evening", "how are you", "hello", "hey", "what's up", "whats up"]
_GREETING_RESPONSES = [
    "Good {tod} sir. All systems running smoothly.",
    "Operating at full capacity sir. How may I assist?",
    "Never better sir. What can I do for you today?",
    "Fully operational sir. What do you need?",
]
_EXIT_TRIGGERS = ["exit", "quit", "bye", "goodbye", "shutdown", "shut down"]

# Exact-phrase instant media control (system-wide media keys) -- kept as a
# fast path so pausing/skipping never waits on a brain round-trip. Fuzzier
# phrasing ("could you pause that") still works, just via the brain's
# media_control tool instead.
_MEDIA_FAST_PHRASES = {
    "pause": "play_pause", "pause video": "play_pause", "pause the video": "play_pause", "pause it": "play_pause",
    "resume": "play_pause", "resume video": "play_pause", "unpause": "play_pause",
    "play video": "play_pause", "play the video": "play_pause", "play it": "play_pause",
    "skip": "next", "skip video": "next", "skip this": "next", "next video": "next", "next": "next",
    "previous video": "previous", "previous": "previous", "go back": "previous", "last video": "previous",
    "volume up": "volume_up", "turn it up": "volume_up", "louder": "volume_up",
    "volume down": "volume_down", "turn it down": "volume_down", "quieter": "volume_down",
    "mute": "mute", "mute it": "mute",
}


def _time_of_day():
    hour = int(time.strftime("%H"))
    return "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"


# ================================================================ BRAIN CALLBACKS
def _on_brain_activity(text):
    set_status(text)
    _ws_broadcast({"type": "activity", "text": text})


# The currently in-flight brain subprocess (if any), tracked so "cancel job"
# can interrupt a long-running command (self_improve/hire_employee can take
# up to 10 minutes) without waiting for it to finish on its own. Guarded by
# its own lock, separate from _command_lock, on purpose: the thread running
# the job holds _command_lock for the job's whole duration, so a cancel has
# to reach in from outside that lock rather than queue behind it.
_active_job_lock = threading.Lock()
_active_job = None  # {"proc": Popen, "cancelled": bool} or None
_JOB_CANCEL_PHRASES = (
    "cancel job", "cancel the job", "cancel that job",
    "stop job", "stop the job", "stop that job",
    "never mind the job", "never mind that job",
)


def _register_active_job(proc):
    global _active_job
    record = {"proc": proc, "cancelled": False}
    with _active_job_lock:
        _active_job = record
    return record


def _clear_active_job(record):
    global _active_job
    with _active_job_lock:
        if _active_job is record:
            _active_job = None


def _cancel_active_job():
    """Best-effort kill of the active job's whole process tree. Returns True
    if there was a job running to cancel, False if there was nothing to do."""
    with _active_job_lock:
        record = _active_job
    if not record or record["proc"].poll() is not None:
        return False
    record["cancelled"] = True
    proc = record["proc"]
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if IS_WINDOWS:
                # CTRL_BREAK_EVENT didn't finish the job in time -- taskkill
                # /T reaches the whole process tree, not just proc itself,
                # same reason killpg is used on the POSIX side above.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                capture_output=True, timeout=10)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    except Exception as e:
        print(f"  [Cancel Job] {e}")
    return True


def _on_brain_creation(payload):
    path = payload.get("path")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception as e:
        print(f"  [Creation] couldn't read {path}: {e}")
        return
    kind = payload.get("kind", "dashboard")
    title = payload.get("title", "") or "Your creation"
    # Works out the phone/tailnet link, persists this creation to the
    # retrievable log, and sends the Telegram notice (with or without a
    # link) -- see tools.notify_creation_ready's docstring for why this has
    # to happen here (jarvis.py's own process) rather than inside
    # build_creation() itself.
    lan_url = tools.notify_creation_ready(payload)
    _ws_broadcast({
        "type": "creation_ready",
        "title": title,
        "kind": kind,
        "html": html,
        "url": lan_url,
    })
    if lan_url:
        print(f"  [Creation] Also reachable on your phone/tailnet -> {lan_url}")
    else:
        print("  [Creation] No phone/tailnet link available (dashboard server isn't running).")
    # Both kinds pop straight into Brave, front-and-center, the moment
    # they're ready -- like a Claude artifact appearing -- in addition to
    # the in-HUD panel above; a dashboard still ALSO shows in the HUD panel
    # since that's where a HUD-styled widget belongs, it just no longer
    # requires the user to notice and click "open in browser" manually.
    _open_in_browser("file:///" + path.replace("\\", "/"))


_BRAVE_BIN = shutil.which("brave") or shutil.which("brave-browser")


def _open_in_browser(url):
    """Open a built website the way Claude Code opens an artifact: pop it
    straight open, prominently, in the user's actual everyday browser --
    Brave specifically if it's installed, since that's what's asked for --
    rather than just quietly saving a file. Falls back to webbrowser.open()
    (whatever the OS default association is) if Brave isn't found, so this
    never silently does nothing."""
    if _BRAVE_BIN:
        try:
            subprocess.Popen([_BRAVE_BIN, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
            return
        except Exception as e:
            print(f"  [Creation] Couldn't launch Brave, falling back: {e}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _notify_all(text):
    """Best-effort push to every configured text channel (SMS, Telegram) --
    used for things Jarvis says unprompted (reminders, important email),
    not for replying to a specific inbound command (which replies on
    whichever channel it came from instead)."""
    sms.send_sms(text)
    telegram_bridge.send_message(text)


def _reminder_watcher_thread():
    while not pipeline_stop.is_set():
        try:
            for r in tools.due_reminders():
                text = f"Reminder sir: {r['text']}"
                with _command_lock:
                    speak(text)
                _notify_all(text)
        except Exception as e:
            print(f"  [Reminders] {e}")
        time.sleep(20)


_HEALTH_CHECK_INTERVAL_SECONDS = 2 * 60 * 60


def _health_watcher_thread():
    """Runs check_system_health() immediately, then every 2 hours for as
    long as Jarvis's backend is running -- overheating risk and low-disk
    cleanup, logged to ~/.jarvis/health_log.jsonl every time. Only speaks up
    if something's actually critical, so it stays quiet in the normal case.

    The JarvisCPU_Alerts sub-agent (jarvis_cpu_alerts.py) rides along on the
    same cycle: it gets a summary after every check, and a critical issue
    (overheating risk or critically low disk space) triggers an immediate
    Telegram alert from it, same as the voice/SMS/main-Telegram alert below."""
    while not pipeline_stop.is_set():
        try:
            result = tools.check_system_health()
            print(f"  [Health] {result}")
        except Exception as e:
            print(f"  [Health] {e}")
            result = ""
        critical = tools.LAST_HEALTH_RESULT.get("critical", False)
        if critical:
            text = f"Health check sir: {result}"
            with _command_lock:
                speak(text)
            _notify_all(text)
            jarvis_cpu_alerts.send_critical_alert(result)
            bot_events.publish("health_critical", {"result": result})
        if result:
            jarvis_cpu_alerts.send_summary(result)
        if pipeline_stop.wait(_HEALTH_CHECK_INTERVAL_SECONDS):
            break


_FINANCE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60


def _finance_watcher_thread():
    """Runs tools._finance_watcher_check() immediately, then once a day for
    as long as Jarvis's backend is running -- bank data doesn't change fast
    enough to justify the 2-hour cadence _health_watcher_thread uses. Only
    speaks up for anomalies that haven't already been flagged (see
    statements_service.check_spending_anomalies's dedup), so this stays
    quiet on every day nothing's actually notable. No dedicated Telegram
    bot for this -- reuses _notify_all like _reminder_watcher_thread and
    _on_important_email already do, since a proactive finance channel
    doesn't need its own isolated bot the way PC-health/security do."""
    while not pipeline_stop.is_set():
        try:
            anomalies = tools._finance_watcher_check()
        except Exception as e:
            print(f"  [FinanceWatch] {e}")
            anomalies = []
        for a in anomalies:
            text = f"Heads up on your spending sir: {a['detail']}"
            with _command_lock:
                speak(text)
            _notify_all(text)
            bot_events.publish("finance_anomaly", a)
        if pipeline_stop.wait(_FINANCE_CHECK_INTERVAL_SECONDS):
            break


_IMPROVEMENT_START_HOUR = 6
_IMPROVEMENT_START_MINUTE = 0


def _improvement_watcher_thread():
    """JarvisImprovement sub-agent: once every 24 hours, starting at 6 AM
    local time, runs tools.run_daily_self_improve() -- the same
    self_improve.md-driven mechanism (and hard constraints/build-verify
    gate) as the on-demand "Jarvis, improve on..." command, but unfocused
    and capped at up to 2 hours -- then reports whatever it safely added
    over Telegram via the dedicated JarvisImprovement bot. Runs silently:
    no voice/_notify_all output during the pass itself, only the
    end-of-run Telegram summary, so it never interrupts anything."""
    while not pipeline_stop.is_set():
        now = datetime.now()
        target = now.replace(hour=_IMPROVEMENT_START_HOUR, minute=_IMPROVEMENT_START_MINUTE,
                              second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if pipeline_stop.wait((target - now).total_seconds()):
            break
        try:
            result = tools.run_daily_self_improve()
        except Exception as e:
            print(f"  [JarvisImprovement] {e}")
            continue
        print(f"  [JarvisImprovement] {result}")
        if result.get("ok"):
            jarvis_improvement.send_report(result.get("added", []), result.get("timed_out", False))


_BRIEFING_HOUR = 7
_BRIEFING_MINUTE = 0


def _daily_briefing_thread():
    """Once every 24 hours, starting at 7 AM local time (same fixed-hour
    scheduling pattern as _improvement_watcher_thread above) -- synthesizes
    ONE cohesive morning briefing across everything Jarvis tracks (PC
    health, security, finances, the hired employee team, and long-term
    memory: tools._daily_briefing_content()) via the real Claude CLI
    (telegram_common.ask_grounded, same "real Claude CLI, Ollama fallback"
    path every other proactive alert in this project already uses), rather
    than the four separate watchdog messages the user would otherwise get
    piecemeal across the morning. Delivered the same way as every other
    proactive alert: spoken + _notify_all, plus a bot_events publish so it
    shows up in the phone dashboard's live feed too."""
    while not pipeline_stop.is_set():
        now = datetime.now()
        target = now.replace(hour=_BRIEFING_HOUR, minute=_BRIEFING_MINUTE, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if pipeline_stop.wait((target - now).total_seconds()):
            break
        try:
            content = tools._daily_briefing_content()
            briefing = telegram_common.ask_grounded(
                "Jarvis",
                "giving the user one cohesive daily briefing across PC health, security, "
                "finances, their hired employee team, and anything remembered long-term",
                content, "Give me my daily briefing.")
        except Exception as e:
            print(f"  [Briefing] {e}")
            continue
        with _command_lock:
            speak(briefing)
        _notify_all(f"[Daily Briefing]\n{briefing}")
        bot_events.publish("daily_briefing", {"text": briefing})


_NETWORK_SCAN_INTERVAL_SECONDS = 15 * 60  # 15 minutes


def _network_watch_thread():
    """Runs a LAN device scan immediately, then every 15 minutes for as long
    as Jarvis's backend is running -- router-agnostic (see network_watch.py),
    works the same regardless of what router/switch is in front of it.
    New-device Telegram alerts happen inside network_watch.scan() itself, so
    this thread only needs to keep calling it on a cadence; nothing here
    needs to speak or notify separately. Silently a no-op if nmap isn't
    installed (tools.scan_network() reports that in its return value, which
    is logged here but never spoken)."""
    while not pipeline_stop.is_set():
        try:
            result = tools.scan_network()
            print(f"  [Network] {result}")
        except Exception as e:
            print(f"  [Network] {e}")
        if pipeline_stop.wait(_NETWORK_SCAN_INTERVAL_SECONDS):
            break


_SECURITY_CHECK_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours
_DEEP_SECURITY_INTERVAL_SECONDS = 24 * 60 * 60  # 1 day -- lynis + rkhunter take minutes, not seconds


def _security_watcher_thread():
    """Runs tools.run_security_check() immediately, then every 4 hours for
    as long as Jarvis's backend is running -- open ports, suspicious
    processes, a LAN scan for new/unknown devices, and uBlock Origin Lite
    verify/install in the dedicated Jarvis browser, logged to
    ~/.jarvis/security_log.jsonl every time.

    The JarSecurity sub-agent (jarvis_security.py) rides along on the same
    cycle: it gets a summary after every sweep, and a critical finding (a
    suspicious process or a brand-new LAN device) triggers an immediate
    Telegram alert from it, same as the voice/SMS/main-Telegram alert
    below."""
    while not pipeline_stop.is_set():
        try:
            result = tools.run_security_check()
            print(f"  [Security] {result}")
        except Exception as e:
            print(f"  [Security] {e}")
            result = ""
        critical = tools.LAST_SECURITY_RESULT.get("critical", False)
        if critical:
            text = f"Security sweep sir: {result}"
            with _command_lock:
                speak(text)
            _notify_all(text)
            jarvis_security.send_alert(result)
            bot_events.publish("security_critical", {"result": result})
            _consult_admin_on_new_devices()
        if result:
            jarvis_security.send_summary(result)
        if pipeline_stop.wait(_SECURITY_CHECK_INTERVAL_SECONDS):
            break


def _deep_security_watcher_thread():
    """Runs the slower, deeper security scans -- lynis's full hardening
    audit and rkhunter's rootkit check -- once daily, immediately then
    every _DEEP_SECURITY_INTERVAL_SECONDS. Too slow (1-4 minutes each) for
    the cheap 4-hour run_security_check() sweep above, so they get their
    own schedule. Logs via tools.log_deep_scan_result() (a distinct
    "deep_scan" entry in the same security_log.jsonl run_security_check()
    already writes to) and alerts through JarSecurity immediately on a
    real finding -- a genuine lynis warning or any rkhunter warning, not
    just "the scan completed" -- same alert path (and same
    bot_events "security_critical" event feeding the phone dashboard's
    Live Activity feed) run_security_check() already uses."""
    while not pipeline_stop.is_set():
        try:
            audit_text = tools.run_security_audit()
            print(f"  [DeepSecurity] lynis: {audit_text}")
            rootkit_text = tools.run_rootkit_scan()
            print(f"  [DeepSecurity] rkhunter: {rootkit_text}")
            result = tools.log_deep_scan_result()
            if result["critical"]:
                jarvis_security.send_alert(result["summary"])
                bot_events.publish("security_critical", {"result": result["summary"]})
            else:
                jarvis_security.send_summary(result["summary"])
        except Exception as e:
            print(f"  [DeepSecurity] {e}")
        if pipeline_stop.wait(_DEEP_SECURITY_INTERVAL_SECONDS):
            break


def _consult_admin_on_new_devices():
    """JarSecurity -> JarvisAdmin consult: a brand-new LAN device was just
    flagged critical (see above). Rather than just alerting and waiting for
    the user to think to follow up, propose one bounded, already-existing
    action -- hiring a researcher employee to look up the device's MAC
    vendor for more context -- through JarvisAdmin's existing yes/no
    approval gate. Deliberately does NOT do anything more autonomous than
    that: there's no real "block this device" capability in this project
    (that would be router-specific and a separate, bigger ask) -- this only
    ever results in a read-only research job, never an action with a real
    effect. No-op if JarvisAdmin isn't configured, same opt-in-at-the-
    call-site pattern tools.system_power already uses."""
    if not jarvis_admin.JARVIS_ADMIN_AVAILABLE:
        return
    devices = tools.LAST_SECURITY_RESULT.get("new_device_list") or []
    if not devices:
        return
    desc = ", ".join(f"{d['mac']} ({d['ip']})" for d in devices)
    approved, _status = jarvis_admin.request_approval(
        f"look up the vendor/manufacturer of the new device(s) on your LAN ({desc}) for more context")
    if not approved:
        return
    for d in devices:
        employees.enqueue_job(
            "researcher",
            f"Look up what device/manufacturer typically uses the MAC address prefix {d['mac'][:8]} "
            f"and whether a device at {d['ip']} on a home LAN is likely benign or worth investigating.",
            auto=True,
        )


_SPEEDTEST_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours


def _speedtest_watcher_thread():
    """Runs a real internet speed test every 6 hours for as long as Jarvis's
    backend is running, logged to ~/.jarvis/speedtest_log.jsonl via
    speedtest_service -- unlike the other watchers, this one waits out the
    first interval before its first run rather than running immediately on
    startup, since it actually uses real bandwidth for ~15-30 seconds each
    time. Silently a no-op if speedtest-cli isn't installed."""
    while not pipeline_stop.is_set():
        if pipeline_stop.wait(_SPEEDTEST_INTERVAL_SECONDS):
            break
        try:
            result = tools.check_internet_speed()
            print(f"  [Speedtest] {result}")
        except Exception as e:
            print(f"  [Speedtest] {e}")


_PC_HEALTH_INTERVAL_SECONDS = 2  # feeds the HUD's live PcInternals 3D model


def _read_cpu_times():
    """(idle_jiffies, total_jiffies) from /proc/stat's aggregate cpu line --
    a single read is meaningless for CPU%, which needs a delta between two
    samples over a known interval (see _pc_health_watcher_thread below)."""
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = f.readline().split()[1:]
    values = [int(x) for x in parts]
    idle = values[3] + values[4]  # idle + iowait
    return idle, sum(values)


def _pc_health_watcher_thread():
    """Live CPU/memory/disk/temp feed for the HUD's PcInternals 3D model
    (see ArcReactor.tsx's "pc_health" WebSocket case) -- deliberately
    separate from check_system_health's existing 2-hour alert cycle
    (tools.py), which keeps doing its own Telegram/voice alerting and
    ~/.jarvis/health_log.jsonl logging unchanged. This one only ever
    broadcasts a live number for the 3D display every few seconds; it
    never alerts and never writes a log file. All reads are the same
    lightweight /proc and /sys style already used elsewhere in this
    project (tools._read_linux_temps, tools.py's own CPU-free philosophy)
    -- no psutil, no subprocess per tick, cheap enough to poll this often.
    Linux-only (/proc/stat, /proc/meminfo) -- only started on that platform,
    see the thread-start block below; PcInternals.tsx just shows "–"
    placeholders if no pc_health message ever arrives, same as before any
    connection at all."""
    prev_idle, prev_total = _read_cpu_times()
    while not pipeline_stop.is_set():
        if pipeline_stop.wait(_PC_HEALTH_INTERVAL_SECONDS):
            break
        try:
            idle, total = _read_cpu_times()
            d_idle, d_total = idle - prev_idle, total - prev_total
            cpu_pct = 100.0 * (1 - d_idle / d_total) if d_total > 0 else 0.0
            prev_idle, prev_total = idle, total

            meminfo = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    meminfo[key] = int(rest.strip().split()[0])  # kB
            mem_total = meminfo.get("MemTotal", 1)
            mem_avail = meminfo.get("MemAvailable", mem_total)
            mem_pct = 100.0 * (1 - mem_avail / mem_total) if mem_total else 0.0

            _total, _used, free = shutil.disk_usage(tools._DISK_ROOT)
            disk_pct = 100.0 * (1 - free / _total) if _total else 0.0

            _zone, hottest = tools._hottest_reading(tools._read_linux_temps())

            _ws_broadcast({
                "type": "pc_health",
                "cpu_pct": round(cpu_pct, 1),
                "mem_pct": round(mem_pct, 1),
                "disk_pct": round(disk_pct, 1),
                "temp_c": round(hottest, 1) if hottest is not None else None,
            })
        except Exception as e:
            print(f"  [PcHealth] {e}")


def _employee_worker_thread():
    """Runs employees.worker_loop() in this, jarvis.py's own long-lived
    process -- see employees.py's module docstring for why background
    employee jobs have to run here rather than inside hire_employee itself
    (which usually executes in a short-lived per-command MCP subprocess
    that exits, taking any thread it spawned with it, the moment that one
    command finishes)."""
    employees.worker_loop(pipeline_stop)


def _agent_scheduler_thread():
    """Polls agent_registry for agents whose schedule is due and enqueues
    them into employees.py's existing job queue -- the same
    _employee_worker_thread above still does all actual execution, so this
    thread never runs anything itself, just decides when to enqueue.
    30s resolution is plenty for "every N minutes" schedules and matches
    the polling cadence of Jarvis's other watcher threads."""
    while not pipeline_stop.is_set():
        for agent in agent_registry.due_agents():
            agent_registry.run_agent_now(agent["id"], auto=True)
        if pipeline_stop.wait(30.0):
            break


def _on_important_email(provider, category, summary):
    text = f"You've got an important email sir, on {provider}: {summary}"
    with _command_lock:
        speak(text)
    _notify_all(f"[Email - {provider} - {category}] {summary}")


def _sms_command_thread():
    def on_command(body):
        handle_command(body, acked=True, notify=sms.send_sms)
    sms.poll_thread(on_command, pipeline_stop)


def _telegram_command_thread():
    def on_command(body):
        handle_command(body, acked=True, notify=telegram_bridge.send_message)
    telegram_bridge.poll_thread(on_command, pipeline_stop)


def _email_command_thread():
    """JarvisEmail's own two-way channel -- routes through the SAME full
    handle_command() brain as the main bridge (not a narrower, fixed-
    response bot like CPU_Alerts/Improvement below), since the whole point
    is open-ended "ask anything about my mail/calendar" Q&A and actions.
    See jarvis_email_bot.poll_thread()."""
    def on_command(body):
        handle_command(body, acked=True, notify=jarvis_email_bot.send_message)
    jarvis_email_bot.poll_thread(on_command, pipeline_stop)


def _outlook_command_thread():
    """JarvisOutlook -- same setup as JarvisEmail (see jarvis_email_bot.py's
    docstring), just its own dedicated bot/chat channel into the same full
    brain. See jarvis_outlook_bot.poll_thread()."""
    def on_command(body):
        handle_command(body, acked=True, notify=jarvis_outlook_bot.send_message)
    jarvis_outlook_bot.poll_thread(on_command, pipeline_stop)


def _cpu_alerts_command_thread():
    """JarvisCPU_Alerts's own two-way channel: any incoming message on its
    bot gets an immediate on-demand health check back. See
    jarvis_cpu_alerts.poll_thread()."""
    jarvis_cpu_alerts.poll_thread(pipeline_stop)


def _improvement_command_thread():
    """JarvisImprovement's own two-way channel: any incoming message on its
    bot is treated as an on-demand self-improvement request. See
    jarvis_improvement.poll_thread()."""
    jarvis_improvement.poll_thread(pipeline_stop)


def _security_command_thread():
    """JarSecurity's own two-way channel: any incoming message on its bot
    gets an immediate recent-activity status report back. See
    jarvis_security.poll_thread()."""
    jarvis_security.poll_thread(pipeline_stop)


def _email_watch_thread():
    email_watcher.poll_thread(_on_important_email, pipeline_stop)


# ================================================================ COMMAND DISPATCH
def handle_command(command, acked=False, notify=None):
    """notify, if given, is called with the final reply text in addition to
    speaking it aloud -- used to text an SMS-originated command's answer
    back, regardless of which channel (voice/typed/SMS) the command came
    from."""
    # Checked before _command_lock on purpose: if a job (self_improve /
    # hire_employee, which can run for minutes) is in flight, the thread that
    # started it is holding _command_lock for the whole duration, so a
    # cancel has to interrupt from outside that lock rather than queue
    # behind it and only "cancel" after the job already finished on its own.
    if command.lower().strip() in _JOB_CANCEL_PHRASES:
        def _cancel_reply(text):
            if notify:
                try: notify(text)
                except Exception as e: print(f"  [Notify Error] {e}")
            return speak(text)
        if _cancel_active_job():
            return _cancel_reply("Cancelled that job sir -- stopping it now.")
        return _cancel_reply("There's no job running right now sir.")

    with _command_lock:
        cmd = command.lower().strip()

        def _reply(text):
            if notify:
                try: notify(text)
                except Exception as e: print(f"  [Notify Error] {e}")
            return speak(text)

        if any(t in cmd for t in _TIME_TRIGGERS):
            return _reply(f"The current time is {time.strftime('%I:%M %p').lstrip('0')} sir.")

        if any(t in cmd for t in _DATE_TRIGGERS):
            return _reply(f"Today is {time.strftime('%A %B %d')} sir.")

        if any(t in cmd for t in _GREETING_TRIGGERS):
            return _reply(random.choice(_GREETING_RESPONSES).replace("{tod}", _time_of_day()))

        if any(w in cmd for w in _EXIT_TRIGGERS):
            _reply("Goodbye sir.")
            return "exit"

        if cmd == "stop listening":
            _listening_enabled.clear()
            return _reply("Voice input off sir. I'll only respond over text from here.")

        if cmd == "start listening":
            _listening_enabled.set()
            return _reply("Voice input back on sir.")

        if cmd == "stop speaking":
            _speaking_enabled.clear()
            return _reply("Going quiet sir. I'll reply over text from here.")

        if cmd == "start speaking":
            _speaking_enabled.set()
            return _reply("Voice output back on sir.")

        if cmd == "text mode only":
            _listening_enabled.clear()
            _speaking_enabled.clear()
            return _reply("Switching to text-only mode sir. Text me \"start listening\" "
                           "or \"start speaking\" whenever you'd like voice back.")

        if cmd in _MEDIA_FAST_PHRASES:
            return _reply(tools.media_control(_MEDIA_FAST_PHRASES[cmd]))

        cleaned_check = _clean_command(cmd)
        if len(command.strip()) < 5 or not cleaned_check:
            return _reply("Could you say that again sir? I didn't catch it clearly.")

        if not acked:
            speak(random.choice(_INLINE_CONFIRMS))

        set_status("Thinking")
        job_record = None

        def _on_process(proc):
            nonlocal job_record
            job_record = _register_active_job(proc)

        reply = brain.run_agent(command, on_activity=_on_brain_activity,
                                 on_creation=_on_brain_creation, on_process=_on_process)
        if job_record is not None:
            was_cancelled = job_record["cancelled"]
            _clear_active_job(job_record)
            if was_cancelled:
                # The "cancel job" fast path already replied -- don't also
                # speak a stray fallback reply for the command it cancelled.
                return None
        if reply is None:
            set_status("Processing (offline)")
            reply = ask_ollama(command)
        return _reply(reply)


# ================================================================ VOICE LOOP
def voice_loop():
    global _sapi_speaker

    if _already_running():
        print(f"  [Startup] Jarvis is already running on port {WS_PORT} sir -- "
              f"this instance is exiting rather than double-processing commands.")
        pipeline_stop.set()
        return

    if PYGAME_AVAILABLE:
        try:
            pygame.mixer.pre_init(44100, -16, 2, 512)
            pygame.mixer.init()
            print("  [Audio] pygame mixer ready")
        except Exception as e:
            print(f"  [Audio Error] {e}")
    elif IS_WINDOWS:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            _sapi_speaker = win32com.client.Dispatch("SAPI.SpVoice")
            _sapi_speaker.Rate = SPEECH_RATE
            _sapi_speaker.Volume = 100
        except Exception as e:
            print(f"  [SAPI Error] {e}")

    ensure_vosk_model()
    start_ws_server()
    if PLAID_AVAILABLE:
        plaid_service.start_server()
    dashboard_server.register_command_handler(handle_command)
    dashboard_server.register_frame_handler(_on_camera_frame)
    dashboard_server.register_gesture_handler(_on_hand_gesture)
    dashboard_server.start_server()
    bot_events.subscribe(_on_bot_event)

    threading.Thread(target=mic_thread, daemon=True, name="Mic").start()
    threading.Thread(target=recognition_thread, daemon=True, name="Vosk").start()
    threading.Thread(target=_reminder_watcher_thread, daemon=True, name="Reminders").start()
    threading.Thread(target=_health_watcher_thread, daemon=True, name="Health").start()
    threading.Thread(target=_finance_watcher_thread, daemon=True, name="FinanceWatch").start()
    threading.Thread(target=_employee_worker_thread, daemon=True, name="EmployeeWorker").start()
    threading.Thread(target=_agent_scheduler_thread, daemon=True, name="AgentScheduler").start()
    threading.Thread(target=_improvement_watcher_thread, daemon=True, name="Improvement").start()
    threading.Thread(target=_daily_briefing_thread, daemon=True, name="DailyBriefing").start()
    threading.Thread(target=_sms_command_thread, daemon=True, name="SMS").start()
    threading.Thread(target=_telegram_command_thread, daemon=True, name="Telegram").start()
    threading.Thread(target=_email_command_thread, daemon=True, name="EmailTelegram").start()
    threading.Thread(target=_outlook_command_thread, daemon=True, name="OutlookTelegram").start()
    threading.Thread(target=_email_watch_thread, daemon=True, name="EmailWatch").start()
    threading.Thread(target=_network_watch_thread, daemon=True, name="NetworkWatch").start()
    threading.Thread(target=_security_watcher_thread, daemon=True, name="Security").start()
    threading.Thread(target=_deep_security_watcher_thread, daemon=True, name="DeepSecurity").start()
    threading.Thread(target=_speedtest_watcher_thread, daemon=True, name="Speedtest").start()
    if not IS_WINDOWS:
        threading.Thread(target=_pc_health_watcher_thread, daemon=True, name="PcHealth").start()
    threading.Thread(target=_cpu_alerts_command_thread, daemon=True, name="CPUAlertsTelegram").start()
    threading.Thread(target=_improvement_command_thread, daemon=True, name="ImprovementTelegram").start()
    threading.Thread(target=_security_command_thread, daemon=True, name="SecurityTelegram").start()
    time.sleep(0.5)

    set_status("Idle")
    print("\n  Ready - say 'Jarvis' to begin.\n")

    while not pipeline_stop.is_set():
        try:
            if not _listening_enabled.is_set():
                set_status("Text-only mode")
                if pipeline_stop.wait(1):
                    break
                continue

            set_status("Idle")
            print("  Waiting for 'Jarvis'...")
            detected, inline_cmd = wait_for_wake_word(timeout=10)
            if not detected:
                continue

            if inline_cmd:
                print(f"  [Inline] {inline_cmd}")
                set_status("Processing")
                speak(random.choice(_INLINE_CONFIRMS))
                result = handle_command(inline_cmd, acked=True)
            else:
                speak(random.choice(_WAKE_RESPONSES))
                set_status("Listening")
                command = get_command(timeout=7, clear=False)
                if not command:
                    speak("I didn't catch that sir.")
                    continue
                set_status("Processing")
                result = handle_command(command)

            if result == "exit":
                pipeline_stop.set()
                break

        except Exception as e:
            print(f"  [Voice Loop Error] {e}")
            time.sleep(1)

    _ws_broadcast({"type": "jarvis_offline"})
    time.sleep(0.5)

    if PYGAME_AVAILABLE:
        try: pygame.mixer.quit()
        except: pass
    if browser_control is not None:
        try: browser_control.shutdown()
        except: pass

    print("\n  Jarvis offline.\n")


# ================================================================ MAIN
def main():
    threading.Thread(target=voice_loop, daemon=True, name="VoiceLoop").start()
    print("  Running headless. Press Ctrl+C to stop.")
    try:
        while not pipeline_stop.is_set():
            # pygame.mixer (TTS playback) initializes SDL under the hood the
            # first time anything speaks -- which can happen on any
            # background thread (e.g. the WS "hud_ready" handler) -- and SDL
            # grabs SIGINT/SIGQUIT/SIGTERM by default to post an internal
            # SDL_QUIT event that this headless app never pumps, silently
            # swallowing termination signals entirely (confirmed live: a
            # real SIGTERM was ignored for 90+ seconds until systemd force-
            # killed it). signal.signal() only works from the main thread,
            # so it can't be fixed at the point pygame claims it -- instead
            # the main thread keeps reasserting the correct handler here on
            # every tick, which bounds worst-case shutdown latency to one
            # tick regardless of which thread touched the mixer first.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    pipeline_stop.set()
    print("\n  Jarvis offline.\n")


if __name__ == "__main__":
    main()
