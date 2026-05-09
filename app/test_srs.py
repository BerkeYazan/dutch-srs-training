"""Sanity tests for the SM-2 logic in srs.py.

Not a full pytest suite, just enough to verify intervals and state transitions
behave the way Anki users expect. Run directly:
    python3 app/test_srs.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from srs import new_card, review


def at(minutes: int = 0, days: int = 0) -> datetime:
    base = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes, days=days)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok  {msg}")


def test_new_card_good_path() -> None:
    """New -> learning step 1 -> learning step 2 -> review with 1 day interval."""
    print("test_new_card_good_path")
    c = new_card(due_at=at())
    expect(c.state == "new", "starts new")

    c = review(c, 3, at=at())
    expect(c.state == "learning", "good moves to learning")
    expect(c.step == 1, "good advances step")
    # Due in 10 minutes per LEARNING_STEPS_MIN[1]
    expect((c.due_at - at()).total_seconds() == 10 * 60, "step 1 due in 10 min")

    c = review(c, 3, at=at(minutes=10))
    expect(c.state == "review", "good graduates to review")
    expect(c.interval_days == 1, "graduating interval is 1 day")


def test_again_resets() -> None:
    print("test_again_resets")
    c = new_card(due_at=at())
    c = review(c, 3, at=at())  # learning step 1
    c = review(c, 1, at=at(minutes=10))  # again
    expect(c.state == "learning", "again keeps learning state")
    expect(c.step == 0, "again resets step to 0")


def test_review_intervals() -> None:
    """After graduating, good doubles via ease=2.5."""
    print("test_review_intervals")
    c = new_card(due_at=at())
    c = review(c, 4, at=at())  # easy graduates immediately
    expect(c.state == "review", "easy graduates")
    expect(c.interval_days == 4, "easy graduating interval is 4 days")
    prev_int = c.interval_days
    c = review(c, 3, at=at(days=4))
    # 4 * 2.5 = 10, allowing rounding
    expect(c.interval_days >= 10, f"good after 4d gives ~10d, got {c.interval_days}")


def test_lapse_then_relearn() -> None:
    print("test_lapse_then_relearn")
    c = new_card(due_at=at())
    c = review(c, 4, at=at())  # ->review
    c = review(c, 3, at=at(days=4))  # ->review longer
    prev_ease = c.ease
    c = review(c, 1, at=at(days=14))
    expect(c.state == "relearning", "lapse moves to relearning")
    expect(c.lapses == 1, "lapse counter increments")
    expect(c.ease < prev_ease, "ease drops after lapse")
    # one more good in relearning graduates back to review
    c = review(c, 3, at=c.due_at)
    expect(c.state == "review", "relearning + good returns to review")


def test_min_ease() -> None:
    print("test_min_ease")
    c = new_card(due_at=at())
    c = review(c, 4, at=at())  # review
    for i in range(20):
        c = review(c, 1, at=c.due_at)
    expect(c.ease >= 1.3, f"ease floored at 1.3, got {c.ease}")


if __name__ == "__main__":
    test_new_card_good_path()
    test_again_resets()
    test_review_intervals()
    test_lapse_then_relearn()
    test_min_ease()
    print("\nall good")
