# ================================================================
#   J.A.R.V.I.S — SMS bridge (Twilio)
#
#   Lets you text Jarvis and get a reply back, without exposing your
#   home PC to inbound internet traffic: instead of a webhook, this
#   POLLS Twilio's REST API for new inbound messages every few
#   seconds (outbound-only, no public endpoint needed).
#
#   Fully inert until TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN /
#   TWILIO_FROM_NUMBER / USER_PHONE_NUMBER are set in .env -- see
#   README's "Text Jarvis (SMS)" setup section.
# ================================================================
import os
import time

try:
    from twilio.rest import Client
    TWILIO_LIB_AVAILABLE = True
except ImportError:
    TWILIO_LIB_AVAILABLE = False

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")  # the Twilio number Jarvis texts from
USER_NUMBER = os.environ.get("USER_PHONE_NUMBER", "")   # your phone, the only number Jarvis will act on

SMS_AVAILABLE = bool(TWILIO_LIB_AVAILABLE and ACCOUNT_SID and AUTH_TOKEN and FROM_NUMBER and USER_NUMBER)

_client = Client(ACCOUNT_SID, AUTH_TOKEN) if SMS_AVAILABLE else None
_seen_sids = set()


def send_sms(body: str) -> bool:
    """Text the configured user number. Best-effort -- returns False on
    failure rather than raising, so a Twilio hiccup never crashes a command."""
    if not SMS_AVAILABLE or not body:
        return False
    try:
        # SMS has no real length limit concern here (Twilio auto-segments),
        # but keep replies concise -- this mirrors the voice persona anyway.
        _client.messages.create(to=USER_NUMBER, from_=FROM_NUMBER, body=body[:1500])
        return True
    except Exception as e:
        print(f"  [SMS] Send error: {e}")
        return False


def poll_thread(on_command, pipeline_stop, poll_secs=15):
    """Background loop: checks for new inbound texts from USER_NUMBER and
    calls on_command(text, reply_fn) for each one. `pipeline_stop` is the
    shared threading.Event used to shut every Jarvis thread down together."""
    if not SMS_AVAILABLE:
        print("  [SMS] Not configured -- texting Jarvis is disabled. See README.")
        return

    print(f"  [SMS] Watching for texts from {USER_NUMBER} -> {FROM_NUMBER}")
    # Only look at messages from roughly the last poll cycle onward, but keep
    # a seen-set too since Twilio's date filters aren't sub-second precise.
    started_at = time.time()

    while not pipeline_stop.is_set():
        try:
            messages = _client.messages.list(
                to=FROM_NUMBER, from_=USER_NUMBER, limit=20,
            )
            for msg in messages:
                if msg.sid in _seen_sids:
                    continue
                _seen_sids.add(msg.sid)
                if msg.date_sent and msg.date_sent.timestamp() < started_at - 60:
                    continue  # ignore anything from before we started watching
                body = (msg.body or "").strip()
                if not body:
                    continue
                print(f"  [SMS] Received: {body!r}")
                try:
                    on_command(body)
                except Exception as e:
                    print(f"  [SMS] Command handling error: {e}")
                    send_sms("Something went wrong handling that sir.")
        except Exception as e:
            print(f"  [SMS] Poll error: {e}")

        time.sleep(poll_secs)
