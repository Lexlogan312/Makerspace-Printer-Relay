"""Hypothesis settings for every property test.

No per-example time limit: the relay's tests also run on the Raspberry Pi, where a slow example is
the hardware, not a bug.
"""

from hypothesis import settings

settings.register_profile("relay", deadline=None)
settings.load_profile("relay")
