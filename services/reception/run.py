"""Trusted isolated-mode entry point: python -I -u run.py [options]."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from reception_service.__main__ import main

if __name__ == '__main__':
    main()
