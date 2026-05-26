"""Config flow for Contact Energy integration."""
import asyncio
import logging
import voluptuous as vol
import aiohttp
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

from .api import ContactEnergyApi, CannotConnect, InvalidAuth, UnknownError
from .const import (
    DOMAIN,
    CONF_USAGE_DAYS,
    CONF_INITIAL_BACKFILL_DAYS,
    CONF_DAILY_LOOKBACK_DAYS,
    CONF_ACCOUNT_ID,
    CONF_CONTRACT_ID,
    CONF_CONTRACT_ICP,
    CONF_CONTRACT_TYPE,
    CONTRACT_TYPE_ELECTRICITY,
    CONTRACT_TYPE_GAS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_INITIAL_BACKFILL_DAYS, default=30): cv.positive_int,
        vol.Optional(CONF_DAILY_LOOKBACK_DAYS, default=4): vol.All(
            cv.positive_int,
            vol.Range(min=4),  # Must be at least 4 to ensure incremental updates reach back far enough with 3-day API lag
        ),
    }
)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    _LOGGER.debug("Starting validation for email: %s", data[CONF_EMAIL])
    
    try:
        api = ContactEnergyApi(hass, data[CONF_EMAIL], data[CONF_PASSWORD])
        if not await api.async_login():
            _LOGGER.error("Login failed for email: %s", data[CONF_EMAIL])
            raise InvalidAuth
        
        # Fetch accounts after successful login
        accounts_data = await api.async_get_accounts()
        if not accounts_data or "accountDetail" not in accounts_data:
            _LOGGER.error("No accounts found for email: %s", data[CONF_EMAIL])
            raise UnknownError("No accounts found")

        # Extract available contracts (both electricity and gas)
        account_id = accounts_data["accountDetail"]["id"]
        contracts = []
        all_contracts = accounts_data["accountDetail"]["contracts"]
        
        for contract in all_contracts:
            contract_type = contract.get("contractType")
            type_label = contract.get("contractTypeLabel", "Unknown")
            
            # Include both electricity and gas contracts
            if contract_type in (CONTRACT_TYPE_ELECTRICITY, CONTRACT_TYPE_GAS):
                contracts.append({
                    "id": contract["id"],
                    "address": contract["premise"]["supplyAddress"]["shortForm"],
                    "account_id": account_id,
                    "icp": contract["icp"],
                    "contract_type": contract_type,
                    "type_label": type_label,
                })
        
        if not contracts:
            _LOGGER.error("No contracts found for email: %s", data[CONF_EMAIL])
            raise UnknownError("No contracts found")
        
        _LOGGER.info("Successfully authenticated Contact Energy account: %s", data[CONF_EMAIL])
        return {
            "title": f"Contact Energy ({data[CONF_EMAIL]})",
            "email": data[CONF_EMAIL],
            "password": data[CONF_PASSWORD],
            "contracts": contracts
        }
        
    except aiohttp.ClientError as error:
        _LOGGER.error("Connection error during validation: %s", str(error))
        raise CannotConnect from error
    except asyncio.TimeoutError as error:
        _LOGGER.error("Timeout error during validation: %s", str(error))
        raise CannotConnect from error
    except InvalidAuth as error:
        _LOGGER.error("Invalid authentication for email: %s", data[CONF_EMAIL])
        raise error
    except Exception as error:
        _LOGGER.exception("Unexpected error during validation: %s", str(error))
        raise UnknownError from error

class ContactEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Contact Energy."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self._current_input = {}
        self._contracts = []
        self._validated_data = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                self._validated_data = await validate_input(self.hass, user_input)
                self._contracts = self._validated_data["contracts"]
                self._current_input.update(user_input)
                
                # Always go to contract selection (even with one contract)
                # This allows users to set it up, or skip if they prefer
                return await self.async_step_contract()
                
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as error:
                _LOGGER.exception("Unexpected error in config flow: %s", error)
                errors["base"] = "unknown"

        # Preserve form values
        schema = self.add_suggested_values_to_schema(
            STEP_USER_DATA_SCHEMA, self._current_input
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle contract selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_contract = next(
                (c for c in self._contracts if c["id"] == user_input[CONF_CONTRACT_ID]),
                None
            )
            if selected_contract:
                self._current_input[CONF_ACCOUNT_ID] = selected_contract["account_id"]
                self._current_input[CONF_CONTRACT_ID] = selected_contract["id"]
                self._current_input[CONF_CONTRACT_ICP] = selected_contract['icp']
                self._current_input[CONF_CONTRACT_TYPE] = selected_contract['contract_type']
                
                await self.async_set_unique_id(selected_contract["id"])
                self._abort_if_unique_id_configured()
                
                type_label = selected_contract.get('type_label', 'Unknown')
                return self.async_create_entry(
                    title=f"{self._validated_data['title']} - {selected_contract['address']} ({type_label})",
                    data=self._current_input
                )
            else:
                errors["base"] = "invalid_contract"

        # Create schema for contract selection
        contract_schema = vol.Schema({
            vol.Required(CONF_CONTRACT_ID): vol.In({
                c["id"]: f"{c['type_label']} - {c['address']}"
                for c in self._contracts
            }),
        })

        return self.async_show_form(
            step_id="contract",
            data_schema=contract_schema,
            errors=errors,
        )