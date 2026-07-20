# Money handling conventions

**All monetary math MUST be done in integer cents, never floating-point
dollars.** Floats accumulate rounding error and are forbidden for money.

- Public helpers accept dollar amounts (floats) at the boundary, but must
  convert to integer cents immediately (`round(dollars * 100)`), do all
  arithmetic in cents, and convert back to dollars only in the return value.
- This is why `add_amounts` must sum in cents: `add_amounts(0.10, 0.20)` has to
  return exactly `0.30`, and the ONLY reliable way is integer-cents math.

A fix that merely wraps the float result in `round(total, 2)` is NOT
acceptable — it papers over one case but keeps the forbidden float
accumulation. Follow the cents convention.
