# ======================================
# SCADA FLOW EDGE KHAZAR CONFIGURATION
# ======================================

# VPS SCADA FLOW SERVER
SERVER_URL = "https://scada.khze.org"

# EDGE DEVICE ID
# This is the identity used by the server to return the Flow
# belonging to this Edge/PLC configuration.
PLC_ID = 2

# How often Edge checks the per-tag scheduler
# This is NOT the tag interval.
# Actual tag intervals come from Flow Editor.
SCHEDULER_TICK = 0.1

# How often Edge refreshes Flow configuration
FLOW_REFRESH_INTERVAL = 30
