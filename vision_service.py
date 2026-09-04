# ================================================================
#   vision_service — gives the phone HUD's camera (see
#   dashboard/jarvis_hud.html) a "sense" the real Jarvis brain can use.
#
#   This does NOT answer the user's question itself anymore. Earlier it
#   sent one frame + the user's exact question straight to Gemini and
#   spoke Gemini's answer back -- which meant every question got a
#   stateless, personality-less vision-model reply with zero awareness
#   of what Jarvis can actually do (open apps, play YouTube, check the
#   weather, remember things, ...). Now describe_scene() gets Gemini to
#   describe what's visible, and dashboard_server.py's `_hud_ask` route
#   hands that description as context to the REAL brain
#   (jarvis.py's handle_command -> brain.run_agent, the same Claude
#   tool-calling pipeline voice/Telegram/SMS/the main dashboard use) --
#   so "what am I looking at" and "what can you help me with" both go
#   through one real, capable, personality-consistent Jarvis.
#
#   Fully inert (describe_scene returns None) until GEMINI_API_KEY is set
#   in .env -- same "no-op until configured" pattern every other optional
#   integration in this project follows (Telegram bots, NAS, email...).
#   Even without a key, the real brain still answers everything that
#   isn't about the live camera view -- see dashboard_server.py's
#   `_hud_ask` for exactly what context it hands over either way.
#
#   (The phone HUD's live bounding boxes are a separate, local-only layer
#   -- TensorFlow.js running entirely in the browser, no backend involved
#   -- that's just an ambient "I can see something" visual, unrelated to
#   this file.)
# ================================================================
import base64
import os

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_AVAILABLE = bool(GEMINI_API_KEY)


# Google retires/renames Gemini model ids fairly often -- gemini-1.5-flash
# (this file's original choice) returned a 404 telling callers to switch
# to gemini-3.6-flash. That one turned out to be the wrong pick anyway:
# it's an extended-thinking model that burns a variable, sometimes large,
# chunk of its own output-token budget on hidden reasoning before writing
# the visible answer -- confirmed live (usageMetadata.thoughtsTokenCount
# was 100-300+ per call) -- which caused exactly the "vision request
# failed" symptom: replies truncated mid-sentence (finishReason
# MAX_TOKENS) and latency swinging from ~2s to 20s+ per call, occasionally
# past whatever timeout was set. gemini-flash-lite-latest doesn't do that
# (thoughtsTokenCount is absent entirely) -- consistently sub-second to a
# couple seconds, no truncation, and it's Google's auto-updating alias for
# "current lite-tier model" so it won't go stale the way a hardcoded
# version did here before. A plain scene description doesn't need
# extended reasoning anyway. If this starts 404ing, check
# https://ai.google.dev/gemini-api/docs/models or GET
# https://generativelanguage.googleapis.com/v1beta/models?key=... for
# the current lite-tier vision-capable model id.
_GEMINI_MODEL = "gemini-flash-lite-latest"
_GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"
)

_PROMPT = (
    "Describe only what is visible in this camera frame, in 1-2 concise, "
    "factual sentences. Plain text, no markdown, no questions, no "
    "commentary -- just what's actually there."
)


def describe_scene(jpeg_bytes: bytes, timeout: int = 20):
    """Sends one JPEG frame to Gemini for a short factual description.
    Returns the description, or None if no API key is configured or the
    call fails for any reason -- callers (dashboard_server.py's
    `_hud_ask`) must treat None as "vision unavailable right now" and say
    so honestly to the real brain, never invent a substitute description.

    Retries once (any failure reason -- timeout, network error, a bad
    response). Verified live: back-to-back calls with zero spacing
    occasionally hit a transient stall (~20s timeout) even though the
    same call normally lands in under a second with real-world pacing
    between requests -- this one retry absorbs exactly that kind of rare
    blip instead of surfacing "vision request failed" for something that
    would have succeeded a second later. Not retried indefinitely, so a
    genuine problem (bad key, real rate-limit exhaustion) still surfaces
    as unavailable rather than hanging."""
    if not GEMINI_AVAILABLE:
        return None
    result = _describe_scene_once(jpeg_bytes, timeout)
    if result is None:
        result = _describe_scene_once(jpeg_bytes, timeout)
    return result


def _describe_scene_once(jpeg_bytes: bytes, timeout: int):
    """Failures are logged here (status code / exception) even though the
    caller only sees None -- without that, "vision request failed" was a
    dead end with no way to tell a bad API key from a rate limit from a
    Gemini-side content-safety block from a plain timeout."""
    try:
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        resp = requests.post(
            _GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{
                    "parts": [
                        {"text": _PROMPT},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    ]
                }],
                "generationConfig": {"maxOutputTokens": 300, "temperature": 0.3},
            },
            timeout=timeout,
        )
        if resp.status_code == 429:
            print("  [Vision] Gemini rate-limited (429) -- free-tier quota likely exceeded for now.")
            return None
        if resp.status_code != 200:
            print(f"  [Vision] Gemini returned {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            print(f"  [Vision] Gemini returned no candidates (likely a safety block): {data}")
            return None
        finish_reason = candidates[0].get("finishReason", "")
        if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
            print(f"  [Vision] Gemini finishReason={finish_reason} -- no usable description.")
            return None
        parts = candidates[0].get("content", {}).get("parts") or []
        text = (parts[0].get("text", "") if parts else "").strip()
        return text or None
    except requests.exceptions.Timeout:
        print(f"  [Vision] Gemini request timed out after {timeout}s.")
        return None
    except Exception as e:
        print(f"  [Vision] Gemini request failed: {e}")
        return None
