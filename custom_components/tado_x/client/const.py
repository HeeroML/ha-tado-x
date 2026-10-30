"""Constants of the tado X cloud API."""

from datetime import timedelta
from typing import Final

LOGIN_URL: Final = "https://login.tado.com/oauth2"
DEVICE_AUTHORIZE_URL: Final = f"{LOGIN_URL}/device_authorize"
TOKEN_URL: Final = f"{LOGIN_URL}/token"
MY_API_URL: Final = "https://my.tado.com/api/v2"
HOPS_API_URL: Final = "https://hops.tado.com"
MINDER_API_URL: Final = "https://minder.tado.com/v1"
ENERGY_IQ_API_URL: Final = "https://energy-insights.tado.com/api"

CLIENT_ID: Final = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
SCOPE: Final = "offline_access"
GRANT_TYPE_DEVICE_CODE: Final = "urn:ietf:params:oauth:grant-type:device_code"
GRANT_TYPE_REFRESH_TOKEN: Final = "refresh_token"

REQUEST_TIMEOUT: Final = 20.0
CONNECT_TIMEOUT: Final = 10.0
REFRESH_MARGIN: Final = timedelta(seconds=60)
DEFAULT_TOKEN_LIFETIME: Final = timedelta(seconds=600)

DEVICE_FLOW_MIN_INTERVAL: Final = timedelta(seconds=5)
DEVICE_FLOW_SLOW_DOWN_STEP: Final = timedelta(seconds=5)
DEFAULT_DEVICE_CODE_LIFETIME: Final = timedelta(seconds=300)

MIN_TIMER_DURATION: Final = timedelta(seconds=60)
MAX_TIMER_DURATION: Final = timedelta(hours=24)
