"""python -m holdshort.state [--db NAME]"""

import sys

from holdshort.agents._stream import main
from holdshort.state.worker import StateWorker

if __name__ == "__main__":
    sys.exit(main(StateWorker))
