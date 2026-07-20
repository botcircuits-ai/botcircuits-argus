"""Put the fixture root on sys.path so `from pricing import ...` resolves when
pytest is run from the sandbox copy of this fixture."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
