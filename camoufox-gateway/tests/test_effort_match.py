"""Pure-logic tests for the effort short-circuit (no browser needed).

Run: uv run python tests/test_effort_match.py
Proves set_effort can safely skip the fragile Ctrl+Shift+M menu dance when the
composer toolbar already shows the wanted effort — the fix for the headed-mode
45s composer-click hang.
"""
from cgw.chat import _effort_satisfied, _effort_target_label

CASES = [
    # (current_toolbar_label, requested_effort, expect_satisfied)
    ("Pro rozšířené", "pro", True),            # the bug: already selected -> skip
    ("Pro rozšířené", "pro-extended", True),
    ("Pro rozšířené", "pro-standard", False),  # wrong sub -> must still switch
    ("Pro Standardní", "pro", False),          # wrong sub -> must still switch
    ("Pro Standardní", "pro-standard", True),
    ("Vysoká", "high", True),
    ("Vysoká", "extended", False),
    ("Velmi vysoká", "high", False),           # prefix trap: Vysoká != Velmi vysoká
    ("Velmi vysoká", "extended", True),
    ("", "pro", False),                        # unknown label -> never short-circuit
    ("Pro rozšířené", "bogus", False),         # unknown effort -> never short-circuit
    ("Střední", "standard", True),
    ("Střední", "medium", True),
]


def main() -> None:
    assert _effort_target_label("pro") == "Pro rozšířené"
    assert _effort_target_label("high") == "Vysoká"
    assert _effort_target_label("bogus") is None
    failures = []
    for current, effort, expect in CASES:
        got = _effort_satisfied(current, effort)
        status = "ok" if got == expect else "FAIL"
        if got != expect:
            failures.append((current, effort, expect, got))
        print(f"[{status}] cur={current!r:18} effort={effort!r:14} -> {got} (want {expect})")
    if failures:
        raise SystemExit(f"\n{len(failures)} case(s) FAILED: {failures}")
    print(f"\nall {len(CASES)} cases passed")


if __name__ == "__main__":
    main()
