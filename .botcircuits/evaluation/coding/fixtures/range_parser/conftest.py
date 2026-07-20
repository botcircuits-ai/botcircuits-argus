"""Put the fixture root on sys.path so `from ranges import ...` resolves when
pytest runs from the sandbox copy."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
