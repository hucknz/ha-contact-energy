"""Contact Energy API client."""
import asyncio
import logging
from typing import Any, Optional
from datetime import datetime, timedelta
import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

class ContactEnergyApi:
    """Async Contact Energy API client."""

    def __init__(self, hass: HomeAssistant, email: str, password: str, account_id: str = None, contract_id: str = None):
        """Initialize the API."""
        self._api_token = ""
        self._contractId = contract_id
        self._accountId = account_id
        self._url_base = "https://api.contact-digital-prod.net"
        self._api_key = "z840P4lQCH9TqcjC9L2pP157DZcZJMcr5tVQCvyx"
        self._email = email
        self._password = password
        self._session = async_get_clientsession(hass)
        self._account_cache = None
        self._account_cache_timestamp = None
        self._account_cache_duration = timedelta(minutes=15)
        self._account_request = None
        self._login_lock = asyncio.Lock()
        self._account_lock = asyncio.Lock()
        
        # Usage data caching: cache full-day responses to avoid re-fetching
        self._usage_cache = {}  # {date_str: response_data}
        self._usage_cache_duration = timedelta(hours=1)

    def _get_headers(self, include_token: bool = True) -> dict:
        """Get headers for API requests."""
        headers = {"x-api-key": self._api_key}
        if include_token and self._api_token:
            headers["session"] = self._api_token
        return headers

    async def _async_request(self, method: str, url: str, **kwargs) -> Any:
        """Make an async request with timeout and error handling."""
        try:
            async with async_timeout.timeout(30):
                async with self._session.request(method, url, **kwargs) as response:
                    _LOGGER.debug("%s response status: %s", url, response.status)
                    # response_text = await response.text()
                    # _LOGGER.debug("%s response body: %s", url, response_text)
                    
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 401:
                        self._api_token = ""  # Clear invalid token
                        raise InvalidAuth
                    return None
                    
        except asyncio.TimeoutError as error:
            _LOGGER.error("Timeout during request to %s: %s", url, error)
            raise CannotConnect from error
        except aiohttp.ClientError as error:
            _LOGGER.error("Error connecting to %s: %s", url, error)
            raise CannotConnect from error
        except Exception as error:
            _LOGGER.exception("Unexpected error during request to %s: %s", url, error)
            raise UnknownError from error

    async def async_login(self) -> bool:
        """Login to the Contact Energy API."""
        async with self._login_lock:
            # If we got a token while waiting for the lock, we're good
            if self._api_token:
                return True

            _LOGGER.debug("Attempting login for email: %s", self._email)
            
            data = {"username": self._email, "password": self._password}
            
            try:
                result = await self._async_request(
                    "POST",
                    f"{self._url_base}/login/v2",
                    json=data,
                    headers=self._get_headers(include_token=False)
                )
                
                if result and "token" in result:
                    self._api_token = result["token"]
                    _LOGGER.debug("Login successful")
                    return True
                
                _LOGGER.error("Failed to login - invalid response format")
                return False
                
            except Exception as error:
                _LOGGER.exception("Login failed: %s", error)
                return False

    async def async_get_accounts(self) -> dict:
        """Get accounts information with caching and request deduplication."""
        now = datetime.now()

        # Check if cache is valid
        if (self._account_cache and self._account_cache_timestamp and 
            (now - self._account_cache_timestamp) < self._account_cache_duration):
            _LOGGER.debug("Using cached account data")
            return self._account_cache

        async with self._account_lock:
            # After acquiring lock, check cache again
            if (self._account_cache and self._account_cache_timestamp and 
                (now - self._account_cache_timestamp) < self._account_cache_duration):
                _LOGGER.debug("Using account data from cache")
                return self._account_cache

            # Check if we need to login
            if not self._api_token:
                _LOGGER.debug("No API token, attempting login")
                if not await self.async_login():
                    raise InvalidAuth("Failed to login")

            try:
                _LOGGER.debug("Fetching fresh account data")
                data = await self._async_request(
                    "GET",
                    f"{self._url_base}/accounts/v2",
                    headers=self._get_headers()
                )

                if data:
                    self._account_cache = data
                    self._account_cache_timestamp = now
                    _LOGGER.debug("Successfully fetched and cached account data")
                    return data

                _LOGGER.error("No account data received from API")
                raise UnknownError("No data received from API")

            except InvalidAuth:
                self._account_cache = None
                self._account_cache_timestamp = None
                self._api_token = ""
                
                # Try one more time with fresh login
                if await self.async_login():
                    data = await self._async_request(
                        "GET",
                        f"{self._url_base}/accounts/v2",
                        headers=self._get_headers()
                    )
                    if data:
                        self._account_cache = data
                        self._account_cache_timestamp = now
                        return data
                raise

    async def get_usage(self, year: str, month: str, day: str, interval: str = "daily") -> Optional[list]:
        """Get usage data for a specific date with caching.
        
        Args:
            year: Year as string
            month: Month as string (no leading zero required)
            day: Day as string (no leading zero required)
            interval: Interval type - "daily" for electricity, "monthly" for gas
            
        Returns:
            List of usage data points, or None if not available
        """
        date_str = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        cache_key = f"{date_str}_{interval}"
        now = datetime.now()

        # Check cache first
        if cache_key in self._usage_cache:
            cached_entry = self._usage_cache[cache_key]
            cache_age = now - cached_entry["timestamp"]
            if cache_age < self._usage_cache_duration:
                _LOGGER.debug("Using cached usage data for %s (interval: %s, age: %s)", date_str, interval, cache_age)
                return cached_entry["data"]
            else:
                # Cache expired, remove it
                del self._usage_cache[cache_key]

        if not self._api_token and not await self.async_login():
            _LOGGER.error("Failed to login when fetching usage data")
            return None

        if not self._contractId or not self._accountId:
            _LOGGER.error("Missing contract ID or account ID")
            return None

        url = f"{self._url_base}/usage/v2/{self._contractId}?ba={self._accountId}&interval={interval}&from={date_str}&to={date_str}"
        
        _LOGGER.debug("Fetching usage data for %s (interval: %s) from API", date_str, interval)
        
        try:
            data = await self._async_request(
                "GET",
                url,
                headers=self._get_headers()
            )
            
            if data:
                _LOGGER.debug("Successfully fetched %d data points for %s (interval: %s)", len(data), date_str, interval)
                # Cache the response
                self._usage_cache[cache_key] = {
                    "data": data,
                    "timestamp": now
                }
                return data
            
            _LOGGER.debug("No usage data available for %s (interval: %s)", date_str, interval)
            return None
            
        except InvalidAuth:
            _LOGGER.debug("Token expired, attempting to login again")
            if await self.async_login():
                # Retry the request with new token
                return await self.get_usage(year, month, day, interval)
            return None
        except Exception as error:
            _LOGGER.error("Failed to fetch usage data for %s (interval: %s): %s", date_str, interval, error)
            return None
        
class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""

class UnknownError(HomeAssistantError):
    """Error to indicate an unknown error occurred."""