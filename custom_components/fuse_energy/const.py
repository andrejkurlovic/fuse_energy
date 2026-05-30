"""Constants for Fuse Energy+ integration."""

DOMAIN = "fuse_energy"

# Config entry keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SESSION_ID = "session_id"    # persistent UUID per installation — Session-Id header
CONF_DEVICE_ID = "device_id"     # persistent UUID per installation — Device-Id header
CONF_PREMISES_FID = "premises_fid"
CONF_AUTH_FLOW_TOKEN = "auth_flow_token"  # ephemeral — used only during config flow

# API
API_BASE_URL = "https://api.fuseenergy.com"

# Proven from BuildConfig.java: VERSION_NAME=2.0.65, VERSION_CODE=542
_APP_VERSION = "2.0.65"
_APP_BUILD = "542"
USER_AGENT = f"Mobile/Android/{_APP_VERSION}/{_APP_BUILD}"

# SupplyType values from SupplyTypeNetwork.java
SUPPLY_ELECTRICITY = "ELECTRICITY_IMPORT"
SUPPLY_ELECTRICITY_EXPORT = "ELECTRICITY_EXPORT"
SUPPLY_GAS = "GAS"
