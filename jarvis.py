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

import os, re, json, time, queue, random, asyncio, webbrowser, socket
import tempfile, threading, zipfile, urllib.request

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ -- must happen before sms.py/email_watcher.py
                # read their TWILIO_*/GMAIL_*/USER_PHONE_NUMBER config at import time

import pyaudio, requests, win32com.client

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


async def _edge_tts_save(text, path):
    communicate = edge_tts.Communicate(text, JARVIS_VOICE, rate=JARVIS_VOICE_RATE, volume=JARVIS_VOICE_VOL)
    await communicate.save(path)


def _prep_tts_text(text):
    result = text.replace(", ", " ").replace(",", " ")
    return re.sub(r'  +', ' ', result).strip()


def speak(text):
    if not text:
        return True
    interrupt_flag.clear()
    print(f"\n  [Jarvis]  {text}\n")
    _ws_broadcast({"type": "speaking", "value": True, "text": text})
    completed = True
    tts_text = _prep_tts_text(text)

    if EDGE_TTS_AVAILABLE and PYGAME_AVAILABLE:
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
            completed = _speak_sapi_fallback(tts_text)
    else:
        completed = _speak_sapi_fallback(tts_text)

    _ws_broadcast({"type": "speaking", "value": False})
    return completed


# ================================================================ SHARED STATE
audio_queue, text_queue = queue.Queue(), queue.Queue()
interrupt_flag, pipeline_stop = threading.Event(), threading.Event()
_last_wake_ts = 0.0

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
    replying to every text twice."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("localhost", WS_PORT))
        s.close()
        return False
    except OSError:
        return True


def start_ws_server():
    if not WS_AVAILABLE:
        print("  [WS] 'websockets' package not installed — HUD will not connect.")
        return
    threading.Thread(target=_ws_server_thread, daemon=True, name="WebSocket").start()


# ================================================================ MIC / VOSK THREADS
def mic_thread():
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    while not pipeline_stop.is_set():
        audio_queue.put(stream.read(CHUNK_SIZE, exception_on_overflow=False))
    stream.stop_stream()
    stream.close()
    p.terminate()


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


def _on_brain_creation(payload):
    path = payload.get("path")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception as e:
        print(f"  [Creation] couldn't read {path}: {e}")
        return
    _ws_broadcast({
        "type": "creation_ready",
        "title": payload.get("title", ""),
        "kind": payload.get("kind", "dashboard"),
        "html": html,
    })
    if payload.get("kind") == "webpage":
        try:
            webbrowser.open("file:///" + path.replace("\\", "/"))
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


def _on_important_email(category, summary):
    text = f"You've got an important email sir: {summary}"
    with _command_lock:
        speak(text)
    _notify_all(f"[Email - {category}] {summary}")


def _sms_command_thread():
    def on_command(body):
        handle_command(body, acked=True, notify=sms.send_sms)
    sms.poll_thread(on_command, pipeline_stop)


def _telegram_command_thread():
    def on_command(body):
        handle_command(body, acked=True, notify=telegram_bridge.send_message)
    telegram_bridge.poll_thread(on_command, pipeline_stop)


def _email_watch_thread():
    email_watcher.poll_thread(_on_important_email, pipeline_stop)


# ================================================================ COMMAND DISPATCH
def handle_command(command, acked=False, notify=None):
    """notify, if given, is called with the final reply text in addition to
    speaking it aloud -- used to text an SMS-originated command's answer
    back, regardless of which channel (voice/typed/SMS) the command came
    from."""
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

        if cmd in _MEDIA_FAST_PHRASES:
            return _reply(tools.media_control(_MEDIA_FAST_PHRASES[cmd]))

        cleaned_check = _clean_command(cmd)
        if len(command.strip()) < 5 or not cleaned_check:
            return _reply("Could you say that again sir? I didn't catch it clearly.")

        if not acked:
            speak(random.choice(_INLINE_CONFIRMS))

        set_status("Thinking")
        reply = brain.run_agent(command, on_activity=_on_brain_activity, on_creation=_on_brain_creation)
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
    else:
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

    threading.Thread(target=mic_thread, daemon=True, name="Mic").start()
    threading.Thread(target=recognition_thread, daemon=True, name="Vosk").start()
    threading.Thread(target=_reminder_watcher_thread, daemon=True, name="Reminders").start()
    threading.Thread(target=_sms_command_thread, daemon=True, name="SMS").start()
    threading.Thread(target=_telegram_command_thread, daemon=True, name="Telegram").start()
    threading.Thread(target=_email_watch_thread, daemon=True, name="EmailWatch").start()
    time.sleep(0.5)

    set_status("Idle")
    print("\n  Ready - say 'Jarvis' to begin.\n")

    while not pipeline_stop.is_set():
        try:
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
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    pipeline_stop.set()
    print("\n  Jarvis offline.\n")


if __name__ == "__main__":
    main()
