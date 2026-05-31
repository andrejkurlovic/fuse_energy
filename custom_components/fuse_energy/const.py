"""Constants for Fuse Energy+ integration."""
from datetime import date

DOMAIN = "fuse_energy"

# Config entry keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SESSION_ID = "session_id"
CONF_DEVICE_ID = "device_id"
CONF_PREMISES_FID = "premises_fid"
CONF_AUTH_FLOW_TOKEN = "auth_flow_token"

API_BASE_URL = "https://api.fuseenergy.com"

# Proven from BuildConfig.java: VERSION_NAME=2.0.65, VERSION_CODE=542
_APP_VERSION = "2.0.65"
_APP_BUILD = "542"
USER_AGENT = f"Mobile/Android/{_APP_VERSION}/{_APP_BUILD}"

# supply_type values confirmed from live API response
# (NOT "ELECTRICITY_IMPORT" — real value is "ELEC_IMPORT")
SUPPLY_ELECTRICITY = "ELEC_IMPORT"
SUPPLY_GAS = "GAS"

# Long-term statistics IDs (source:name format required by HA)
STAT_ELECTRICITY_KWH = f"{DOMAIN}:electricity_import_kwh"
STAT_ELECTRICITY_COST = f"{DOMAIN}:electricity_import_cost"
STAT_GAS_KWH = f"{DOMAIN}:gas_import_kwh"
STAT_GAS_COST = f"{DOMAIN}:gas_import_cost"

# Service name
SERVICE_IMPORT_HISTORY = "import_history"

# Earliest date for which Fuse API has REALISED data (proven from live testing)
SWITCH_IN_DATE = date(2025, 9, 9)
