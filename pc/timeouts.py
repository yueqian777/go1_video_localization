"""Independent safety clocks for network, manual input and navigation intent."""
NETWORK_TIMEOUT_S = 3.0
BROWSER_FRESHNESS_S = 3.0
UI_LEASE_S = .5
ACTION_QUEUE_S = .5
OWNER_LEASE_S = 1.0
SDK_STATE_TIMEOUT_MS = 150

# The receiver's MOVE TTL remains a lower-level 600 ms deadman.  These values
# describe the PC-side automatic navigation contract and must not be used as a
# substitute for the receiver TTL.
RECEIVER_MOVE_TTL_S = .6
NAVIGATION_INTENT_MAX_S = 2.0
NAVIGATION_TASK_LEASE_S = 3.0
NAVIGATION_RESUME_WINDOW_S = 5.0
NAVIGATION_SCHEDULER_PERIOD_S = .05
NAVIGATION_FEEDBACK_VERSION = 1
