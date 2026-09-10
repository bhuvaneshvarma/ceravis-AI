/* Microphone -> 8 kHz mono G.711 A-law, on the audio thread.

   The camera speaker takes exactly one format (see edge/talkback/mpegts.py), so
   the conversion has to happen somewhere. It happens HERE, in an AudioWorklet,
   for two reasons: the browser already holds the samples, and doing it on the
   audio thread means no main-thread jank can put a gap in someone's voice.

   Two steps, both cheap:
     * resample to 8 kHz by averaging each input window (a box filter, so the
       decimation does not fold hiss down into the speech band the way plain
       point-sampling does);
     * A-law encode through a 64 kB table built once at construction.

   Output: 160-byte (20 ms) frames posted to the main thread, plus a peak level
   for the meter. */

const FRAME_BYTES = 160;          // 20 ms at 8 kHz
const TARGET_RATE = 8000;

function buildAlawTable() {
  const SEG_ENDS = [0x1f, 0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff];
  const table = new Uint8Array(65536);
  for (let u = 0; u < 65536; u++) {
    let s = u < 32768 ? u : u - 65536;
    s >>= 3;                                    // 16-bit -> 13-bit
    let mask;
    if (s >= 0) {
      mask = 0xd5;                              // sign bit 1 = positive
    } else {
      mask = 0x55;
      s = -s - 1;
    }
    let seg = 8;
    for (let i = 0; i < 8; i++) if (s <= SEG_ENDS[i]) { seg = i; break; }
    if (seg >= 8) { table[u] = 0x7f ^ mask; continue; }
    const val = seg < 2 ? (s >> 1) & 0x0f : (s >> seg) & 0x0f;
    table[u] = ((seg << 4) | val) ^ mask;
  }
  return table;
}

class TalkProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.alaw = buildAlawTable();
    this.ratio = sampleRate / TARGET_RATE;      // `sampleRate` = context rate
    this.pos = 0;                               // fractional read head
    this.tail = new Float32Array(0);            // samples not yet consumed
    this.out = new Uint8Array(FRAME_BYTES);
    this.filled = 0;
    this.peak = 0;
    this.muted = true;                          // starts silent: no hot mic
    this.gain = 1;
    this.port.onmessage = (e) => {
      if (!e.data) return;
      if (e.data.type === "mute") this.muted = !!e.data.value;
      // Clamped here, not at the caller: this is the last code that touches the
      // samples, so it is the only place a bad number cannot get past.
      else if (e.data.type === "gain")
        this.gain = Math.max(0.1, Math.min(8, Number(e.data.value) || 1));
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    // Carry the unconsumed tail so window boundaries never clip a sample.
    const buf = new Float32Array(this.tail.length + ch.length);
    buf.set(this.tail, 0);
    buf.set(ch, this.tail.length);

    let p = this.pos;
    while (p + this.ratio <= buf.length) {
      const start = p | 0;
      const end = Math.min(buf.length, Math.ceil(p + this.ratio));
      let sum = 0;
      for (let i = start; i < end; i++) sum += buf[i];
      let v = sum / (end - start || 1);

      const a = Math.abs(v);
      if (a > this.peak) this.peak = a;
      if (this.muted) v = 0;
      else if (this.gain !== 1) {
        // Soft limiter, not a multiply. G.711 clips hard at full scale, so a
        // plain gain turns a raised voice into a burst of noise; tanh bends the
        // loud part instead of shearing it, and stays linear where it matters.
        v = Math.tanh(v * this.gain);
      }

      // Float [-1,1] -> 16-bit -> A-law, clipped rather than wrapped.
      let s = Math.round(v * 32767);
      if (s > 32767) s = 32767; else if (s < -32768) s = -32768;
      this.out[this.filled++] = this.alaw[s < 0 ? s + 65536 : s];

      if (this.filled === FRAME_BYTES) {
        const frame = this.out.slice(0);
        this.port.postMessage({ type: "audio", frame: frame.buffer, peak: this.peak },
                              [frame.buffer]);
        this.peak = 0;
        this.filled = 0;
      }
      p += this.ratio;
    }

    const consumed = p | 0;
    this.tail = buf.slice(consumed);
    this.pos = p - consumed;
    return true;
  }
}

registerProcessor("talk-processor", TalkProcessor);
