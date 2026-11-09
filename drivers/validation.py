"""
drivers/validation.py
~~~~~~~~~~~~~~~~~~~~~

Automatic document validation.

This is a deliberately inert default implementation: the real AI agent is
plugged in later. It always returns ``manual_review`` so nothing is approved
or rejected without a human decision.
"""


def validate_driver_documents(profile) -> dict:
    """
    Return an automatic decision for a driver profile.

    Shape::

        {"decision": "approve" | "reject" | "manual_review",
         "reasons": ["short", "texts"],
         "confidence": 0.0}

    The default implementation never decides on its own.
    """
    return {
        "decision": "manual_review",
        "reasons": ["Automatic document validation is not configured."],
        "confidence": 0.0,
    }
