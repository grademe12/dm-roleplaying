"""Shared cap for new reply/initiate parts and one serialized send batch."""

# This is a maximum, never a requirement to pad a reply to five bubbles.
# Previously persisted longer groups can still drain in batches of this size.
MAX_PARTS = 5
