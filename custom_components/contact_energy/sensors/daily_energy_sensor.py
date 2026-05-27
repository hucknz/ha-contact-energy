"""Contact Energy Daily Energy Sensor."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from custom_components.contact_energy.sensors.base_sensor import BaseSensor
from custom_components.contact_energy.const import (
    CONF_INITIAL_BACKFILL_DAYS,
    CONF_DAILY_LOOKBACK_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Data lag from Contact Energy API: 3 days
API_DATA_LAG_DAYS = 3


class ContactEnergyDailyEnergySensor(BaseSensor, RestoreEntity):
    """Contact Energy Daily Energy Sensor.
    
    Shows energy consumed for the current day (or most recent day with data).
    Resets to 0 at midnight. Daily totals are backfilled once per day.
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
            UnitOfEnergy.KILO_WATT_HOUR,
            "mdi:lightning-bolt",
            state_class=SensorStateClass.TOTAL,
            device_class=SensorDeviceClass.ENERGY,
        )

        self._config_entry = config_entry
        self._daily_kwh = 0.0
        self._current_date = None
        self._most_recent_date = None  # Track most recent day with data for state display
        self._first_run_complete = False
        self._force_initial_backfill = True
        self._last_daily_update = None
        self._cumulative_stat_sum = 0.0  # Running cumulative total for statistics

    @property
    def state(self) -> Optional[str]:
        """Return the state."""
        if self._daily_kwh is not None:
            return str(round(self._daily_kwh, 3))
        return None

    @property
    def last_reset(self) -> Optional[datetime]:
        """Return the last reset time (midnight of current day)."""
        if self._current_date:
            return self._current_date
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        return {
            "last_update_timestamp": self._last_update.isoformat()
            if self._last_update
            else None,
            "data_lag_days": API_DATA_LAG_DAYS,
            "current_date": self._current_date.isoformat() if self._current_date else None,
            "first_run_complete": self._first_run_complete,
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await super().async_added_to_hass()

        # Try to restore state from previous session
        if state := await self.async_get_last_state():
            try:
                self._daily_kwh = float(state.state)
                _LOGGER.debug(
                    "Restored daily kWh from previous session: %.3f kWh",
                    self._daily_kwh,
                )
            except (ValueError, TypeError):
                _LOGGER.debug(
                    "Could not restore state from previous session, starting fresh"
                )
                self._daily_kwh = 0.0

        # Set current date
        self._current_date = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    async def async_update(self) -> None:
        """Update the sensor once per day."""
        now = datetime.now()

        # Check if we need to reset for a new day
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._current_date and self._current_date < today:
            _LOGGER.info(
                "Day changed, resetting daily energy from %.3f kWh to 0.0 kWh",
                self._daily_kwh,
            )
            self._daily_kwh = 0.0
            self._current_date = today

        # Determine if we should update today
        # Only update once per day, around midnight or at a specific hour
        if self._last_daily_update:
            time_since_update = now - self._last_daily_update
            if time_since_update < timedelta(hours=23):
                _LOGGER.debug(
                    "Skipping daily update (last update %.1f hours ago)",
                    time_since_update.total_seconds() / 3600,
                )
                return

        try:
            _LOGGER.debug("Beginning daily energy update")

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

            self._last_daily_update = now
            self._last_update = now
            self._update_failures = 0

        except Exception as error:
            self._update_failures += 1
            _LOGGER.error(
                "Error updating daily sensor (attempt %d): %s",
                self._update_failures,
                str(error),
            )

            # If we've failed multiple times, try to re-login
            if self._update_failures >= 3:
                _LOGGER.warning("Multiple update failures, attempting to re-login")
                await self._api.async_login()

    async def _async_perform_backfill(self, now: datetime) -> None:
        """Perform initial backfill of daily totals."""
        backfill_days = self._config_entry.data.get(
            "initial_backfill_days", 30
        )
        _LOGGER.info("Starting initial backfill of %d days for daily energy", backfill_days)

        # Calculate date range
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today - timedelta(
            days=backfill_days + API_DATA_LAG_DAYS
        )
        end_date = today - timedelta(days=API_DATA_LAG_DAYS)

        current_date = start_date
        consecutive_empty_days = 0
        self._cumulative_stat_sum = 0.0  # Reset cumulative sum at start of each backfill

        while current_date <= end_date:
            _LOGGER.debug("Backfilling daily total for %s", current_date.strftime("%Y-%m-%d"))

            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                str(current_date.day),
                interval="hourly"
            )

            if not response:
                consecutive_empty_days += 1
                _LOGGER.debug(
                    "No hourly data for %s (empty count: %d)",
                    current_date.strftime("%Y-%m-%d"),
                    consecutive_empty_days,
                )

                # Stop early if we've hit 2 consecutive empty days
                if consecutive_empty_days >= 2:
                    _LOGGER.debug("Found 2 consecutive empty days, stopping backfill")
                    break
            else:
                consecutive_empty_days = 0
                await self._async_process_daily_total(response, current_date)

            current_date += timedelta(days=1)

        _LOGGER.info("Backfill complete. Current daily kWh: %.3f", self._daily_kwh)

    async def _async_perform_incremental_update(self, now: datetime) -> None:
        """Perform incremental update for recent days (catch-up for days after backfill).
        
        Fetches the last `lookback_days` of data that are available (accounting for API lag).
        This ensures we catch daily data as it becomes available without waiting for the next backfill.
        """
        lookback_days = self._config_entry.data.get("daily_lookback_days", 4)
        _LOGGER.debug("Performing incremental daily update (lookback: %d days)", lookback_days)

        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Fetch data from lookback window up to the most recent available day
        # Most recent available = today - lag_days (data from 3 days ago)
        end_date = today - timedelta(days=API_DATA_LAG_DAYS)
        start_date = end_date - timedelta(days=lookback_days)
        
        _LOGGER.debug(
            "Incremental update range: %s to %s (with %d-day lag)",
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            API_DATA_LAG_DAYS,
        )

        current_date = start_date
        updates_attempted = 0
        updates_succeeded = 0
        
        while current_date <= end_date:
            updates_attempted += 1
            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                str(current_date.day),
                interval="hourly"
            )

            if response:
                await self._async_process_daily_total(response, current_date)
                updates_succeeded += 1
            
            current_date += timedelta(days=1)
        
        _LOGGER.debug(
            "Incremental daily update complete. %d/%d days updated. Current daily kWh: %.3f",
            updates_succeeded,
            updates_attempted,
            self._daily_kwh,
        )

    async def _async_process_daily_total(self, response: list, date: datetime) -> None:
        """Calculate and store daily total from hourly data.
        
        Creates both:
        - Sensor state update (only for current day)
        - Statistics entry (for all days, used by Energy dashboard)
        
        Args:
            response: List of hourly data points
            date: The date for which we're calculating the daily total
        """
        if not response:
            return

        daily_total = 0.0
        hourly_count = 0

        for point in response:
            if not point.get("value"):
                continue

            # Only count paid energy (free energy has offpeakValue != "0.00")
            if point.get("offpeakValue", "0.00") == "0.00":
                kwh_value = float(point["value"])
                daily_total += kwh_value
                hourly_count += 1

        if hourly_count > 0:
            _LOGGER.debug(
                "Daily total for %s: %.3f kWh (%d hours)",
                date.strftime("%Y-%m-%d"),
                daily_total,
                hourly_count,
            )
            
            # Always update state with the most recent day's data
            # Due to 3-day API lag, "today" never has data, so use most recent processed day
            self._daily_kwh = daily_total
            self._most_recent_date = date
            
            # Create statistics entry for all days (both past and current)
            # This allows the Energy dashboard to show historical daily totals
            # with proper timestamps even though sensor state only shows today
            try:
                icp = self._icp
                # Use timezone-aware midnight of the target date as statistic start time
                # datetime.now() is naive, so convert to local time first
                stat_start = dt_util.as_local(
                    date.replace(hour=0, minute=0, second=0, microsecond=0)
                )
                
                self._cumulative_stat_sum += daily_total

                statistics_data = [
                    StatisticData(
                        start=stat_start,
                        sum=self._cumulative_stat_sum,
                    )
                ]
                
                # Use stable ID so all daily data points go into one statistics series
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"Contact Energy - Daily Electricity ({icp})",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:daily_energy_consumption",
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                )
                
                async_add_external_statistics(self.hass, metadata, statistics_data)
                _LOGGER.debug(
                    "Created statistics entry for daily energy on %s: %.3f kWh",
                    date.strftime("%Y-%m-%d"),
                    daily_total,
                )
            except Exception as error:
                _LOGGER.error(
                    "Failed to create statistics for daily energy on %s: %s",
                    date.strftime("%Y-%m-%d"),
                    str(error),
                )
