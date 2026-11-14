"""Isolated six-metric benchmark; see performance_checker/README.md."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from performance_checker.paired import main
if __name__ == '__main__':
    main()
