"""Voice alert output service: speaks executed STOP decisions (ElevenLabs TTS).

    uv run python -m holdshort.voice_alert            # watch decisions, speak each executed STOP

- Watches ``decisions`` inserts with ``intent_action: "stop"`` and ``outcome: "executed"``.
- Builds a short radio phrase from the decision's intent ("Truck one, STOP. Hold short
  runway zero four. Air Canada eight six four six, traffic Truck one holding short runway
  zero four.") and, when VOICE_ENABLED is true and
  ELEVENLABS_API_KEY is set, synthesises it to ``logs/voice_alerts/<decision_id>.mp3``.
  The API serves that file at ``GET /voice/alerts/{decision_id}`` and the web UI plays it.
- With voice disabled it only logs the phrase (text mode). Read-only on MongoDB: this
  service never writes to the database (it is an output, not an agent).
- ``VOICE_ALERT_PLAY=1`` also plays the clip on the machine's own speakers (ffplay/mpv).
"""

from holdshort.voice_alert.core import (
    ALERT_DIR,
    VoiceAlert,
    alert_path,
    phrase,
    speak,
    spoken_callsign,
)

__all__ = ["ALERT_DIR", "VoiceAlert", "alert_path", "phrase", "speak", "spoken_callsign"]
