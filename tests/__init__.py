import os
import sys

# Run the tests against the source tree: `python -m unittest` from the repo root, nothing installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
