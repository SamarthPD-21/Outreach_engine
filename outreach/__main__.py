"""`python -m outreach` runs the same CLI as pipeline.py."""

import sys

from .config import ROOT

sys.path.insert(0, str(ROOT))

from pipeline import main  # noqa: E402

main()
