"""History retention policy shared by the sample stores (see app/store/samples.py).

Each athlete keeps at most MAX_HISTORY_SAMPLES of their newest samples per stream, persisted in the
local SQLite database. That is a sample count, not a time window: at one sample per second it is
five minutes, at ten per second it is thirty seconds. This milestone does not store full matches
or seasons.
"""

# Newest samples kept per athlete per stream. A count, not a duration (see module docstring).
MAX_HISTORY_SAMPLES = 300
