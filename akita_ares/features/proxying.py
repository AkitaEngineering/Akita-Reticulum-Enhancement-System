import base64
import json
import os
import re
import threading

from akita_ares.core.logger import get_logger

try:
    import RNS
    from RNS import Destination, Identity, Link, Packet

    RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False

    class Identity:
        @staticmethod
        def recall(*args, **kwargs):
            return None

    class Destination:
        IN, OUT, SINGLE, GROUP, PLAIN = 0, 1, 2, 3, 4

        @staticmethod
        def app_and_aspects_from_name(full_name):
            components = full_name.split(".")
            return components[0], components[1:]

        @staticmethod
        def hash_from_name_and_identity(full_name, identity):
            return None

    class Packet:
        def __init__(self, destination, data, *args, **kwargs):
            self.destination = destination
            self.data = data

        def send(self):
            return True

    class Link:
        def __init__(
            self,
            destination=None,
            established_callback=None,
            closed_callback=None,
            owner=None,
            peer_pub_bytes=None,
            peer_sig_pub_bytes=None,
            mode=1,
        ):
            self.destination = destination
            self.link_id = os.urandom(16)
            self._active = True
            self._closed_callback = closed_callback
            if established_callback is not None:
                established_callback(self)

        def set_resource_callback(self, callback):
            self._resource_callback = callback

        def set_link_closed_callback(self, callback):
            self._closed_callback = callback

        def send(self, data):
            return True

        def close(self):
            self._active = False
            if self._closed_callback is not None:
                self._closed_callback(self)

        def is_active(self):
            return self._active


PROXY_PROTOCOL_VERSION_1_0 = "1.0"
RNS_HASH_REGEX = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_MAX_PROXY_PAYLOAD_BYTES = 1024 * 1024


class ProxyManager:
    def __init__(self, config, rns_instance=None, metrics_monitor=None):
        self.logger = get_logger("Feature.ProxyManager")
        self.rns_instance = rns_instance
        self.metrics_monitor = metrics_monitor

        self.is_proxy_node = False
        self.proxy_routes_config = []
        self.proxy_routes = []
        self.service_destination = None
        self.active_client_links = {}
        self.pending_client_requests = {}
        self.proxy_protocol_version = PROXY_PROTOCOL_VERSION_1_0
        self.max_payload_size_bytes = DEFAULT_MAX_PROXY_PAYLOAD_BYTES
        self.current_listen_aspect = None
        self.lock = threading.Lock()

        if not RNS_AVAILABLE:
            self.logger.error("RNS library not found. ProxyManager cannot function.")
        elif not self.rns_instance:
            self.logger.error("RNS instance not provided. ProxyManager cannot function.")

        self.update_config(config)

    def update_config(self, config):
        config = config or {}
        previous_role = self.is_proxy_node
        previous_aspect = self.current_listen_aspect

        self.config = config
        self.proxy_routes_config = config.get("proxy_routes", [])
        self.proxy_protocol_version = config.get("proxy_protocol_version", PROXY_PROTOCOL_VERSION_1_0)
        self.max_payload_size_bytes = max(1, int(config.get("max_payload_size_bytes", DEFAULT_MAX_PROXY_PAYLOAD_BYTES)))
        self.current_listen_aspect = config.get("listen_on_aspect", "default_proxy_service")
        self.is_proxy_node = bool(config.get("is_proxy_node", False))

        self.logger.info(
            "ProxyMan cfg update. IsProxyNode:%s, Proto:%s, MaxPayload:%s",
            self.is_proxy_node,
            self.proxy_protocol_version,
            self.max_payload_size_bytes,
        )

        role_changed = self.is_proxy_node != previous_role
        aspect_changed = self.is_proxy_node and previous_aspect != self.current_listen_aspect

        if self.is_proxy_node:
            self._shutdown_client_proxy_resources()
            if role_changed or aspect_changed:
                self._shutdown_proxy_service_destination()
            if self.service_destination is None:
                self._setup_proxy_service_destination()
        else:
            if previous_role:
                self._shutdown_proxy_service_destination()
            self._configure_routes()

        if self.metrics_monitor:
            self.metrics_monitor.set_active_proxy_routes_count(len(self.proxy_routes))

    def _configure_routes(self):
        new_routes = []
        for route_cfg in self.proxy_routes_config:
            alias = route_cfg.get("alias")
            entry_name = route_cfg.get("entry_destination_name")
            exit_hash = route_cfg.get("exit_node_identity_hash")
            if not alias or not entry_name or not exit_hash:
                self.logger.warning("Skipping invalid proxy route config: %s", route_cfg)
                continue
            if not RNS_HASH_REGEX.match(exit_hash):
                self.logger.warning(
                    "Skipping invalid proxy route '%s': exit_node_identity_hash '%s' invalid format.",
                    alias,
                    exit_hash,
                )
                continue

            new_routes.append(
                {
                    "alias": alias,
                    "entry_destination_name_str": entry_name,
                    "exit_node_identity_hash_hex": exit_hash,
                    "target_network_prefix": route_cfg.get("target_network_prefix"),
                    "allow_all_targets": bool(route_cfg.get("allow_all_targets", False)),
                    "allowed_target_aspects": list(route_cfg.get("allowed_target_aspects", [])),
                }
            )

        self.proxy_routes = new_routes
        self.logger.info("Client proxy routes configured: %s valid routes.", len(self.proxy_routes))

    def _setup_proxy_service_destination(self):
        if not RNS_AVAILABLE or not self.rns_instance:
            self.logger.error("RNS not available for proxy service.")
            return
        if self.service_destination is not None:
            self.logger.info("Proxy service destination already exists.")
            return

        destination_name = ["ares", "proxy", self.current_listen_aspect]
        try:
            self.service_destination = Destination(
                self.rns_instance.identity,
                Destination.IN,
                Destination.SINGLE,
                *destination_name,
            )
            self.service_destination.set_link_established_callback(self._handle_client_link_established)
            self.logger.info(
                "Proxy node listening on RNS destination: %s (%s)",
                ".".join(destination_name),
                getattr(self.service_destination, "hexhash", "unknown"),
            )
        except Exception as exc:
            self.logger.error("Failed to create proxy service destination: %s", exc, exc_info=True)
            self.service_destination = None

    def _handle_client_link_established(self, link):
        if not RNS_AVAILABLE:
            return

        link_id = self._link_id_hex(link)
        with self.lock:
            self.active_client_links[link_id] = link

        self.logger.info("New client link established to proxy service: %s", link_id)
        link.set_resource_callback(lambda resource: self._handle_proxied_request_on_link(resource, link))
        link.set_link_closed_callback(self._handle_client_link_closed)

        if self.metrics_monitor:
            self.metrics_monitor.set_active_proxy_clients_count(len(self.active_client_links))

    def _handle_client_link_closed(self, link):
        if not RNS_AVAILABLE:
            return

        link_id = self._link_id_hex(link)
        closed_reqs = 0

        with self.lock:
            if link_id in self.active_client_links:
                del self.active_client_links[link_id]
                self.logger.info("Client link closed: %s", link_id)

            for request_id, request_link in list(self.pending_client_requests.items()):
                if request_link.link_id == link.link_id:
                    del self.pending_client_requests[request_id]
                    closed_reqs += 1

        if closed_reqs > 0:
            self.logger.debug("Removed %s pending requests for closed link %s.", closed_reqs, link_id)
        if self.metrics_monitor:
            self.metrics_monitor.set_active_proxy_clients_count(len(self.active_client_links))

    def _handle_proxied_request_on_link(self, resource, client_link):
        if not RNS_AVAILABLE:
            return

        client_link_id_hex = self._link_id_hex(client_link)
        request_id = None
        self.logger.debug(
            "Proxy node received data from client link %s (size %s bytes).",
            client_link_id_hex,
            len(resource.data),
        )

        try:
            message = json.loads(resource.data.decode("utf-8"))
            request_id = message.get("request_id")
        except Exception as exc:
            self.logger.error("Error decoding proxy request from %s: %s", client_link_id_hex, exc)
            self._send_proxy_response(client_link, request_id or "unknown", error=f"request_decode_error: {exc}")
            return

        if message.get("version") != self.proxy_protocol_version:
            self.logger.warning(
                "Incompatible proxy protocol version from %s. Got %s",
                client_link_id_hex,
                message.get("version"),
            )
            self._send_proxy_response(client_link, request_id, error="incompatible_protocol_version")
            return

        message_type = message.get("type", "data_oneway")
        target_hash_hex = message.get("target_destination_hash")
        target_name = message.get("target_destination_name")
        payload_b64 = message.get("payload")

        if not request_id or not target_hash_hex or not target_name or payload_b64 is None:
            self.logger.error("Invalid proxy request from %s: missing required fields.", client_link_id_hex)
            self._send_proxy_response(client_link, request_id, error="invalid_request_format")
            return

        if not RNS_HASH_REGEX.match(target_hash_hex):
            self.logger.error("Invalid target hash format from %s: %s", client_link_id_hex, target_hash_hex)
            self._send_proxy_response(client_link, request_id, error="invalid_target_hash_format")
            return

        if not self._is_valid_destination_name(target_name):
            self.logger.error("Invalid target destination name from %s: %s", client_link_id_hex, target_name)
            self._send_proxy_response(client_link, request_id, error="invalid_target_destination_name")
            return

        try:
            payload_bytes = base64.b64decode(payload_b64, validate=True)
        except Exception as exc:
            self.logger.error("Error decoding proxy payload from %s: %s", client_link_id_hex, exc)
            self._send_proxy_response(client_link, request_id, error="invalid_payload_encoding")
            return

        if len(payload_bytes) > self.max_payload_size_bytes:
            self.logger.warning(
                "Rejected proxy request %s from %s: payload size %s exceeds limit %s.",
                request_id,
                client_link_id_hex,
                len(payload_bytes),
                self.max_payload_size_bytes,
            )
            self._send_proxy_response(client_link, request_id, error="payload_too_large")
            return

        if message_type != "data_oneway":
            self.logger.warning(
                "Rejected proxy request %s from %s: response proxying is not supported on this RNS transport.",
                request_id,
                client_link_id_hex,
            )
            self._send_proxy_response(client_link, request_id, error="response_proxying_not_supported")
            return

        target_destination, target_error = self._build_outbound_destination(target_hash_hex, target_name)
        if target_error:
            self.logger.error(
                "Unable to build target destination for request %s from %s: %s",
                request_id,
                client_link_id_hex,
                target_error,
            )
            self._send_proxy_response(client_link, request_id, error=target_error)
            return

        try:
            Packet(target_destination, payload_bytes).send()
            self.logger.info(
                "Forwarded one-way proxy request %s from %s to %s.",
                request_id,
                client_link_id_hex,
                target_hash_hex[:8],
            )
            if self.metrics_monitor:
                self.metrics_monitor.increment_proxied_packets("proxy_node_service", direction="sent_to_target")
        except Exception as exc:
            self.logger.error(
                "Error sending proxied packet to target %s: %s",
                target_hash_hex[:8],
                exc,
                exc_info=True,
            )
            self._send_proxy_response(client_link, request_id, error=f"proxy_send_failed: {exc}")

    def _build_outbound_destination(self, target_hash_hex, target_destination_name):
        if not RNS_AVAILABLE or not self.rns_instance:
            return None, "rns_unavailable"

        target_hash_bytes = bytes.fromhex(target_hash_hex)
        target_identity = Identity.recall(target_hash_bytes)
        if target_identity is None:
            try:
                RNS.Transport.request_path(target_hash_bytes)
            except Exception:
                pass
            return None, "unknown_target_destination"

        try:
            app_name, aspects = Destination.app_and_aspects_from_name(target_destination_name)
            destination = Destination(target_identity, Destination.OUT, Destination.SINGLE, app_name, *aspects)
        except Exception as exc:
            return None, f"invalid_target_destination_name: {exc}"

        resolved_hash = getattr(destination, "hash", None)
        if isinstance(resolved_hash, bytes) and resolved_hash.hex() != target_hash_hex:
            return None, "target_destination_name_hash_mismatch"

        return destination, None

    def send_via_proxy(
        self,
        target_dest_hash,
        data_to_send,
        proxy_alias=None,
        response_callback=None,
        timeout_s=30,
        target_destination_name=None,
    ):
        if not RNS_AVAILABLE or not self.rns_instance:
            self.logger.error("RNS not available for proxy send.")
            self._invoke_response_callback(response_callback, None, "rns_unavailable")
            return None

        route = next((route for route in self.proxy_routes if route["alias"] == proxy_alias), None)
        if route is None:
            route = self.proxy_routes[0] if self.proxy_routes else None
        if route is None:
            error = f"proxy route '{proxy_alias or 'default'}' not found"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        if not RNS_HASH_REGEX.match(target_dest_hash):
            error = f"invalid target_destination_hash format: {target_dest_hash}"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        if not target_destination_name:
            error = "target_destination_name is required for proxying"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        if not self._is_valid_destination_name(target_destination_name):
            error = f"invalid target_destination_name: {target_destination_name}"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        if response_callback is not None:
            error = "response callbacks are not supported with the current RNS proxy transport"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        try:
            payload_bytes = bytes(data_to_send)
        except Exception as exc:
            error = f"proxy payload must be bytes-like: {exc}"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        if len(payload_bytes) > self.max_payload_size_bytes:
            error = f"proxy payload exceeds max_payload_size_bytes ({self.max_payload_size_bytes})"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        try:
            proxy_identity_hash = bytes.fromhex(route["exit_node_identity_hash_hex"])
            proxy_server_identity = Identity.recall(proxy_identity_hash, from_identity_hash=True)
            if proxy_server_identity is None:
                proxy_destination_hash = Destination.hash_from_name_and_identity(
                    route["entry_destination_name_str"],
                    proxy_identity_hash,
                )
                if proxy_destination_hash is not None:
                    try:
                        RNS.Transport.request_path(proxy_destination_hash)
                    except Exception:
                        pass
                self.logger.warning(
                    "Proxy server identity is unknown for route '%s'; wait for announce/path discovery and retry.",
                    route["alias"],
                )
                return None

            proxy_entry_dest = Destination(
                proxy_server_identity,
                Destination.OUT,
                Destination.SINGLE,
                *route["entry_destination_name_str"].split("."),
            )
        except ValueError as exc:
            self.logger.error(
                "Invalid proxy route identity hash for '%s': %s",
                route["alias"],
                exc,
            )
            return None
        except Exception as exc:
            self.logger.error(
                "Failed to create RNS destination for proxy entry '%s': %s",
                route["entry_destination_name_str"],
                exc,
                exc_info=True,
            )
            return None

        request_id = os.urandom(8).hex()
        proxy_req_data = {
            "version": self.proxy_protocol_version,
            "type": "data_oneway",
            "request_id": request_id,
            "target_destination_hash": target_dest_hash,
            "target_destination_name": target_destination_name,
            "payload": base64.b64encode(payload_bytes).decode("utf-8"),
        }

        try:
            proxy_req_bytes = json.dumps(proxy_req_data).encode("utf-8")
        except Exception as exc:
            self.logger.error("Failed to encode proxy request %s: %s", request_id, exc)
            return None

        established_event = threading.Event()
        try:
            link_to_proxy = Link(
                proxy_entry_dest,
                established_callback=lambda link: established_event.set(),
                closed_callback=lambda link: self.logger.info(
                    "Link to proxy server %s closed.",
                    self._destination_preview(getattr(link, "destination", None)),
                ),
            )
            if not established_event.wait(timeout=timeout_s):
                self.logger.error(
                    "Timeout establishing link to proxy server %s.",
                    self._destination_preview(proxy_entry_dest),
                )
                link_to_proxy.close()
                return None

            link_to_proxy.send(proxy_req_bytes)
            if self.metrics_monitor:
                self.metrics_monitor.increment_proxied_packets(route["alias"], direction="sent_to_proxy")
            link_to_proxy.close()
            self.logger.debug(
                "One-way data sent to proxy %s for request %s.",
                self._destination_preview(proxy_entry_dest),
                request_id,
            )
            return request_id
        except Exception as exc:
            self.logger.error("Error in send_via_proxy for '%s': %s", route["alias"], exc, exc_info=True)
            return None

    def _handle_proxy_response_on_client(self, resource, original_response_callback, original_request_id):
        if not RNS_AVAILABLE:
            return

        link_id_hex = self._link_id_hex(getattr(resource, "link", None))
        self.logger.debug(
            "Client received resource from proxy link %s. Size: %s",
            link_id_hex,
            len(resource.data),
        )
        try:
            proxy_response = json.loads(resource.data.decode("utf-8"))
            received_request_id = proxy_response.get("request_id")
            if received_request_id != original_request_id:
                self.logger.warning(
                    "Received proxy response with mismatched request_id (%s != %s). Ignoring.",
                    received_request_id,
                    original_request_id,
                )
                return

            if "error" in proxy_response:
                error_msg = proxy_response["error"]
                self.logger.error("Proxy returned error for request_id %s: %s", original_request_id, error_msg)
                self._invoke_response_callback(original_response_callback, None, error_msg)
            elif "payload" in proxy_response:
                try:
                    actual_response_data = base64.b64decode(proxy_response["payload"], validate=True)
                except Exception as decode_error:
                    error_msg = f"Proxy response payload decode error: {decode_error}"
                    self.logger.error(error_msg)
                    self._invoke_response_callback(original_response_callback, None, error_msg)
                    return

                self.logger.debug(
                    "Received response payload for request %s (size: %s bytes).",
                    original_request_id,
                    len(actual_response_data),
                )
                self._invoke_response_callback(original_response_callback, actual_response_data, None)
            else:
                self.logger.warning(
                    "Received proxy response for %s with no payload or error.",
                    original_request_id,
                )
                self._invoke_response_callback(original_response_callback, None, "empty_proxy_response")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            error_msg = f"Proxy response JSON decode error: {exc}"
            self.logger.error(error_msg)
            self._invoke_response_callback(original_response_callback, None, error_msg)
        except Exception as exc:
            error_msg = f"Unexpected proxy response processing error: {exc}"
            self.logger.error(error_msg, exc_info=True)
            self._invoke_response_callback(original_response_callback, None, error_msg)
        finally:
            link = getattr(resource, "link", None)
            if link and hasattr(link, "is_active") and link.is_active():
                self.logger.debug(
                    "Closing link to proxy %s after receiving response for %s.",
                    link_id_hex,
                    original_request_id,
                )
                link.close()

    def periodic_check(self):
        with self.lock:
            if self.is_proxy_node:
                count = len(self.active_client_links)
                self.logger.debug("ProxyMan (node) check. Active links:%s", count)
                if self.metrics_monitor:
                    self.metrics_monitor.set_active_proxy_clients_count(count)
            else:
                self.logger.debug("ProxyMan (client) check. Config routes:%s", len(self.proxy_routes))

    def _shutdown_client_proxy_resources(self):
        self.logger.info("Shutting down client proxy resources.")
        self.proxy_routes = []

    def _shutdown_proxy_service_destination(self):
        if not RNS_AVAILABLE:
            return

        with self.lock:
            service_destination = self.service_destination
            self.service_destination = None
            links = list(self.active_client_links.items())
            self.active_client_links.clear()
            self.pending_client_requests.clear()

        if service_destination:
            self.logger.info(
                "Closing proxy service destination %s...",
                getattr(service_destination, "hexhash", "unknown"),
            )
            try:
                if hasattr(service_destination, "close"):
                    service_destination.close()
            except Exception as exc:
                self.logger.error("Error closing proxy service destination: %s", exc)

        for link_id, link in links:
            self.logger.debug("Closing active client link %s during shutdown.", link_id)
            try:
                if hasattr(link, "is_active") and link.is_active():
                    link.close()
            except Exception as exc:
                self.logger.error("Error closing client link %s: %s", link_id, exc)

        if self.metrics_monitor:
            self.metrics_monitor.set_active_proxy_clients_count(0)

    def shutdown(self):
        self.logger.info("ProxyManager shutting down...")
        if self.is_proxy_node:
            self._shutdown_proxy_service_destination()
        else:
            self._shutdown_client_proxy_resources()

    def _send_proxy_response(self, client_link, request_id, error=None, payload=None):
        if not client_link or (hasattr(client_link, "is_active") and not client_link.is_active()):
            return

        response = {
            "version": self.proxy_protocol_version,
            "type": "response",
            "request_id": request_id,
        }
        if error is not None:
            response["error"] = error
        if payload is not None:
            response["payload"] = base64.b64encode(payload).decode("utf-8")

        try:
            client_link.send(json.dumps(response).encode("utf-8"))
        except Exception as exc:
            self.logger.error("Failed to send proxy response for request %s: %s", request_id, exc)

    @staticmethod
    def _invoke_response_callback(callback, payload, error):
        if callback is None:
            return
        callback(payload, error)

    @staticmethod
    def _is_valid_destination_name(destination_name):
        if not isinstance(destination_name, str) or not destination_name:
            return False
        components = destination_name.split(".")
        return all(component for component in components)

    @staticmethod
    def _link_id_hex(link):
        link_id = getattr(link, "link_id", None)
        if isinstance(link_id, bytes):
            return link_id.hex()
        if hasattr(link_id, "hex"):
            return link_id.hex()
        return str(link_id or "unknown")

    @staticmethod
    def _destination_preview(destination):
        if destination is None:
            return "unknown"
        if hasattr(destination, "hexhash"):
            return destination.hexhash[:8]
        destination_hash = getattr(destination, "hash", None)
        if isinstance(destination_hash, bytes):
            return destination_hash.hex()[:8]
        return str(destination)[:8]
