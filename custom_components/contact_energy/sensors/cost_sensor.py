"""Contact Energy Cost Sensor for tracking electricity costs."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import CURRENCY_DOLLAR
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.contact_energy.sensors.base_sensor import BaseSensor
from custom_components.contact_energy.const import (
    CONF_INITIAL_BACKFILL_DAYS,
    CONF_DAILY_LOOKBACK_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Data lag from Contact Energy API: 3 days
API_DATA_LAG_DAYS = 3


class ContactEnergyCostSensor(BaseSensor, RestoreEntity):
    """Contact Energy Cost Sensor for tracking cumulative electricity costs.
    
    This sensor creates statistics entries with historical timestamps for use with
    cost tracking. The sensor state shows the cumulative NZD spent for monitoring
    purposes, but the Energy dashboard could be configured to read from the 
    statistics to properly display historical cost data on the correct dates.
    """

    def __init__(
        self,
        hass,
        name,
        api,
        icp,
        config_entry,
    ):
        """Initialize the sensor."""
        super().__init__(
            hass,
            name,
            api,
            icp,
            CURRENCY_DOLLAR,
            "mdi:currency-usd",
            state_class=SensorStateClass.TOTAL_INCREASING,
            device_class=SensorDeviceClass.MONETARY,
        )

        self._config_entry = config_entry
        self._cumulative_nzd = 0.0
        self._last_hour_cost = 0.0
        self._first_run_complete = False
        self._fetched_hours: set = set()  # Track hour timestamps to prevent duplicates
        self._force_initial_backfill = True

    @property
    def state(self) -> Optional[str]:
        """Return the state."""
        if self._cumulative_nzd is not None:
            return round(self._cumulative_nzd, 3)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        return {
            "last_update_timestamp": self._last_update.isoformat()
            if self._last_update
            else None,
            "data_lag_days": API_DATA_LAG_DAYS,
            "last_hour_cost": round(self._last_hour_cost, 3),
            "first_run_complete": self._first_run_complete,
            "fetched_hours_count": len(self._fetched_hours),
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await super().async_added_to_hass()

        # Try to restore state from previous session
        if state := await self.async_get_last_state():
            try:
                self._cumulative_nzd = float(state.state)
                _LOGGER.info(
                    "Restored cumulative NZD from previous session: %.3f NZD",
                    self._cumulative_nzd,
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Could not restore state from previous session, starting fresh"
                )
                self._cumulative_nzd = 0.0

    async def async_update(self) -> None:
        """Update the sensor."""
        now = datetime.now()

        # Check if we need to force an update
        force_update = False
        if self._last_update:
            time_since_update = now - self._last_update
            if time_since_update > self._force_update_interval:
                _LOGGER.warning(
                    "More than 24 hours since last successful update, forcing update"
                )
                force_update = True
                self._update_failures = 0  # Reset failure count on forced update

        try:
            _LOGGER.debug("Beginning cost sensor update")

            # Check if API token is valid
            if not self._api._api_token:
                _LOGGER.debug("Not logged in, attempting login...")
                if not await self._api.async_login():
                    _LOGGER.error("Failed to login - check credentials")
                    self._update_failures += 1
                    return

            # Determine fetch strategy based on first-run status
            if self._force_initial_backfill or not self._first_run_complete:
                await self._async_perform_backfill(now)
                self._first_run_complete = True
                self._force_initial_backfill = False
            else:
                await self._async_perform_incremental_update(now)

            self._last_update = now
            self._update_failures = 0

        except Exception as error:
            self._update_failures += 1
            _LOGGER.error(
                "Error updating cost sensor (attempt %d): %s",
                self._update_failures,
                str(error),
            )

            # If we've failed multiple times, try to re-login
            if self._update_failures >= 3:
                _LOGGER.warning("Multiple update failures, attempting to re-login")
                await self._api.async_login()

    async def _async_perform_backfill(self, now: datetime) -> None:
        """Perform initial backfill of historical cost data."""
        backfill_days = self._config_entry.data.get(
            CONF_INITIAL_BACKFILL_DAYS, 30
        )
        _LOGGER.info("Starting initial backfill of %d days for cost", backfill_days)

        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today - timedelta(
            days=backfill_days + API_DATA_LAG_DAYS
        )
        end_date = today - timedelta(days=API_DATA_LAG_DAYS)

        current_date = start_date
        consecutive_empty_days = 0

        while current_date <= end_date:
            _LOGGER.debug("Backfilling cost data for %s", current_date.strftime("%Y-%m-%d"))

            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                str(current_date.day),
                interval="hourly"
            )

            if not response:
                consecutive_empty_days += 1
                _LOGGER.debug(
                    "No cost data for %s (empty count: %d)",
                    current_date.strftime("%Y-%m-%d"),
                    consecutive_empty_days,
                )

                if consecutive_empty_days >= 2:
                    _LOGGER.debug("Found 2 consecutive empty days, stopping cost backfill")
                    break
            else:
                consecutive_empty_days = 0
                await self._async_process_cost_data(response)

            current_date += timedelta(days=1)

        _LOGGER.info("Cost backfill complete. Cumulative NZD: %.3f", self._cumulative_nzd)

    async def _async_perform_incremental_update(self, now: datetime) -> None:
        """Perform incremental update of recent cost data."""
        lookback_days = self._config_entry.data.get(CONF_DAILY_LOOKBACK_DAYS, 4)
        _LOGGER.debug("Performing incremental cost update with %d-day lookback", lookback_days)

        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today - timedelta(days=lookback_days + API_DATA_LAG_DAYS)
        end_date = today - timedelta(days=API_DATA_LAG_DAYS)

        current_date = start_date
        consecutive_empty_days = 0

        while current_date <= end_date:
            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                str(current_date.day),
                interval="hourly"
            )

            if not response:
                consecutive_empty_days += 1
                _LOGGER.debug(
                    "No cost data for %s (incremental)",
                    current_date.strftime("%Y-%m-%d"),
                )

                if consecutive_empty_days >= 2:
                    _LOGGER.debug(
                        "Found 2 consecutive empty days, stopping incremental cost update"
                    )
                    break
            else:
                consecutive_empty_days = 0
                await self._async_process_cost_data(response)

            current_date += timedelta(days=1)

        _LOGGER.debug(
            "Incremental cost update complete. Cumulative NZD: %.3f",
            self._cumulative_nzd,
        )

    async def _async_process_cost_data(self, response: list) -> None:
        """Process cost data points and update cumulative total.

        De-duplicates against previously fetched hours to prevent double-counting.
        Only processes paid energy cost (dollarValue).
        Creates statistics entries with proper historical timestamps.
        """
        if not response:
            return

        hourly_nzd = 0.0
        added_hours = 0
        statistics_data = []

        for point in response:
            if not point.get("dollarValue"):
                continue

            # Parse the timestamp
            try:
                timestamp = datetime.strptime(
                    point["date"], "%Y-%m-%dT%H:%M:%S.%f%z"
                )
            except ValueError:
                _LOGGER.warning("Could not parse timestamp: %s", point["date"])
                continue

            # Use hour-level precision for de-duplication
            hour_key = timestamp.replace(minute=0, second=0, microsecond=0).isoformat()

            # Skip if we've already processed this hour
            if hour_key in self._fetched_hours:
                _LOGGER.debug("Skipping duplicate hour: %s", hour_key)
                continue

            # Only count paid energy cost (free energy has offpeakValue != "0.00")
            # Default "0.00" treats missing field as paid energy - assumes API always includes this field for valid readings
            if point.get("offpeakValue", "0.00") == "0.00":
                nzd_value = float(point["dollarValue"])
                hourly_nzd = nzd_value
                self._cumulative_nzd += nzd_value
                self._fetched_hours.add(hour_key)
                added_hours += 1
                
                # Create a statistics entry with the ACTUAL timestamp from the API
                statistics_data.append(
                    StatisticData(
                        start=timestamp,
                        sum=self._cumulative_nzd,
                    )
                )
                
                _LOGGER.debug(
                    "Added hour %s: NZD%.3f (cumulative: NZD%.3f)",
                    hour_key,
                    nzd_value,
                    self._cumulative_nzd,
                )

        self._last_hour_cost = hourly_nzd

        # Create statistics with proper historical timestamps
        if statistics_data:
            _LOGGER.debug("Creating %d cost statistics entries with historical timestamps", len(statistics_data))
            try:
                icp = self._icp
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"Contact Energy - Electricity Cost ({icp})",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:energy_cost",
                    unit_of_measurement=CURRENCY_DOLLAR,
                )
                async_add_external_statistics(self.hass, metadata, statistics_data)
                _LOGGER.debug("Cost statistics entries created successfully")
            except Exception as error:
                _LOGGER.error("Failed to create cost statistics entries: %s", error)

        if added_hours > 0:
            _LOGGER.debug("Processed %d new hours for cost", added_hours)
