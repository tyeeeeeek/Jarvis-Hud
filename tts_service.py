# ================================================================
#   tts_service — shared server-side edge-tts synthesis.
#
#   Extracted out of jarvis.py so any other module (a phone-facing web
#   server, a second bot, etc.) can generate the exact same voice without
#   importing jarvis.py itself. jarvis.py's own speak() (desktop voice,
#   played locally through this PC's speakers via pygame) calls
#   synthesize_to_file() here.
# ================================================================
import asyncio
import re

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

# Same voice/rate/volume everywhere Jarvis speaks, desktop or phone.
JARVIS_VOICE = "en-GB-RyanNeural"
JARVIS_VOICE_RATE = "+8%"
JARVIS_VOICE_VOL = "+10%"


def prep_text(text: str) -> str:
    """Strips commas (edge-tts reads them as long, unnatural pauses in
    short spoken replies) and collapses the resulting double-spaces."""
    result = text.replace(", ", " ").replace(",", " ")
    return re.sub(r'  +', ' ', result).strip()


async def _save(text: str, path: str):
    communicate = edge_tts.Communicate(text, JARVIS_VOICE, rate=JARVIS_VOICE_RATE, volume=JARVIS_VOICE_VOL)
    await communicate.save(path)


def synthesize_to_file(text: str, path: str) -> bool:
    """Synchronous wrapper: writes an mp3 of `text` to `path`. Returns
    True on success, False if edge-tts isn't installed, text is empty, or
    synthesis fails for any reason (network, rate limit, etc) -- callers
    must treat False as "no audio available" and fall back to text-only,
    never raise it up as an error."""
    if not EDGE_TTS_AVAILABLE or not text:
        return False
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_save(prep_text(text), path))
        finally:
            loop.close()
        return True
    except Exception as e:
        print(f"  [TTS] edge-tts synthesis failed: {e}")
        return False
