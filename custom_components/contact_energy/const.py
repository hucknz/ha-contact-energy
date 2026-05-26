"""Constants for the Contact Energy sensors."""

from datetime import timedelta
from typing import Final, final

DOMAIN = "contact_energy"
DOMAIN_NAME = "Contact Energy"

# Global polling interval for all sensors
SCAN_INTERVAL = timedelta(hours=1)
FORCED_SCAN_INTERVAL = timedelta(hours=24)

# Electricity sensor names
SENSOR_ENERGY_NAME = "Total Energy"
SENSOR_DAILY_ENERGY_NAME = "Daily Energy"
SENSOR_COST_NAME = "Total Cost"
SENSOR_DAILY_COST_NAME = "Daily Cost"

# Gas sensor names
SENSOR_GAS_NAME = "Total Gas"
SENSOR_DAILY_GAS_NAME = "Daily Gas"

SENSOR_USAGE_NAME = "Usage"
SENSOR_SOLD_NAME = "Sold"
SENSOR_PRICES_NAME = "Prices"

SENSOR_ACCOUNT_BALANCE_NAME = "Account Balance"
SENSOR_NEXT_BILL_AMOUNT_NAME = "Next Bill Amount"
SENSOR_NEXT_BILL_DATE_NAME = "Next Bill Date"
SENSOR_PAYMENT_DUE_NAME = "Payment Due"
SENSOR_PAYMENT_DUE_DATE_NAME = "Payment Due Date"
SENSOR_PREVIOUS_READING_DATE_NAME = "Previous Reading Date"
SENSOR_NEXT_READING_DATE_NAME = "Next Reading Date"

# Contract types
CONTRACT_TYPE_ELECTRICITY = 1
CONTRACT_TYPE_GAS = 2

CONF_ACCOUNT_ID = "account_id"
CONF_CONTRACT_ID = "contract_id"
CONF_CONTRACT_ICP = "contract_icp"
CONF_CONTRACT_TYPE = "contract_type"
CONF_PRICES = "prices"
CONF_USAGE = "usage"
CONF_SOLD = "sold"
CONF_SOLD_MEASURE = "sold_measure"
CONF_SOLD_DAILY = "sold_daily"
CONF_USAGE_DAYS = "usage_days"
CONF_INITIAL_BACKFILL_DAYS = "initial_backfill_days"
CONF_DAILY_LOOKBACK_DAYS = "daily_lookback_days"
CONF_SHOW_HOURLY = "show_hourly"
CONF_DATE_FORMAT = "date_format"
CONF_TIME_FORMAT = "time_format"
CONF_HOURLY_OFFSET_DAYS = "hourly_offset_days"

MONITORED_CONDITIONS_DEFAULT = [
    "is_retail_customer",
    "current_price",
    "referral_discount_in_kr",
    "has_unpaid_invoices",
    "yearly_savings_in_kr",
    "timezone",
    "retail_termination_date",
    "current_day",
    "next_day",
    "current_month",
]

#-----#
ENTITY_ID_FORMAT: Final = DOMAIN + ".{}"