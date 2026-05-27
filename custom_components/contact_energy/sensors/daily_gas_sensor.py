"""Contact Energy Daily Gas Sensor."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
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


class ContactEnergyDailyGasSensor(BaseSensor, RestoreEntity):
    """Contact Energy Daily Gas Sensor.
    
    Shows gas consumed for the current day (or most recent day with data).
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
            UnitOfVolume.CUBIC_METERS,
            "mdi:fire",
            state_class=SensorStateClass.TOTAL,
            device_class=SensorDeviceClass.GAS,
        )

        self._config_entry = config_entry
        self._daily_m3 = 0.0
        self._current_date = None
        self._most_recent_date = None  # Track most recent month with data for state display
        self._first_run_complete = False
        self._force_initial_backfill = True
        self._last_daily_update = None
        self._cumulative_stat_sum = 0.0  # Running cumulative total for statistics

    @property
    def state(self) -> Optional[str]:
        """Return the state."""
        if self._daily_m3 is not None:
            return str(round(self._daily_m3, 3))
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
                self._daily_m3 = float(state.state)
                _LOGGER.debug(
                    "Restored daily m³ from previous session: %.3f m³",
                    self._daily_m3,
                )
            except (ValueError, TypeError):
                _LOGGER.debug(
                    "Could not restore state from previous session, starting fresh"
                )
                self._daily_m3 = 0.0

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
                "Day changed, resetting daily gas from %.3f m³ to 0.0 m³",
                self._daily_m3,
            )
            self._daily_m3 = 0.0
            self._current_date = today

        # Determine if we should update today
        # Only update once per day, around midnight or at a specific hour
        if self._last_daily_update:
            time_since_update = now - self._last_daily_update
            if time_since_update < timedelta(hours=23):
                _LOGGER.debug(
                    "Skipping daily gas update (last update %.1f hours ago)",
                    time_since_update.total_seconds() / 3600,
                )
                return

        try:
            _LOGGER.debug("Beginning daily gas update")

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
                "Error updating daily gas sensor (attempt %d): %s",
                self._update_failures,
                str(error),
            )

            # If we've failed multiple times, try to re-login
            if self._update_failures >= 3:
                _LOGGER.warning("Multiple update failures, attempting to re-login")
                await self._api.async_login()

    async def _async_perform_backfill(self, now: datetime) -> None:
        """Perform initial backfill of monthly gas totals."""
        # Treat initial_backfill_days as months for gas (data is monthly only)
        backfill_months = self._config_entry.data.get(
            "initial_backfill_days", 12
        )
        _LOGGER.info("Starting initial backfill of %d months for gas", backfill_months)

        # Calculate start month: backfill_months ago, normalised to the 1st
        today_first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Subtract backfill_months from today_first
        start_month_num = today_first.month - backfill_months
        start_year = today_first.year
        while start_month_num <= 0:
            start_month_num += 12
            start_year -= 1
        start_date = today_first.replace(year=start_year, month=start_month_num)

        # End at the last completed month (not the current month)
        if today_first.month == 1:
            end_date = today_first.replace(year=today_first.year - 1, month=12)
        else:
            end_date = today_first.replace(month=today_first.month - 1)

        current_date = start_date
        consecutive_empty_months = 0
        self._cumulative_stat_sum = 0.0  # Reset cumulative sum at start of each backfill

        while current_date <= end_date:
            _LOGGER.debug(
                "Backfilling gas total for %s-%02d",
                current_date.year,
                current_date.month,
            )

            # Query the first day of the month with interval=monthly
            response = await self._api.get_usage(
                str(current_date.year),
                str(current_date.month),
                "1",
                interval="monthly",
            )

            if not response:
                consecutive_empty_months += 1
                _LOGGER.debug(
                    "No gas data for %s-%02d (empty count: %d)",
                    current_date.year,
                    current_date.month,
                    consecutive_empty_months,
                )
                if consecutive_empty_months >= 2:
                    _LOGGER.debug("Found 2 consecutive empty months, stopping gas backfill")
                    break
            else:
                consecutive_empty_months = 0
                await self._async_process_monthly_total(response, current_date)

            # Advance to next month
            if current_date.month == 12:
                current_date = current_date.replace(year=current_date.year + 1, month=1)
            else:
                current_date = current_date.replace(month=current_date.month + 1)

        _LOGGER.info("Gas backfill complete. Cumulative gas: %.3f m³", self._cumulative_stat_sum)

    async def _async_perform_incremental_update(self, now: datetime) -> None:
        """Perform incremental update - check if the previous month's data is available."""
        _LOGGER.debug("Performing incremental gas update")

        today_first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Previous completed month
        if today_first.month == 1:
            prev_month = today_first.replace(year=today_first.year - 1, month=12)
        else:
            prev_month = today_first.replace(month=today_first.month - 1)

        response = await self._api.get_usage(
            str(prev_month.year),
            str(prev_month.month),
            "1",
            interval="monthly",
        )

        if response:
            await self._async_process_monthly_total(response, prev_month)
            _LOGGER.debug(
                "Updated gas statistics for %s-%02d",
                prev_month.year,
                prev_month.month,
            )
        else:
            _LOGGER.debug(
                "No gas data available for %s-%02d",
                prev_month.year,
                prev_month.month,
            )

    async def _async_process_monthly_total(self, response: list, month_date: datetime) -> None:
        """Calculate and store monthly gas total.

        Creates both:
        - Sensor state update (most recent month's value)
        - Statistics entry (for Energy dashboard, cumulative sum per month)

        Args:
            response: List of data points returned by the monthly API
            month_date: The first day of the month for which we have data
        """
        if not response:
            return

        monthly_total = 0.0

        for point in response:
            if not point.get("value"):
                continue
            monthly_total += float(point["value"])

        if monthly_total > 0:
            _LOGGER.debug(
                "Monthly gas total for %s-%02d: %.3f m³",
                month_date.year,
                month_date.month,
                monthly_total,
            )

            self._daily_m3 = monthly_total
            self._most_recent_date = month_date

            try:
                icp = self._icp
                # Statistics entry at midnight on the 1st of each month
                stat_start = dt_util.as_local(
                    month_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                )

                self._cumulative_stat_sum += monthly_total

                statistics_data = [
                    StatisticData(
                        start=stat_start,
                        sum=self._cumulative_stat_sum,
                    )
                ]

                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"Contact Energy - Monthly Gas ({icp})",
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:monthly_gas_consumption",
                    unit_of_measurement=UnitOfVolume.CUBIC_METERS,
                )

                async_add_external_statistics(self.hass, metadata, statistics_data)
                _LOGGER.debug(
                    "Created statistics entry for gas month %s-%02d: %.3f m³ (cumulative: %.3f)",
                    month_date.year,
                    month_date.month,
                    monthly_total,
                    self._cumulative_stat_sum,
                )
            except Exception as error:
                _LOGGER.error(
                    "Failed to create statistics for gas month %s-%02d: %s",
                    month_date.year,
                    month_date.month,
                    str(error),
                )
