import sys

from holdshort.agents._stream import main
from holdshort.voice_alert import VoiceAlert

if __name__ == "__main__":
    sys.exit(main(VoiceAlert))
