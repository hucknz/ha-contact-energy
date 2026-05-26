"""Contact Energy Gas Sensor for HA Gas Integration."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
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


class ContactEnergyGasSensor(BaseSensor, RestoreEntity):
    """Contact Energy Gas Sensor for Home Assistant Gas Integration.
    
    This sensor creates statistics entries with historical timestamps for use with
    the Home Assistant Energy dashboard. The sensor state shows the cumulative m³
    for monitoring purposes, but the Energy dashboard should be configured to read
    from the statistics (not this sensor) to properly display historical data on
    the correct dates.
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
            UnitOfVolume.CUBIC_METERS,
            "mdi:fire",
            state_class=SensorStateClass.TOTAL_INCREASING,
            device_class=SensorDeviceClass.GAS,
        )

        self._config_entry = config_entry
        self._cumulative_m3 = 0.0
        self._last_day_usage = 0.0
        self._first_run_complete = False
        self._fetched_days: set = set()  # Track days to prevent duplicates
        self._force_initial_backfill = True

    @property
    def state(self) -> Optional[str]:
        """Return the state."""
        if self._cumulative_m3 is not None:
            return round(self._cumulative_m3, 3)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        return {
            "last_update_timestamp": self._last_update.isoformat()
            if self._last_update
            else None,
            "data_lag_days": API_DATA_LAG_DAYS,
            "last_day_usage": round(self._last_day_usage, 3),
            "first_run_complete": self._first_run_complete,
            "fetched_days_count": len(self._fetched_days),
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await super().async_added_to_hass()

        # Try to restore state from previous session
        if state := await self.async_get_last_state():
            try:
                self._cumulative_m3 = float(state.state)
                _LOGGER.info(
                    "Restored cumulative m³ from previous session: %.3f m³",
                    self._cumulative_m3,
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Could not restore state from previous session, starting fresh"
                )
                self._cumulative_m3 = 0.0

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
            _LOGGER.debug("Beginning gas sensor update")

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
                "Error updating gas sensor (attempt %d): %s",
                self._update_failures,
                str(error),
            )

            # If we've failed multiple times, try to re-login
            if self._update_failures >= 3:
                _LOGGER.warning("Multiple update failures, attempting to re-login")
                await self._api.async_login()

    async def _async_perform_backfill(self, now: datetime) -> None:
        """Perform initial backfill of historical data.

        Fetches many days of history on first setup to populate cumulative total.
        Gas API returns daily readings, not hourly.
        """
        backfill_days = self._config_entry.data.get(
            CONF_INITIAL_BACKFILL_DAYS, 30
        )
        _LOGGER.info("Starting initial backfill of %d days for gas", backfill_days)

        # Calculate the start date: today - backfill_days - API_DATA_LAG_DAYS
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today - timedelta(
            days=backfill_days + API_DATA_LAG_DAYS
        )
        end_date = today - timedelta(days=API_DATA_LAG_DAYS)

        current_date = start_date
        consecutive_empty_days = 0

        while current_date <= end_date:
            _LOGGER.debug("Backfilling gas data for %s", current_date.strftime("%Y-%m-%d"))

            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                str(current_date.day),
                interval="monthly"
            )

            if not response:
                consecutive_empty_days += 1
                _LOGGER.debug(
                    "No gas data for %s (empty count: %d)",
                    current_date.strftime("%Y-%m-%d"),
                    consecutive_empty_days,
                )

                # Stop early if we've hit 2 consecutive empty days
                if consecutive_empty_days >= 2:
                    _LOGGER.debug("Found 2 consecutive empty days, stopping gas backfill")
                    break
            else:
                consecutive_empty_days = 0
                await self._async_process_gas_data(response, current_date)

            current_date += timedelta(days=1)

        _LOGGER.info("Gas backfill complete. Cumulative m³: %.3f", self._cumulative_m3)

    async def _async_perform_incremental_update(self, now: datetime) -> None:
        """Perform incremental update of recent gas data.

        Fetches last N days to capture any new data while respecting API lag.
        """
        lookback_days = self._config_entry.data.get(CONF_DAILY_LOOKBACK_DAYS, 4)
        _LOGGER.debug("Performing incremental gas update with %d-day lookback", lookback_days)

        # Calculate date range
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
                interval="monthly"
            )

            if not response:
                consecutive_empty_days += 1
                _LOGGER.debug(
                    "No gas data for %s (incremental)",
                    current_date.strftime("%Y-%m-%d"),
                )

                # Stop early if we've hit 2 consecutive empty days
                if consecutive_empty_days >= 2:
                    _LOGGER.debug(
                        "Found 2 consecutive empty days, stopping incremental gas update"
                    )
                    break
            else:
                consecutive_empty_days = 0
                await self._async_process_gas_data(response, current_date)

            current_date += timedelta(days=1)

        _LOGGER.debug(
            "Incremental gas update complete. Cumulative m³: %.3f",
            self._cumulative_m3,
        )

    async def _async_process_gas_data(self, response: list, date: datetime) -> None:
        """Process gas usage data and update cumulative total.

        Gas API returns daily readings, so we process the daily total.
        Creates statistics entries with proper historical timestamps.
        """
        if not response:
            return

        daily_m3 = 0.0
        daily_count = 0
        statistics_data = []

        for point in response:
            if not point.get("value"):
                continue

            # Gas data appears to be daily readings
            daily_m3 += float(point["value"])
            daily_count += 1

        if daily_count > 0:
            day_key = date.strftime("%Y-%m-%d")
            
            # Skip if we've already processed this day
            if day_key in self._fetched_days:
                _LOGGER.debug("Skipping duplicate day: %s", day_key)
                return

            self._cumulative_m3 += daily_m3
            self._fetched_days.add(day_key)
            self._last_day_usage = daily_m3

            # Create a statistics entry with the date
            stat_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
            statistics_data.append(
                StatisticData(
                    start=stat_start,
                    sum=self._cumulative_m3,
                )
            )

            _LOGGER.debug(
                "Added day %s: %.3f m³ (cumulative: %.3f)",
                day_key,
                daily_m3,
                self._cumulative_m3,
            )

        # Create statistics with proper historical timestamps
        if statistics_data:
            _LOGGER.debug("Creating %d gas statistics entries", len(statistics_data))
            try:
                icp = self._icp
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"Contact Energy - Gas ({icp})",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:gas_consumption",
                    unit_of_measurement=UnitOfVolume.CUBIC_METERS,
                )
                async_add_external_statistics(self.hass, metadata, statistics_data)
                _LOGGER.debug("Gas statistics entries created successfully")
            except Exception as error:
                _LOGGER.error("Failed to create gas statistics entries: %s", error)
