"""Contact Energy sensors."""
import logging
from datetime import datetime, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from custom_components.contact_energy.sensors import (
    ContactEnergyAccountSensor,
    ContactEnergyEnergySensor,
    ContactEnergyDailyEnergySensor,
    ContactEnergyCostSensor,
    ContactEnergyDailyCostSensor,
    ContactEnergyGasSensor,
    ContactEnergyDailyGasSensor,
)
from custom_components.contact_energy.api import ContactEnergyApi

from homeassistant.const import (
    CURRENCY_DOLLAR,
    CONF_EMAIL,
    CONF_PASSWORD,
    UnitOfEnergy
)

from custom_components.contact_energy.const import (
    CONF_ACCOUNT_ID, 
    CONF_CONTRACT_ID, 
    CONF_CONTRACT_ICP,
    CONF_CONTRACT_TYPE,
    CONTRACT_TYPE_ELECTRICITY,
    CONTRACT_TYPE_GAS,
    SENSOR_ENERGY_NAME,
    SENSOR_DAILY_ENERGY_NAME,
    SENSOR_COST_NAME,
    SENSOR_DAILY_COST_NAME,
    SENSOR_GAS_NAME,
    SENSOR_DAILY_GAS_NAME,
    SENSOR_ACCOUNT_BALANCE_NAME,
    SENSOR_NEXT_BILL_AMOUNT_NAME,
    SENSOR_NEXT_BILL_DATE_NAME,
    SENSOR_PAYMENT_DUE_NAME,
    SENSOR_PAYMENT_DUE_DATE_NAME,
    SENSOR_PREVIOUS_READING_DATE_NAME,
    SENSOR_NEXT_READING_DATE_NAME,
    DOMAIN,
    SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=1)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Contact Energy sensors from a config entry."""
    icp = entry.data[CONF_CONTRACT_ICP]
    contract_type = entry.data.get(CONF_CONTRACT_TYPE, CONTRACT_TYPE_ELECTRICITY)

    # Get the stored API instance and entry data
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api = entry_data["api"]

    sensors = []

    # Add consumption sensors based on contract type
    if contract_type == CONTRACT_TYPE_ELECTRICITY:
        sensors.extend([
            ContactEnergyEnergySensor(
                hass,
                SENSOR_ENERGY_NAME,
                api,
                icp,
                entry,
            ),
            ContactEnergyDailyEnergySensor(
                hass,
                SENSOR_DAILY_ENERGY_NAME,
                api,
                icp,
                entry,
            ),
            ContactEnergyCostSensor(
                hass,
                SENSOR_COST_NAME,
                api,
                icp,
                entry,
            ),
            ContactEnergyDailyCostSensor(
                hass,
                SENSOR_DAILY_COST_NAME,
                api,
                icp,
                entry,
            ),
        ])
    elif contract_type == CONTRACT_TYPE_GAS:
        sensors.extend([
            ContactEnergyGasSensor(
                hass,
                SENSOR_GAS_NAME,
                api,
                icp,
                entry,
            ),
            ContactEnergyDailyGasSensor(
                hass,
                SENSOR_DAILY_GAS_NAME,
                api,
                icp,
                entry,
            ),
        ])

    # Add account sensors (available for all contract types)
    sensors.extend([
        ContactEnergyAccountSensor(
            hass,
            SENSOR_ACCOUNT_BALANCE_NAME,
            api,
            icp,
            CURRENCY_DOLLAR,
            "mdi:cash",
            SensorStateClass.MEASUREMENT,
            SensorDeviceClass.MONETARY,
            lambda data: data["accountDetail"]["accountBalance"]["currentBalance"],
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_NEXT_BILL_AMOUNT_NAME,
            api,
            icp,
            CURRENCY_DOLLAR,
            "mdi:cash-clock",
            SensorStateClass.MEASUREMENT,
            SensorDeviceClass.MONETARY,
            lambda data: data["accountDetail"]["nextBill"]["amount"],
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_NEXT_BILL_DATE_NAME,
            api,
            icp,
            None,
            "mdi:calendar",
            None,
            SensorDeviceClass.DATE,
            lambda data: datetime.strptime(
                data["accountDetail"]["nextBill"]["date"],
                "%d %b %Y"
            ).date().isoformat(),
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_PAYMENT_DUE_NAME,
            api,
            icp,
            CURRENCY_DOLLAR,
            "mdi:cash-marker",
            SensorStateClass.MEASUREMENT,
            SensorDeviceClass.MONETARY,
            lambda data: data["accountDetail"]["invoice"]["amountDue"],
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_PAYMENT_DUE_DATE_NAME,
            api,
            icp,
            None,
            "mdi:calendar-clock",
            None,
            SensorDeviceClass.DATE,
            lambda data: datetime.strptime(
                data["accountDetail"]["invoice"]["paymentDueDate"],
                "%d %b %Y"
            ).date().isoformat(),
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_PREVIOUS_READING_DATE_NAME,
            api,
            icp,
            None,
            "mdi:calendar",
            None,
            SensorDeviceClass.DATE,
            lambda data: datetime.strptime(
                data["accountDetail"]["contracts"][0]["devices"][0]["registers"][0]["previousMeterReadingDate"],
                "%d %b %Y"
            ).date().isoformat(),
        ),
        ContactEnergyAccountSensor(
            hass,
            SENSOR_NEXT_READING_DATE_NAME,
            api,
            icp,
            None,
            "mdi:calendar",
            None,
            SensorDeviceClass.DATE,
            lambda data: datetime.strptime(
                data["accountDetail"]["contracts"][0]["devices"][0]["nextMeterReadDate"],
                "%d %b %Y"
            ).date().isoformat(),
        ),
    ])
    async_add_entities(sensors, True)