// J.A.R.V.I.S HUD -- synthesized UI sound design (Web Audio oscillators, not
// shipped audio files -- nothing to bundle or license). Mirrors the chirp
// approach the phone HUD (dashboard/jarvis_hud.html) uses, tuned for a
// subtler, ambient desktop presence: a wake chirp, a soft looping tick
// while Jarvis is thinking/processing, and a short confirm tone right as
// it starts speaking. Muted state persists across launches.

const MUTE_KEY = "jarvis-hud-muted";

let ctx: AudioContext | null = null;
let thinkingTimer: ReturnType<typeof setInterval> | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!ctx) {
    const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
    if (!AudioCtx) return null;
    ctx = new AudioCtx();
  }
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  return ctx;
}

export function isMuted(): boolean {
  try { return localStorage.getItem(MUTE_KEY) === "1"; } catch { return false; }
}

export function setMuted(muted: boolean) {
  try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch {}
  if (muted) stopThinking();
}

function tone(freq: number, startOffset: number, dur: number, type: OscillatorType, peak = 0.05) {
  const audio = getCtx();
  if (!audio || isMuted()) return;
  const osc = audio.createOscillator();
  const gain = audio.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, audio.currentTime + startOffset);
  gain.gain.setValueAtTime(0, audio.currentTime + startOffset);
  gain.gain.linearRampToValueAtTime(peak, audio.currentTime + startOffset + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + startOffset + dur);
  osc.connect(gain).connect(audio.destination);
  osc.start(audio.currentTime + startOffset);
  osc.stop(audio.currentTime + startOffset + dur + 0.05);
}

/** Ascending two-tone chirp -- wake word / HUD power-on. */
export function playWake() {
  tone(440, 0, 0.08, "sine", 0.06);
  tone(740, 0.07, 0.14, "sine", 0.05);
}

/** Soft triad -- Jarvis begins speaking a response. */
export function playRespond() {
  tone(520, 0, 0.07, "triangle", 0.04);
  tone(780, 0.05, 0.09, "triangle", 0.035);
}

/** Single, understated tick -- one "thinking" heartbeat. Looped by
 * startThinking() below rather than called directly. */
function thinkingTick() {
  tone(300, 0, 0.04, "sine", 0.02);
  tone(300, 0.14, 0.04, "sine", 0.015);
}

/** Starts a soft, regular tick loop for as long as Jarvis is
 * thinking/processing -- idempotent, safe to call repeatedly. */
export function startThinking() {
  if (thinkingTimer) return;
  thinkingTick();
  thinkingTimer = setInterval(thinkingTick, 480);
}

export function stopThinking() {
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
}
