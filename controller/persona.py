"""Persona-level switches. Each persona repository sets its own values here."""

NAME = '유진'
# The clock alone forces quiet before this hour. None: only real sleep/attention decides.
NIGHT_QUIET_UNTIL_HOUR = None
# Daily wake-up greeting tied to the recorded main sleep (morning_contact.py).
MORNING_CONTACT = False
# Messenger for rows without an explicit channel and for initiate without one.
DEFAULT_CHANNEL = 'slack'
# Message-only revisit for initiate judgment (proactive_check.py). Not a daily count cap.
PROACTIVE_CHECK_INTERVAL_MS = 45 * 60_000
