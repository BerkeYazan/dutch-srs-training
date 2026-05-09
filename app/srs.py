"""SM-2 spaced repetition logic, ported with Anki-style learning steps.

Why: Anki's algorithm is the standard. SM-2 with learning steps for new cards,
graduating intervals, and lapse handling matches what most learners already
experience. We reimplement here so the data and the schedule live inside our
SQLite database, not Anki's.

States and transitions:
- new        : never reviewed, sits in the new queue
- learning   : in the learning steps (1m, 10m). Graduates to review after passing all steps.
- review     : standard SM-2 with interval and ease.
- relearning : a review card that lapsed. Goes back through one short relearning step.

Grades:
1 again, 2 hard, 3 good, 4 easy.

References:
- Wozniak SM-2, supermemo.com
- Anki manual on intervals (https://docs.ankiweb.net/deck-options.html)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

State = Literal["new", "learning", "review", "relearning"]
Grade = Literal[1, 2, 3, 4]

# Learning steps in minutes for new cards. Match Anki defaults.
LEARNING_STEPS_MIN = [1, 10]
# After a lapse, one short relearning step before re-graduating.
RELEARNING_STEPS_MIN = [10]
# When a card graduates from learning, this is the first true interval.
GRADUATING_INTERVAL_DAYS = 1
EASY_GRADUATING_INTERVAL_DAYS = 4
# Lapse penalty multiplier on the previous interval.
LAPSE_INTERVAL_MULT = 0.0  # standard Anki resets to 0 then uses relearning step
# Bounds.
MIN_EASE = 1.3
MAX_INTERVAL_DAYS = 365 * 5


@dataclass(frozen=True)
class Card:
    state: State
    step: int
    ease: float
    interval_days: int
    repetitions: int
    lapses: int
    due_at: datetime
    last_reviewed: datetime | None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_card(due_at: datetime | None = None) -> Card:
    """Construct a fresh card in the 'new' state, due immediately by default."""
    return Card(
        state="new",
        step=0,
        ease=2.5,
        interval_days=0,
        repetitions=0,
        lapses=0,
        due_at=due_at or now_utc(),
        last_reviewed=None,
    )


def review(card: Card, grade: Grade, at: datetime | None = None) -> Card:
    """Apply a grade to a card and return the updated card.

    The function is pure: it does not touch the database. Persistence is the
    caller's job. Keeping the algorithm separable makes it easy to test and to
    swap in FSRS later.
    """
    at = at or now_utc()

    if card.state in ("new", "learning"):
        return _step_learning(card, grade, at, LEARNING_STEPS_MIN)
    if card.state == "relearning":
        return _step_learning(card, grade, at, RELEARNING_STEPS_MIN, after_lapse=True)
    if card.state == "review":
        return _step_review(card, grade, at)
    raise ValueError(f"unknown state {card.state}")


def _step_learning(
    card: Card,
    grade: Grade,
    at: datetime,
    steps_min: list[int],
    after_lapse: bool = False,
) -> Card:
    """Handle a card that is currently in learning or relearning.

    Again restarts the steps. Hard repeats the current step. Good advances by
    one step, graduating to review when the steps are exhausted. Easy graduates
    immediately with the easy graduating interval.
    """
    if grade == 1:  # again
        return replace(
            card,
            state="learning" if not after_lapse else "relearning",
            step=0,
            due_at=at + timedelta(minutes=steps_min[0]),
            last_reviewed=at,
        )
    if grade == 2:  # hard, repeat current step
        s = max(0, card.step)
        return replace(
            card,
            state="learning" if not after_lapse else "relearning",
            step=s,
            due_at=at + timedelta(minutes=steps_min[s]),
            last_reviewed=at,
        )
    if grade == 3:  # good, advance one step
        next_step = card.step + 1
        if next_step >= len(steps_min):
            # graduate
            return replace(
                card,
                state="review",
                step=0,
                interval_days=GRADUATING_INTERVAL_DAYS,
                repetitions=card.repetitions + 1,
                due_at=at + timedelta(days=GRADUATING_INTERVAL_DAYS),
                last_reviewed=at,
            )
        return replace(
            card,
            state="learning" if not after_lapse else "relearning",
            step=next_step,
            due_at=at + timedelta(minutes=steps_min[next_step]),
            last_reviewed=at,
        )
    if grade == 4:  # easy, graduate now with easy interval
        return replace(
            card,
            state="review",
            step=0,
            interval_days=EASY_GRADUATING_INTERVAL_DAYS,
            repetitions=card.repetitions + 1,
            due_at=at + timedelta(days=EASY_GRADUATING_INTERVAL_DAYS),
            last_reviewed=at,
        )
    raise ValueError(f"bad grade {grade}")


def _step_review(card: Card, grade: Grade, at: datetime) -> Card:
    """Handle a card that is in the standard review queue.

    SM-2 update of ease and interval, with lapse handling for grade=1.
    """
    if grade == 1:  # lapse
        return replace(
            card,
            state="relearning",
            step=0,
            ease=max(MIN_EASE, card.ease - 0.20),
            interval_days=int(round(card.interval_days * LAPSE_INTERVAL_MULT)),
            lapses=card.lapses + 1,
            due_at=at + timedelta(minutes=RELEARNING_STEPS_MIN[0]),
            last_reviewed=at,
        )

    # passing grades update ease and stretch the interval
    if grade == 2:  # hard
        new_ease = max(MIN_EASE, card.ease - 0.15)
        new_interval = max(card.interval_days + 1, int(round(card.interval_days * 1.2)))
    elif grade == 3:  # good
        new_ease = card.ease
        new_interval = max(card.interval_days + 1, int(round(card.interval_days * card.ease)))
    elif grade == 4:  # easy
        new_ease = card.ease + 0.15
        new_interval = max(card.interval_days + 1, int(round(card.interval_days * card.ease * 1.3)))
    else:
        raise ValueError(f"bad grade {grade}")

    new_interval = min(MAX_INTERVAL_DAYS, new_interval)

    return replace(
        card,
        state="review",
        step=0,
        ease=new_ease,
        interval_days=new_interval,
        repetitions=card.repetitions + 1,
        due_at=at + timedelta(days=new_interval),
        last_reviewed=at,
    )
