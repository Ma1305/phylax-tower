// Mock mode has no recorded audio, so each clip gets a short placeholder tone (the same
// fallback the tower uses when voice is off). It is real audio served to a real <audio>
// element; nothing is drawn from it.

const cache = new Map<string, string>();

export function toneUrl(key: string, durationS: number, hz: number): string {
  const hit = cache.get(key);
  if (hit) return hit;
  const rate = 8000;
  const n = Math.max(1, Math.floor(rate * durationS));
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i));
  };
  str(0, 'RIFF');
  v.setUint32(4, 36 + n * 2, true);
  str(8, 'WAVE');
  str(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, 'data');
  v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    // Syllable-like pulses so it reads as "someone is transmitting", not as an alarm.
    const pulse = 0.55 + 0.45 * Math.sin(2 * Math.PI * 3.1 * t);
    const edge = Math.min(1, t / 0.04, (durationS - t) / 0.08);
    const s = Math.sin(2 * Math.PI * hz * t) * 0.16 * pulse * Math.max(0, edge);
    v.setInt16(44 + i * 2, Math.round(s * 32767), true);
  }
  const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  cache.set(key, url);
  return url;
}
