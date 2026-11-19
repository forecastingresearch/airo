"""Forecast eligibility for externally supplied question sets."""


def is_resolved(question):
    """Require an explicit resolution decision; a close date or candidate is insufficient."""
    return question.get("resolved") is True or question.get("status") == "resolved"
