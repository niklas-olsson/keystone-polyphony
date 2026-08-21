import sys

def run():
    with open("src/liminal_bridge/mesh.py", "r") as f:
        content = f.read()

    init_find = """        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.ble_enabled = ble_enabled
        self._mqtt_task: Optional[asyncio.Task] = None
        self._ble_task: Optional[asyncio.Task] = None

        # Callbacks for Pulse"""
    init_replace = """        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.ble_enabled = ble_enabled
        self._mqtt_task: Optional[asyncio.Task] = None
        self._ble_task: Optional[asyncio.Task] = None

        self._mqtt_client = None

        # BLE Server specific attributes
        self._ble_server = None
        self._ble_characteristic_uuid = f"0000{self.topic[:4]}-0000-1000-8000-00805f9b34fb"
        self._ble_service_uuid = f"{self.topic[:8]}-{self.topic[8:12]}-{self.topic[12:16]}-{self.topic[16:20]}-{self.topic[20:32]}"

        # Callbacks for Pulse"""

    if init_find in content:
        content = content.replace(init_find, init_replace)
    else:
        print("Could not find init block")


    stop_find = """    async def stop(self):
        \"\"\"Stops the sidecar.\"\"\"
        self.running = False

        # Cancel all background tasks"""
    stop_replace = """    async def stop(self):
        \"\"\"Stops the sidecar.\"\"\"
        self.running = False

        if self._ble_server:
            try:
                await self._ble_server.stop()
            except Exception:
                pass

        # Cancel all background tasks"""

    if stop_find in content:
        content = content.replace(stop_find, stop_replace)
    else:
        print("Could not find stop block")

    mqtt_find = """    async def _mqtt_listener(self):
        \"\"\"Background task that listens to MQTT broker for macro messages.\"\"\"
        if not self.mqtt_host:
            return

        while self.running:
            try:
                import aiomqtt
                async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                    topic_sub = f"keystone/{self.topic}/macro"
                    await client.subscribe(topic_sub)
                    print(f"Subscribed to MQTT topic: {topic_sub}")

                    async for message in client.messages:
                        if not self.running:
                            break
                        try:
                            msg_str = message.payload.decode()
                            msg_dict = json.loads(msg_str)
                            # Wrap it to simulate sidecar format for handle_message
                            if "e" in msg_dict and "s" in msg_dict and "p" in msg_dict:
                                # A bit hacky, but derive peer_id from p to conform to handle_message
                                derived_peer_id = hashlib.sha256(bytes.fromhex(msg_dict["p"])).hexdigest()[:16]
                                wrapped_msg = {
                                    "type": "message",
                                    "peer_id": derived_peer_id,
                                    "payload": msg_dict
                                }
                                await self._handle_message(wrapped_msg)
                        except Exception as e:
                            print(f"Error parsing incoming MQTT message: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"MQTT listener error: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)"""
    mqtt_replace = """    async def _mqtt_listener(self):
        \"\"\"Background task that listens to MQTT broker for macro messages.\"\"\"
        if not self.mqtt_host:
            return

        while self.running:
            try:
                import aiomqtt
                async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                    self._mqtt_client = client
                    topic_sub = f"keystone/{self.topic}/macro"
                    await client.subscribe(topic_sub)
                    print(f"Subscribed to MQTT topic: {topic_sub}")

                    async for message in client.messages:
                        if not self.running:
                            break
                        try:
                            msg_str = message.payload.decode()
                            msg_dict = json.loads(msg_str)
                            # Wrap it to simulate sidecar format for handle_message
                            if "e" in msg_dict and "s" in msg_dict and "p" in msg_dict:
                                # A bit hacky, but derive peer_id from p to conform to handle_message
                                derived_peer_id = hashlib.sha256(bytes.fromhex(msg_dict["p"])).hexdigest()[:16]
                                wrapped_msg = {
                                    "type": "message",
                                    "peer_id": derived_peer_id,
                                    "payload": msg_dict
                                }
                                await self._handle_message(wrapped_msg)
                        except Exception as e:
                            print(f"Error parsing incoming MQTT message: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"MQTT listener error: {e}. Retrying in 5 seconds...")
                self._mqtt_client = None
                await asyncio.sleep(5)"""
    if mqtt_find in content:
        content = content.replace(mqtt_find, mqtt_replace)
    else:
        print("Could not find mqtt_listener block")


    macro_find = """    async def broadcast_macro(self, payload: Any):
        \"\"\"Routes high urgency messages via macro transport (e.g., MQTT/5G).
        Defaults to Hyperswarm if no macro transport is configured.\"\"\"
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}

        if self.mqtt_host:
            try:
                import aiomqtt
                async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                    await client.publish(f"keystone/{self.topic}/macro", json.dumps(msg).encode())
                    # Successfully sent via MQTT, do not fallback
                    return
            except Exception as e:
                print(f"Warning: MQTT broadcast failed, falling back to Hyperswarm: {e}")

        # Default fallback to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)"""
    macro_replace = """    async def broadcast_macro(self, payload: Any):
        \"\"\"Routes high urgency messages via macro transport (e.g., MQTT/5G).
        Defaults to Hyperswarm if no macro transport is configured.\"\"\"
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}

        if self.mqtt_host:
            try:
                if getattr(self, "_mqtt_client", None):
                    # Reuse existing client connection
                    await self._mqtt_client.publish(f"keystone/{self.topic}/macro", json.dumps(msg).encode())
                    return
                else:
                    # Fallback to creating a one-off connection if background task failed
                    import aiomqtt
                    async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                        await client.publish(f"keystone/{self.topic}/macro", json.dumps(msg).encode())
                        return
            except Exception as e:
                print(f"Warning: MQTT broadcast failed, falling back to Hyperswarm: {e}")

        # Default fallback to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)"""
    if macro_find in content:
        content = content.replace(macro_find, macro_replace)
    else:
        print("Could not find broadcast_macro block")


    ble_listener_find = """    async def _ble_listener(self):
        \"\"\"Background task that uses Bleak scanner to find peers.
        Currently a stub waiting for full GATT server capability, but scans to show intent.\"\"\"
        if not self.ble_enabled:
            return

        while self.running:
            try:
                import bleak
                # Derived from swarm key, formatting to fake a UUID for scanner filter
                # Not a real valid standard UUID format here without more parsing, just simple
                service_uuid = self.topic[:32]
                service_uuid = f"{service_uuid[:8]}-{service_uuid[8:12]}-{service_uuid[12:16]}-{service_uuid[16:20]}-{service_uuid[20:32]}"

                print(f"BLE Listener starting scan for: {service_uuid}")

                def callback(device, advertisement_data):
                    # We found someone in our swarm!
                    # For now, we print. A real implementation would connect to GATT and read payload.
                    if service_uuid in advertisement_data.service_uuids:
                        print(f"BLE Peer discovered: {device.address}. GATT connect omitted in MVP.")

                scanner = bleak.BleakScanner(detection_callback=callback)
                await scanner.start()
                await asyncio.sleep(60.0) # Scan for 60s chunks
                await scanner.stop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"BLE listener error: {e}. Retrying in 10 seconds...")
                await asyncio.sleep(10)"""
    ble_listener_replace = """    async def _ble_listener(self):
        \"\"\"Background task that starts a BLE GATT Server via bless and periodically scans via bleak.\"\"\"
        if not self.ble_enabled:
            return

        try:
            import bleak
            import bless
            from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions
        except ImportError:
            print("Error: 'bleak' or 'bless' module not installed for BLE. BLE Listener disabled.")
            return

        # Start Bless GATT Server
        server_name = f"K-Polyphony"

        def read_request(characteristic):
            # When another node reads this, give them our last state or an empty payload.
            # In a true push system, they write to us, not read from us.
            return b"{}"

        def write_request(characteristic, value):
            try:
                msg_str = value.decode()
                msg_dict = json.loads(msg_str)
                if "e" in msg_dict and "s" in msg_dict and "p" in msg_dict:
                    derived_peer_id = hashlib.sha256(bytes.fromhex(msg_dict["p"])).hexdigest()[:16]
                    wrapped_msg = {
                        "type": "message",
                        "peer_id": derived_peer_id,
                        "payload": msg_dict
                    }
                    asyncio.create_task(self._handle_message(wrapped_msg))
            except Exception as e:
                print(f"Error parsing incoming BLE message: {e}")

        try:
            loop = asyncio.get_running_loop()
            self._ble_server = BlessServer(name=server_name, loop=loop)
            self._ble_server.read_request_func = read_request
            self._ble_server.write_request_func = write_request

            await self._ble_server.add_new_service(self._ble_service_uuid)

            char_flags = GATTCharacteristicProperties.read | GATTCharacteristicProperties.write | GATTCharacteristicProperties.indicate
            permissions = GATTAttributePermissions.readable | GATTAttributePermissions.writeable

            await self._ble_server.add_new_characteristic(
                self._ble_service_uuid,
                self._ble_characteristic_uuid,
                char_flags,
                None,
                permissions,
            )

            await self._ble_server.start()
            print(f"BLE GATT Server started. Advertising Service: {self._ble_service_uuid}")
        except Exception as e:
            print(f"Failed to start BLE GATT Server: {e}")
            self._ble_server = None

        # Periodically Scan for other Swarm peers (Bleak Client side of listener)
        while self.running:
            try:
                # We do not connect here; broadcast_micro handles connection and writing.
                # The scan just keeps discovery active.
                def callback(device, advertisement_data):
                    if self._ble_service_uuid.lower() in [str(u).lower() for u in advertisement_data.service_uuids]:
                        # We found someone in our swarm!
                        pass

                scanner = bleak.BleakScanner(detection_callback=callback)
                await scanner.start()
                await asyncio.sleep(30.0)
                await scanner.stop()
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"BLE scanner error: {e}")
                await asyncio.sleep(30)"""
    if ble_listener_find in content:
        content = content.replace(ble_listener_find, ble_listener_replace)
    else:
        print("Could not find ble_listener block")

    ble_macro_find = """    async def broadcast_micro(self, payload: Any):
        \"\"\"Routes low urgency messages via micro transport (e.g., BLE/Visual).
        Defaults to Hyperswarm if no micro transport is configured.\"\"\"
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}

        if self.ble_enabled:
            print("BLE Micro Broadcast initiated. GATT server omitted in MVP, falling back to Hyperswarm.")
            # We would write msg to the GATT characteristic here, but lacking a good Python BLE Server,
            # we rely on the sidecar to bridge the gap while demonstrating the intent.

        # Default fallback to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)"""
    ble_macro_replace = """    async def broadcast_micro(self, payload: Any):
        \"\"\"Routes low urgency messages via micro transport (e.g., BLE/Visual).
        Defaults to Hyperswarm if no micro transport is configured.\"\"\"
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}

        if self.ble_enabled:
            success = False
            try:
                import bleak
                msg_bytes = json.dumps(msg).encode()

                # Scan for a short time to find a peer
                devices = await bleak.BleakScanner.discover(timeout=3.0)
                target_device = None
                for d in devices:
                    # Note: on macOS service_uuids might be empty unless connected,
                    # but we'll try to match it if present.
                    if self._ble_service_uuid.lower() in [str(u).lower() for u in d.metadata.get('uuids', [])]:
                        target_device = d
                        break

                if target_device:
                    # Connect and Write
                    async with bleak.BleakClient(target_device) as client:
                        await client.write_gatt_char(self._ble_characteristic_uuid, msg_bytes, response=True)
                        success = True
                        return # Sent successfully via BLE
            except Exception as e:
                print(f"Warning: BLE micro broadcast failed, falling back to Hyperswarm: {e}")

            if success:
                return

        # Default fallback to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)"""

    if ble_macro_find in content:
        content = content.replace(ble_macro_find, ble_macro_replace)
    else:
        print("Could not find broadcast_micro block")

    with open("src/liminal_bridge/mesh.py", "w") as f:
        f.write(content)

if __name__ == "__main__":
    run()
