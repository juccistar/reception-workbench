"""Source-only compatibility shim for synthetic tests; excluded from releases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'test_support'))
from reception_test_support.demo import *  # noqa: F401,F403
