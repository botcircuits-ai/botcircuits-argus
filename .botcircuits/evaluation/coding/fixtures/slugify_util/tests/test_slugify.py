"""Target behavior — starts RED, a correct slugify() turns it GREEN."""
from utils import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_trims_and_collapses():
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"


def test_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_empty():
    assert slugify("") == ""
