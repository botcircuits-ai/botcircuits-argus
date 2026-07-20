"""Parse compact range strings into explicit lists of ints.

Currently supports closed ranges only: "1-5" -> [1, 2, 3, 4, 5], and single
values "7" -> [7].
"""


def parse_range(spec: str, *, cap: int = 100) -> list[int]:
    """Parse a range spec into a list of ints.

    Closed range: "2-4" -> [2, 3, 4]. Single value: "7" -> [7].
    """
    spec = spec.strip()
    if "-" in spec:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        return list(range(lo, hi + 1))
    return [int(spec)]
