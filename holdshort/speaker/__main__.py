"""python -m holdshort.speaker [--db NAME]"""

import sys

from holdshort.agents._stream import main
from holdshort.speaker.core import Speaker

if __name__ == "__main__":
    sys.exit(main(Speaker))
