from __future__ import annotations
import json
import logging
import asyncio
import signal
import ssl
import websockets
import uuid
import random


from .switch import HarviaPowerSwitch, HarviaFanSwitch, HarviaSteamerSwitch
from .light import HarviaLight
from .climate import HarviaThermostat
from .sensor import HarviaHumiditySensor, HarviaWifiRssiSensor, HarviaRemainingTimeSensor, HarviaStovePowerSensor
from .number import HarviaHumiditySetPoint
from .api import HarviaSaunaAPI
from .binary_sensor import HarviaDoorSensor
from .constants import DOMAIN, STORAGE_KEY, STORAGE_VERSION, REGION
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.const import Platform

from pycognito import Cognito
import boto3

_LOGGER = logging.getLogger(__name__)

# Home Assistant platforms to set up for this integration.
# IMPORTANT: use Platform enums (not strings) so forward/unload works reliably across HA versions.
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.NUMBER,
]

class HarviaDevice:
    def __init__(self, sauna: HarviaSauna, id: str):
        self.sauna = sauna
        self.data = {}
        self.user  = None
        self.id = id
        self.active = False
        self.lightsOn = False
        self.steamOn = False
        self.targetTemp = None
        self.targetRh = None
        self.currentTemp = None
        self.humidity = None
        self.remainingTime = None
        self.wifiRSSI = None
        self.stovePower = None
        self.heatUpTime = 0
        self.targetRh = 0
        self.onTime = 0
        self.fanOn = False
        self.statusCodes = None
        self.doorSafetyState = None
        self.safetyRelay = None
        self.name = None
        self.lightSwitch = None
        self.powerSwitch = None
        self.fanSwitch = None
        self.steamerSwitch = None
        self.doorSensor = None
        self.safetySwitchSensor = None
        self.thermostat = None
        self.binarySensors = None
        self.humiditySensor = None
        self.wifiRssiSensor = None
        self.remainingTimeSensor = None
        self.stovePowerSensor = None
        self.humidityNumber = None
        self.sensors = None
        self.numbers = None
        self.switches = None
        self.lights = None
        self.thermostats = None
        self.lastestUpdate = None

    async def update_generic_sensors(self):
        # Update all generic attribute sensors
        if hasattr(self, 'generic_sensors'):
            for attr, sensor in self.generic_sensors.items():
                value = getattr(self, attr, None)
                sensor._state = value
                await sensor.update_state()

    async def update_data(self, data: dict):
        self.data = data

        _LOGGER.debug(f"Performing device update: " + json.dumps(data))

        # Set all attributes from data and update corresponding GenericAttributeSensor if it exists
        for key, value in data.items():
            setattr(self, key, value)
            # Update GenericAttributeSensor if it exists for this attribute
            if hasattr(self, 'generic_sensors') and key in self.generic_sensors:
                self.generic_sensors[key]._state = value
                if getattr(self.generic_sensors[key], 'hass', None) is not None:
                    await self.generic_sensors[key].update_state()

        # Maintain legacy/explicit attributes for compatibility
        if 'displayName' in data:
            self.name = data['displayName']
        if 'active' in data:
            self.active = bool(data['active'])
        if 'light' in data:
            self.lightsOn = bool(data['light'])
        if 'fan' in data:
            self.fanOn = bool(data['fan'])
        if 'steamOn' in data:
            self.steamOn = data['steamOn']
        if 'steamEn' in data:
            self.steamOn = bool(data['steamEn'])
        if 'heatOn' in data:
            self.active = data['heatOn']
        if 'targetTemp' in data:
            self.targetTemp = data['targetTemp']
        if 'targetRh' in data:
            self.targetRh = data['targetRh']
        if 'heatUpTime' in data:
            self.heatUpTime = data['heatUpTime']
        if 'remainingTime' in data:
            self.remainingTime = data['remainingTime']
        if 'wifiRSSI' in data:
            self.wifiRSSI = data['wifiRSSI']
        if 'stovePower' in data:
            self.stovePower = data['stovePower']
        if 'temperature' in data:
            self.currentTemp = data['temperature']
        if 'humidity' in data:
            self.humidity = data['humidity']
        if 'timestamp' in data:
            self.lastestUpdate = data['timestamp']
        if 'doorSafetyState' in data:
            self.doorSafetyState = data['doorSafetyState']
        if 'safetyRelay' in data:
            self.safetyRelay = data['safetyRelay']
        if 'statusCodes' in data:
            if data['statusCodes'] != self.statusCodes:
                _LOGGER.debug("StatusCodes changed: " +  str(data['statusCodes']))
            self.statusCodes = data['statusCodes']

        await self.dump_data()
        await self.update_ha_devices()

    async def update_ha_devices(self):
        # StatusCodes sensor: expose raw statusCodes for diagnostics
        if hasattr(self, 'statusCodesSensor') and self.statusCodesSensor is not None:
            self.statusCodesSensor._state = self.statusCodes
            await self.statusCodesSensor.update_state()

        if self.lightSwitch is not None:
            self.lightSwitch._is_on = self.lightsOn
            await self.lightSwitch.update_state()

        if self.powerSwitch is not None:
            self.powerSwitch._is_on = self.active
            await self.powerSwitch.update_state()

        if self.steamerSwitch is not None:
            self.steamerSwitch._is_on = self.steamOn
            await self.steamerSwitch.update_state()

        if self.fanSwitch is not None:
            self.fanSwitch._is_on = self.fanOn
            await self.fanSwitch.update_state()


        if self.doorSensor is not None:
            # Best practice: Use the second digit of statusCodes to determine door state, as in the original repo.
            # If statusCodes is not available or not a string of sufficient length, default to closed (False).
            door_open = False
            if self.statusCodes is not None:
                try:
                    status_str = str(self.statusCodes)
                    if len(status_str) > 1 and status_str[1].isdigit():
                        # According to original logic, 9 means open
                        door_open = int(status_str[1]) == 9
                        _LOGGER.debug("Door state determined from statusCodes[1]: %s (statusCodes=%s)", door_open, status_str)
                    else:
                        _LOGGER.debug("statusCodes[1] not a digit or statusCodes too short: %s", status_str)
                except Exception as e:
                    _LOGGER.error("Error parsing statusCodes for door state: %s", e)
            else:
                _LOGGER.debug("statusCodes is None; defaulting door to closed.")
            self.doorSensor._door_open = door_open
            if getattr(self.doorSensor, 'hass', None) is not None:
                await self.doorSensor.update_state()

        # Sauna Ready Sensor: on if sauna is active and current temp >= target temp
        if hasattr(self, 'readySensor') and self.readySensor is not None:
            ready = False
            if self.active and self.currentTemp is not None and self.targetTemp is not None:
                try:
                    ready = float(self.currentTemp) >= float(self.targetTemp)
                except Exception:
                    ready = False
            self.readySensor._ready = ready
            if getattr(self.readySensor, 'hass', None) is not None:
                await self.readySensor.update_state()

        if self.humiditySensor is not None:
            self.humiditySensor._state = self.humidity
            if getattr(self.humiditySensor, 'hass', None) is not None:
                await self.humiditySensor.update_state()

        if self.wifiRssiSensor is not None:
            self.wifiRssiSensor._state = self.wifiRSSI
            if getattr(self.wifiRssiSensor, 'hass', None) is not None:
                await self.wifiRssiSensor.update_state()

        if self.remainingTimeSensor is not None:
            self.remainingTimeSensor._state = self.remainingTime
            if getattr(self.remainingTimeSensor, 'hass', None) is not None:
                await self.remainingTimeSensor.update_state()

        if self.stovePowerSensor is not None:
            self.stovePowerSensor._state = self.stovePower
            if getattr(self.stovePowerSensor, 'hass', None) is not None:
                await self.stovePowerSensor.update_state()

        if self.humidityNumber is not None:
            self.humidityNumber._state = self.targetRh
            if getattr(self.humidityNumber, 'hass', None) is not None:
                await self.humidityNumber.update_state()

        if self.thermostat is not None:
            self.thermostat._target_temperature = self.targetTemp
            self.thermostat._current_temperature = self.currentTemp

            if self.active == True:
                self.thermostat._hvac_mode = HVACMode.HEAT
            else:
                self.thermostat._hvac_mode =  HVACMode.OFF
            await self.thermostat.update_state()

    async def set_target_temperature(self, temp: int):

        payload = {'targetTemp': temp}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def set_target_relative_humidity(self, temp: int):
        payload = {'targetRh': temp}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def set_fan(self, state: bool):
        fanInt = int(state)
        payload = {'fan': fanInt}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def set_lights(self, state: bool):
        lightInt = int(state)
        payload = {'light': lightInt}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def set_steamer(self, state: bool):
        activeInt = int(state)
        payload = {'steamEn': activeInt}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def set_active(self, state: bool):
        activeInt = int(state)
        payload = {'active': activeInt}
        await self.sauna.device_mutation(deviceId=self.id,payload=payload)

    async def dump_data(self):

        data = { 'name': self.name, 'active': self.active, 'lightsOn': self.lightsOn, 'SteamOn': self.steamOn,  'targetTemp': self.targetTemp,  'targetRh': self.targetRh, 'fanOn':  self.fanOn, 'heatUpTime': self.heatUpTime  }

        attributes_as_string = json.dumps(data, indent=4)
        _LOGGER.debug(f"Device attributes: {attributes_as_string}")

    async def get_binary_sensors(self) -> list:
        if self.binarySensors is not None:
            return self.binarySensors
        self.binarySensors = []
        # Door contact sensor
        from .binary_sensor import HarviaDoorSensor, SaunaReadySensor
        self.doorSensor = HarviaDoorSensor(device=self, name=self.name, sauna=self.sauna)
        self.binarySensors.append(self.doorSensor)
        # Sauna ready sensor
        self.readySensor = SaunaReadySensor(device=self, name=self.name, sauna=self.sauna)
        self.binarySensors.append(self.readySensor)
        return self.binarySensors


    async def get_sensors(self) -> list:
        if self.sensors is not None:
            return self.sensors
        self.sensors = []
        humiditySensor = HarviaHumiditySensor(device=self, name=self.name, sauna=self.sauna)
        wifiRssiSensor = HarviaWifiRssiSensor(device=self, name=self.name)
        remainingTimeSensor = HarviaRemainingTimeSensor(device=self, name=self.name)
        stovePowerSensor = HarviaStovePowerSensor(device=self, name=self.name)
        from .sensor import StatusCodesSensor
        statusCodesSensor = StatusCodesSensor(device=self, name=self.name)
        self.sensors.append(humiditySensor)
        self.sensors.append(wifiRssiSensor)
        self.sensors.append(remainingTimeSensor)
        self.sensors.append(stovePowerSensor)
        self.sensors.append(statusCodesSensor)
        return self.sensors

    async def get_numbers(self) -> list:

        if self.numbers != None:
            return self.numbers

        self.numbers = []

        humidityNumber = HarviaHumiditySetPoint(device=self, name=self.name, sauna=self.sauna)
        self.numbers.append(humidityNumber)

        return self.numbers

    async def get_thermostats(self) -> list:
        if self.thermostats != None:
            return self.thermostats

        self.thermostats = []

        thermostat = HarviaThermostat(device=self, name=self.name, sauna=self.sauna)
        self.thermostats.append(thermostat)

        return self.thermostats

    async def get_switches(self) -> list:
        if self.switches != None:
            return self.switches

        self.switches = []

        powerSwitch = HarviaPowerSwitch(device=self, name=self.name, sauna=self.sauna)
        steamerSwitch = HarviaSteamerSwitch(device=self, name=self.name, sauna=self.sauna)
        fanSwitch = HarviaFanSwitch(device=self, name=self.name, sauna=self.sauna)

        self.switches.append(powerSwitch)
        self.switches.append(steamerSwitch)
        self.switches.append(fanSwitch)

        return self.switches

    async def get_lights(self) -> list:
        if self.lights is not None:
            return self.lights

        self.lights = []

        light = HarviaLight(device=self, name=self.name, sauna=self.sauna)
        self.lights.append(light)

        return self.lights


class HarviaWebsock:

    def __init__(self, sauna: 'HarviaSauna', endpoint: str, user_receiver: bool = False):
        self.sauna = sauna
        self.websocket = None
        self.timeout = 300
        self.endpoint = endpoint
        self.endpoint_host = None
        self.reconnect_attempts = 0
        self.uuid = None
        self.user_receiver = user_receiver
        self.websocket_task = None
        # Stale connection watchdog: reconnect only if we stop receiving messages.
        self.stale_timeout = 600  # seconds without any message before forcing reconnect
        self._last_message_monotonic: float | None = None
        self.watchdog_task: asyncio.Task | None = None


    async def connect(self):
        # Start (or restart) the websocket loop and watchdog.
        if self.websocket_task is None or self.websocket_task.done():
            self.websocket_task = asyncio.create_task(self.start())
        if self.watchdog_task is None or self.watchdog_task.done():
            self.watchdog_task = asyncio.create_task(self.watchdog())


    async def start(self):
        """Run the websocket loop; reconnect on failure with backoff."""
        while True:
            try:
                endpoint = await self.sauna.api.getWebsocketEndpoint(self.endpoint)
                self.endpoint_host = endpoint['host']
                self.uuid = str(uuid.uuid4())

                url = await self.sauna.api.getWebsockUrlByEndpoint(self.endpoint)
                payload = {'type': 'connection_init'}
                _LOGGER.debug(
                    "Websock %s connecting (user_receiver=%s) to host=%s",
                    self.endpoint,
                    self.user_receiver,
                    self.endpoint_host,
                )

                # Reset last-message timestamp whenever we (re)connect
                self._last_message_monotonic = asyncio.get_running_loop().time()

                # Create SSL context off the event loop (HA warns if default certs are loaded on-loop)
                ssl_context = await self.sauna.hass.async_add_executor_job(ssl.create_default_context)
                async with websockets.connect(url, subprotocols=["graphql-ws"], ssl=ssl_context) as self.websocket:
                    self.reconnect_attempts = 0
                    await self.websocket.send(json.dumps(payload))

                    while True:
                        message = await self.receive_message(self.websocket)
                        if message:
                            await self.handle_message(message)
                        else:
                            _LOGGER.error("No 'ka' keepalive received within the timeout window.")
                            break  # Break inner loop to reconnect

            except (websockets.exceptions.ConnectionClosedError, asyncio.TimeoutError) as e:
                _LOGGER.error("Connection error: %s", e)
            except Exception as e:
                _LOGGER.exception("Unexpected websocket error on %s: %s", self.endpoint, e)

            # Ensure websocket reference is cleared before retrying
            self.websocket = None

            # Exponential backoff (cap at 60s) + jitter
            await asyncio.sleep(min(2 ** self.reconnect_attempts, 60) + random.uniform(0, 1))
            self.reconnect_attempts += 1

    async def create_subscription(self):
        id_token = await self.sauna.api.getIdToken()
        data = ""
        if self.endpoint == 'data':
            data =  await self.create_data_subscription_message()
        elif self.endpoint == 'device':
            data =  await self.create_device_subscription_message()

        payload = {
                    "id": self.uuid,
                    "payload": {
                        "data": data,
                        "extensions": {
                            "authorization": {
                                "Authorization": id_token,
                                "host": self.endpoint_host,
                                "x-amz-user-agent": "aws-amplify/2.0.5 react-native"
                            }
                        }
                    },
                    "type": "start"
                    }

        message = json.dumps(payload)
        _LOGGER.debug(
            "Websock %s sending subscription (user_receiver=%s, id=%s)",
            self.endpoint,
            self.user_receiver,
            self.uuid,
        )
        await self.websocket.send(message)

    async def create_data_subscription_message(self) -> str:
        userData = await self.sauna.get_user_data()
        if not self.user_receiver:
            receiver = userData["organizationId"]
        else:
            receiver =  userData["email"]
        return "{\"query\":\"subscription Subscription($receiver: String!) {\\n  onDataUpdates(receiver: $receiver) {\\n    item {\\n      deviceId\\n      timestamp\\n      sessionId\\n      type\\n      data\\n      __typename\\n    }\\n    __typename\\n  }\\n}\\n\",\"variables\":{\"receiver\":\""+receiver+"\"}}"

    async def create_device_subscription_message(self) -> str:
        userData = await self.sauna.get_user_data()
        if not self.user_receiver:
            receiver = userData["organizationId"]
        else:
            receiver =  userData["email"]
        return "{\"query\":\"subscription Subscription($receiver: String!) {\\n  onStateUpdated(receiver: $receiver) {\\n    desired\\n    reported\\n    timestamp\\n    receiver\\n    __typename\\n  }\\n}\\n\",\"variables\":{\"receiver\":\""+receiver+"\"}}"

    async def handle_message(self, message):
        """Process and respond to an incoming message."""
        _LOGGER.debug(
            "Websock %s received message (user_receiver=%s, type=%s)",
            self.endpoint,
            self.user_receiver,
            json.loads(message).get("type"),
        )
        data = json.loads(message)  # Message is JSON-formatted
        # Mark connection as alive whenever we receive a message
        self._last_message_monotonic = asyncio.get_running_loop().time()
        if data.get("type") == "ka":
            _LOGGER.debug("Websock " + self.endpoint + " keepalive received.")
        elif data.get('type') == 'connection_ack':
            _LOGGER.debug("Websock connection_ack received")
            if data.get('payload'):
                self.timeout = data['payload']['connectionTimeoutMs']/1000
            await self.create_subscription()
        elif data.get("type") == "start_ack":
            _LOGGER.debug("Websock %s subscription acknowledged.", self.endpoint)
        elif data.get("type") == "data":
            _LOGGER.debug("Websock %s data update received.", self.endpoint)
            if self.endpoint == 'device':
                await self.sauna.process_device_update(data)
            elif self.endpoint == 'data':
                await self.sauna.process_device_update(data)
        else:
            _LOGGER.debug(
                "Unknown message type on %s: %s",
                self.endpoint,
                data.get("type"),
            )

    async def receive_message(self,websocket):
        """Wait for a message with a maximum timeout."""
        try:
            message = await asyncio.wait_for(websocket.recv(), self.timeout)
            return message
        except websockets.exceptions.ConnectionClosedError as e:
            _LOGGER.error("WebSocket connection was closed: %s", e)
        except asyncio.TimeoutError:
            return None

    async def watchdog(self):
        """Force a reconnect only if the websocket stops receiving messages."""
        while True:
            await asyncio.sleep(30)

            if self.websocket is None:
                continue

            if self._last_message_monotonic is None:
                continue

            now = asyncio.get_running_loop().time()
            if now - self._last_message_monotonic > self.stale_timeout:
                _LOGGER.warning(
                    "Websock %s appears stale (no messages for %ss). Forcing reconnect.",
                    self.endpoint,
                    self.stale_timeout,
                )
                try:
                    await self.websocket.close()
                except Exception:
                    pass
                self.websocket = None

class HarviaSauna:

    def __init__(self, hass: HomeAssistant, storage: Store, config: dict):
        self.hass = hass
        self.storage = storage
        self.config = config
        self.data = {}
        self.devices = None
        self.user_data = None
        self.cognito = None
        self.websockDevice = None
        self.websockData = None
        self.websockDeviceUser = None
        self.websockDataUser = None
        self.api = None

    async def async_setup(self, config: dict) -> bool:
        """Set up the Harvia Sauna integration from the config entry."""
        _LOGGER.info("Starting setup of Harvia Sauna component.")

        self.data = await self.storage.async_load() or {}

        if DOMAIN not in self.hass.data:
            self.hass.data[DOMAIN] = {}

        username = self.config.data.get(CONF_USERNAME)
        password = self.config.data.get(CONF_PASSWORD)

        self.api =  HarviaSaunaAPI(username, password, self.hass)
        if await self.api.authenticate():
            _LOGGER.info("Harvia Sauna component setup completed.")
        else:
            _LOGGER.info("Error! Could'nt authenticate!")
            return False

        await self.update_devices()
        self.hass.loop.create_task(self.check_connections())
        self.hass.data[DOMAIN]['api'] = self

        return True

    async def get_device(self, deviceId: str) -> dict:
        query = { "operationName": "Query", "variables": {  "deviceId": deviceId  },      "query": "query Query($deviceId: ID!) {\n  getDeviceState(deviceId: $deviceId) {\n    desired\n    reported\n    timestamp\n    __typename\n  }\n}\n"}
        data = await self.api.endpoint('device', query )
        return json.loads(data['data']['getDeviceState']['reported'])

    async def get_latest_device_data(self, deviceId: str) -> dict:
        query ={
            "operationName": "Query",
            "variables": {
                "deviceId": deviceId
            },
            "query": "query Query($deviceId: String!) {\n  getLatestData(deviceId: $deviceId) {\n    deviceId\n    timestamp\n    sessionId\n    type\n    data\n    __typename\n  }\n}\n"
        }
        data = await self.api.endpoint('data', query)
        deviceData = json.loads(data['data']['getLatestData']['data'])
        deviceData['timestamp'] = data['data']['getLatestData']['timestamp']
        deviceData['type'] = data['data']['getLatestData']['type']
        return deviceData

    async def get_headers(self) -> dict:
        _LOGGER.warning("get_headers is DEPRICATED use HarviaSaunaApi.getHeaders() instead.")
        return await self.api.getHeaders()

    async def get_devices(self) -> list:
        if self.devices is None:
            await self.update_devices()
        return self.devices


    async def update_devices(self):
        self.devices = []

        query = {
        "operationName": "Query",
        "variables": {},
        "query": 'query Query {\n  getDeviceTree\n}\n'
        }

        deviceTree = await self.api.endpoint('device', query)
        if 'data' in deviceTree and 'getDeviceTree' in deviceTree['data']:
            devicesTreeData =  json.loads(deviceTree['data']['getDeviceTree'])
            if devicesTreeData:  # Ensure the list is not empty
                data_string = json.dumps(devicesTreeData, indent=4)
                devices = devicesTreeData[0]['c']
                for device in devices:
                    deviceId = device['i']['name']
                    _LOGGER.info("Found device: " + deviceId)
                    deviceData = await self.get_device(deviceId)
                    latestDeviceData = await self.get_latest_device_data(deviceId)
                    deviceObject  = HarviaDevice(sauna=self, id=deviceId)
                    await deviceObject.update_data(deviceData)
                    await deviceObject.update_data(latestDeviceData)
                    self.devices.append(deviceObject)
            else:
                _LOGGER.error("No devices found in the response.")
        else:
            _LOGGER.error("Unexpected response structure: missing 'data' or 'getDeviceTree'.")


    async def process_device_update(self, message: dict):

        _LOGGER.debug("Device update process: " + json.dumps(message))


        if message.get('type') != 'data':
            return
        if 'onStateUpdated' in message['payload']['data']:
            #{"id":"5ae58e56-d03a-4a7a-ab7d-9e8b308810a1","type":"data","payload":{"data":{"onStateUpdated":{"desired":null,"reported":"{\"active\":1,\"light\":0,\"fan\":0,\"steamEn\":0,\"targetTemp\":90,\"targetRh\":50,\"heatUpTime\":37,\"tz\":\"UTC+1 dst\",\"onTime\":360,\"dehumEn\":0,\"autoLight\":0,\"tempUnit\":0,\"timedStart\":\"ARhaABxEDGY=\",\"displayName\":\"Sauna boven\",\"cmd\":\"\",\"autoFan\":0,\"aromaEn\":0,\"aromaLevel\":0,\"wClkEn\":0,\"wClk\":\"\",\"deviceId\":\"e0b84f32-9eb0-4add-aad5-8d50886b3a66\",\"otaId\":\"\",\"__typename\":\"SAUNA\"}","timestamp":1712078650,"receiver":"5b53af61-8f6a-4fc5-ba12-d33af78dbac3","__typename":"StateResponse"}}}}
            data = json.loads(message['payload']['data']['onStateUpdated']['reported'])
            deviceId = data['deviceId']
        elif 'onDataUpdates' in message['payload']['data']:
            #{ "id": "898df439-408e-4d35-b7c6-9a5bc6d69e81", "type": "data", "payload": { "data": { "onDataUpdates": { "item": { "deviceId": "e0b84f32-9eb0-4add-aad5-8d50886b3a66", "timestamp": "1712161222726", "sessionId": "1", "type": "sauna", "data": "{\"targetTemp\":90,\"ph2RelayCounterLT\":0,\"remainingTime\":357,\"steamOn\":false,\"temperature\":27,\"humidity\":0,\"heatOn\":true,\"steamOnCounterLT\":0,\"steamOnCounter\":0,\"heatOnCounterLT\":0,\"heatOnCounter\":0,\"ph1RelayCounterLT\":1,\"ph3RelayCounterLT\":0,\"ph1RelayCounter\":1,\"ph3RelayCounter\":0,\"wifiRSSI\":-68,\"testVar1\":0,\"testVar2\":0,\"ph2RelayCounter\":0}", "__typename": "DataItem" }, "__typename": "UpdatedData" } } } }
            data = json.loads(message['payload']['data']['onDataUpdates']['item']['data'])
            data['timestamp'] = message['payload']['data']['onDataUpdates']['item']['timestamp']
            data['type'] = message['payload']['data']['onDataUpdates']['item']['type']
            deviceId =  message['payload']['data']['onDataUpdates']['item']['deviceId']
        else:
            return

        for device in self.devices:
            if device.id != deviceId:
                continue
            await device.update_data(data)

    async def device_mutation(self, deviceId: str, payload: str):
        payloadString =  json.dumps(payload, indent=4)
        query = {   "operationName": "Mutation",
                    "variables": {
                    "deviceId": deviceId,
                    "state": payloadString,
                    "getFullState": False
                    },
                    "query": "mutation Mutation($deviceId: ID!, $state: AWSJSON!, $getFullState: Boolean) {\n  requestStateChange(deviceId: $deviceId, state: $state, getFullState: $getFullState)\n}\n"
                }
        response = await self.api.endpoint('device', query)
        return response

    async def get_client(self) -> Cognito:
        if self.cognito == None:
            await self.authenticate_and_save_tokens()
        return self.cognito

    async def websock_get_url(self, endpoint) -> str:
        return await self.api.getWebsockUrlByEndpoint(endpoint)

    async def get_user_data(self):
        if self.user_data is not None:
            return self.user_data
        query= {
            "operationName": "Query",
            "variables": {},
            "query": "query Query {\n  getCurrentUserDetails {\n    email\n    organizationId\n    admin\n    given_name\n    family_name\n    superAdmin\n    rdUser\n    appSettings\n    __typename\n  }\n}\n"
        }
        data = await self.api.endpoint('users',query )
        self.user_data = data['data']['getCurrentUserDetails']
        return  self.user_data

    async def check_connections(self):
        while True:
            _LOGGER.debug("Checking websocket connections: ")
            if self.websockDevice is None:
                self.websockDevice = HarviaWebsock(self, 'device')
                await self.websockDevice.connect()

            if self.websockDeviceUser is None:
                self.websockDeviceUser = HarviaWebsock(self, 'device', True)
                await self.websockDeviceUser.connect()

            if self.websockData is None:
                self.websockData = HarviaWebsock(self, 'data')
                await self.websockData.connect()

            if self.websockDataUser is None:
                self.websockDataUser = HarviaWebsock(self, 'data', True)
                await self.websockDataUser.connect()

            if self.websockDevice and (self.websockDevice.websocket_task is None or self.websockDevice.websocket_task.done()):
                _LOGGER.debug("WebSocket Device: NOT RUNNING. Reconnecting!")
                await self.websockDevice.connect()
            else:
                _LOGGER.debug("\tWebsocket Device: RUNNING")

            if self.websockDeviceUser and (self.websockDeviceUser.websocket_task is None or self.websockDeviceUser.websocket_task.done()):
                _LOGGER.debug("WebSocket Device  (user): NOT RUNNING. Reconnecting!")
                await self.websockDeviceUser.connect()
            else:
                _LOGGER.debug("\tWebsocket Device  (user): RUNNING")

            if self.websockData and (self.websockData.websocket_task is None or self.websockData.websocket_task.done()):
                _LOGGER.debug("\tWebSocket Data: NOT RUNNING. Reconnecting!")
                await self.websockData.connect()
            else:
                _LOGGER.debug("\tWebsocket Data: RUNNING")

            if self.websockDataUser and (self.websockDataUser.websocket_task is None or self.websockDataUser.websocket_task.done()):
                _LOGGER.debug("\tWebSocket Data (user): NOT RUNNING. Reconnecting!")
                await self.websockDataUser.connect()
            else:
                _LOGGER.debug("\tWebsocket Data  (user): RUNNING")

            await asyncio.sleep(60)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Harvia Sauna integration."""
    boto3.set_stream_logger('custom_component.harvia_sauna')
    return True

async def async_setup_entry(hass, entry):
    """Set up Harvia Sauna from a config entry."""
    _LOGGER.debug(f"Setup entry...")

    # Retrieve configuration stored by the config flow
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)

    if not username or not password:
        _LOGGER.error("Username or password not configured.")
        return False

    storage = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    harvia_sauna = HarviaSauna(hass, storage, entry)
    await harvia_sauna.async_setup(entry)

    # Store the integration instance for platform/entity access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = harvia_sauna

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Clean up stored instance
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle reloading a config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
