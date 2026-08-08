from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.helpers.device_registry import DeviceInfo
from .constants import DOMAIN, _LOGGER


class HarviaLight(LightEntity):
    """The sauna cabin light, exposed as an on/off light.

    Wraps the same device plumbing the old light *switch* used
    (device.lightsOn / device.set_lights); it just lives on the `light`
    platform so it shows up as a light in Home Assistant, HomeKit, etc.
    """

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, device, name, sauna):
        self._device = device
        self._name = name + ' Light'
        self._is_on = device.lightsOn
        self._device_id = device.id + '_light'
        self._sauna = sauna
        self._attr_unique_id = device.id + '_light'
        self._attr_icon = 'mdi:lightbulb-multiple'

        # Bind entity to the same Home Assistant device as the other sauna entities
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
    def unique_id(self):
        """Return a unique ID."""
        return self._device_id

    async def async_added_to_hass(self):
        """Register as the device's light entity and sync current state."""
        # HarviaDevice.update_ha_devices() pushes light state via `.lightSwitch`,
        # so we reuse that hook — the device loop only needs `_is_on` + update_state().
        self._device.lightSwitch = self
        await self._device.update_ha_devices()

    async def update_state(self):
        # Called by HarviaDevice.update_ha_devices() when lightsOn changes.
        # Avoid writing state for disabled entities (HA 2026+ warns about this).
        if not self.enabled:
            return
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._device.set_lights(True)
        self._is_on = True
        if self.enabled:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_lights(False)
        self._is_on = False
        if self.enabled:
            self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Harvia sauna light(s)."""
    devices = await hass.data[DOMAIN]['api'].get_devices()
    lights = []

    for device in devices:
        _LOGGER.debug(f"Loading lights for device: {device.name}")
        device_lights = await device.get_lights()
        for device_light in device_lights:
            lights.append(device_light)

    async_add_entities(lights, True)
