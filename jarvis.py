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
load_dotenv()  # loads .env into os.environ before any module below reads its config

import sys as _sys
import pyaudio, requests

IS_WINDOWS = _sys.platform == "win32"

if IS_WINDOWS:
    import win32com.client

try:
    import pygame; PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

try:
    import websockets; WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

import tools
import brain
import bot_events
import tts_service

try:
    import browser_control
except ImportError:
    browser_control = None


# ================================================================ SETTINGS
OLLAMA_MODEL, OLLAMA_URL = "llama3.2", "http://localhost:11434/api/generate"
# Groq retires/renames model ids fairly often -- if this 404s with "model
# does not exist", check
# https://api.groq.com/openai/v1/models (with your key) for what's current.
GROQ_MODEL, GROQ_URL = "openai/gpt-oss-120b", "https://api.groq.com/openai/v1/chat/completions"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
WAKE_WORD, SPEECH_RATE, WAKE_COOLDOWN = "jarvis", 2, 2.0
SAMPLE_RATE, CHUNK_SIZE = 16000, 8000
CTX_TIMEOUT, WS_PORT, INLINE_WAIT_SECS = 15.0, 8765, 1.5

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
    tts_text = tts_service.prep_text(text)

    if tts_service.EDGE_TTS_AVAILABLE and PYGAME_AVAILABLE and _ensure_mixer():
        tmp = os.path.join(tempfile.gettempdir(), f"jarvis_{threading.get_ident()}.mp3")
        try:
            if not tts_service.synthesize_to_file(tts_text, tmp):
                raise RuntimeError("edge-tts synthesis failed")
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


def _on_bot_event(event):
    """Bridges bot_events (the shared pub/sub bus tools.py's voice-triggered
    map tools publish to) into the desktop HUD's own WebSocket feed -- the
    voice-triggered map tools (`show_map`/`show_weather_radar` in tools.py),
    so a voice command actually updates the desktop UI instead of only
    being spoken back. Never raises -- bot_events.publish already isolates
    subscriber exceptions, but this stays defensive since it runs on that
    shared dispatch path."""
    etype = event.get("type")
    payload = event.get("payload") or {}

    if etype == "show_map":
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


# ================================================================ FALLBACK BRAINS
# Used only when the main Claude brain is unreachable (offline, not
# authenticated, claude.exe missing). Neither path has tool access -- chat
# only. Groq is tried first (fast, cloud, needs GROQ_API_KEY -- inert
# no-op until that's set in .env); if it's not configured or the call
# fails for any reason, ask_ollama is the final, fully-offline fallback.
_GROQ_SYSTEM = (
    "You are J.A.R.V.I.S, a voice assistant currently running on a fast "
    "backup brain with no tool access -- you can't check files, run "
    "commands, or reach any of your normal capabilities right now, only "
    "talk. You only know what the user directly tells you in this message. "
    "Never invent facts, events, numbers, times, or scenarios that were not "
    "stated by the user. If asked to do something that needs a real tool, "
    "say plainly you're running on backup and can't reach your full "
    "capabilities right now. Speak with calm confidence and quiet wit. "
    "Address the user as sir occasionally but not every sentence. Keep "
    "answers under 40 words. Short natural sentences. Never use bullet "
    "points or markdown. Never say your own name."
)

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


def ask_groq(question):
    """Fast cloud fallback tried before the fully-offline Ollama path.
    Returns "" (never raises) if GROQ_API_KEY isn't set or the call fails
    for any reason, so the caller falls through to ask_ollama."""
    if not GROQ_API_KEY:
        return ""
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": _GROQ_SYSTEM},
                    {"role": "user", "content": question},
                ],
            },
            timeout=15,
        )
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(f"  [Jarvis] ask_groq error: {e}")
    return ""


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


def handle_command(command, acked=False, notify=None):
    """notify, if given, is called with the final reply text in addition to
    speaking it aloud."""
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

        reply = brain.run_agent(command, on_activity=_on_brain_activity, on_process=_on_process)
        if job_record is not None:
            was_cancelled = job_record["cancelled"]
            _clear_active_job(job_record)
            if was_cancelled:
                # The "cancel job" fast path already replied -- don't also
                # speak a stray fallback reply for the command it cancelled.
                return None
        if reply is None:
            set_status("Processing (fallback)")
            reply = ask_groq(command)
            if not reply:
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
    bot_events.subscribe(_on_bot_event)

    threading.Thread(target=mic_thread, daemon=True, name="Mic").start()
    threading.Thread(target=recognition_thread, daemon=True, name="Vosk").start()
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
