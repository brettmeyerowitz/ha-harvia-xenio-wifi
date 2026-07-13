from homeassistant.components.switch import SwitchEntity
from .constants import DOMAIN, _LOGGER
from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers.device_registry import DeviceInfo

class HarviaPowerSwitch(SwitchEntity):
    def __init__(self, device, name, sauna):
        self._device = device
        self._name = name + ' Power switch'
        self._is_on = device.active
        self._device_id = device.id + '_power'
        self._sauna = sauna
        self._attr_unique_id = device.id + '_power'
        self._attr_icon = 'mdi:heating-coil'

        # Bind entities to a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=getattr(device, "name", None) or name,
            manufacturer="Harvia",
            model=getattr(device, "model", None) or "Xenio WiFi",
        )


    @property
    def name(self):
        return self._name

    @property
    def is_on(self):
        return self._is_on

    @property
    def icon(self) -> str | None:
        """Icon of the entity."""
        return "mdi:heater"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._device_id

    async def async_added_to_hass(self):
        """Actions to perform when the entity is added to Home Assistant."""
        self._device.powerSwitch = self
        await self._device.update_ha_devices()

    async def update_state(self):
        # Avoid writing state for disabled entities (HA 2026+ warns about this)
        if not self.enabled:
            return
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        # Code to turn the sauna on
        self._is_on = True
        if self.enabled:
            self.async_write_ha_state()

        await self._device.set_active(True)
        if self._device.thermostat is not None:
            self._device.thermostat._hvac_mode = HVACMode.HEAT
            await self._device.thermostat.update_state()

    async def async_turn_off(self, **kwargs):
        # Code to turn the sauna off
        self._is_on = False
        if self.enabled:
            self.async_write_ha_state()

        await self._device.set_active(False)
        if self._device.thermostat is not None:
            self._device.thermostat._hvac_mode = HVACMode.OFF
            await self._device.thermostat.update_state()

# NOTE: the cabin light used to be a HarviaLightSwitch here. It now lives on the
# `light` platform (see light.py / HarviaLight) so it presents as a real light.

class HarviaSteamerSwitch(SwitchEntity):
    def __init__(self, device, name, sauna):
        self._device = device
        self._name = name + ' Steamer Switch'
        self._is_on = device.lightsOn
        self._device_id = device.id + '_steamer'
        self._sauna = sauna
        self._attr_unique_id = device.id + '_steamer'
        self._attr_icon = 'mdi:wave'

        # Bind entities to a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=getattr(device, "name", None) or name,
            manufacturer="Harvia",
            model=getattr(device, "model", None) or "Xenio WiFi",
        )


    async def async_added_to_hass(self):
        """Actions to perform when the entity is added to Home Assistant."""
        self._device.steamerSwitch = self
        await self._device.update_ha_devices()
        #self._device.

    async def update_state(self):
        if not self.enabled:
            return
        self.async_write_ha_state()

    @property
    def name(self):
        return self._name

    @property
    def is_on(self):
        return self._is_on


    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._device_id

    async def async_turn_on(self, **kwargs):
        # Code to turn the sauna on
        await self._device.set_steamer(True)
        self._is_on = True

    async def async_turn_off(self, **kwargs):
        # Code to turn the sauna off
        await self._device.set_steamer(False)
        self._is_on = False

class HarviaFanSwitch(SwitchEntity):
    def __init__(self, device, name, sauna):
        self._device = device
        self._name = name + ' Fan Switch'
        self._is_on = device.fanOn
        self._device_id = device.id + '_fan'
        self._sauna = sauna
        self._attr_unique_id = device.id + '_fan'
        self._attr_icon = 'mdi:fan'

        # Bind entities to a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=getattr(device, "name", None) or name,
            manufacturer="Harvia",
            model=getattr(device, "model", None) or "Xenio WiFi",
        )


    async def async_added_to_hass(self):
        """Actions to perform when the entity is added to Home Assistant."""
        self._device.fanSwitch = self
        await self._device.update_ha_devices()
        #self._device.

    async def update_state(self):
        if not self.enabled:
            return
        self.async_write_ha_state()

    @property
    def name(self):
        return self._name

    @property
    def is_on(self):
        return self._is_on


    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._device_id

    async def async_turn_on(self, **kwargs):
        # Code to turn the sauna on
        await self._device.set_fan(True)
        self._is_on = True

    async def async_turn_off(self, **kwargs):
        # Code to turn the sauna off
        await self._device.set_fan(False)
        self._is_on = False

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Harvia switches."""
    # Here you would add the logic to retrieve your devices.
    # For now we manually add switches as an example.
    devices = await hass.data[DOMAIN]['api'].get_devices()
    switches = []

    for device in devices:
        _LOGGER.debug(f"Loading switches for device: {device.name}")
        device_switches = await device.get_switches()
        for device_switch in device_switches:
            switches.append(device_switch)

    async_add_entities(switches, True)
