import base64
import json
import os
import re
import threading
import time

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

        def request(self, path, data=None, response_callback=None, failed_callback=None, progress_callback=None, timeout=None):
            return False

        def close(self):
            self._active = False
            if self._closed_callback is not None:
                self._closed_callback(self)

        def is_active(self):
            return self._active


PROXY_PROTOCOL_VERSION_1_0 = "1.0"
RNS_HASH_REGEX = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_MAX_PROXY_PAYLOAD_BYTES = 1024 * 1024
DEFAULT_PROXY_REQUEST_TIMEOUT_SECONDS = 30.0


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
        self.pending_target_links = {}
        self.pending_request_started_at = {}
        self.pending_target_request_started_at = {}
        self.proxy_protocol_version = PROXY_PROTOCOL_VERSION_1_0
        self.max_payload_size_bytes = DEFAULT_MAX_PROXY_PAYLOAD_BYTES
        self.default_request_timeout_s = DEFAULT_PROXY_REQUEST_TIMEOUT_SECONDS
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
        configured_request_timeout = config.get("default_request_timeout_seconds", DEFAULT_PROXY_REQUEST_TIMEOUT_SECONDS)
        default_request_timeout_s, timeout_error = self._coerce_request_timeout(configured_request_timeout)
        if timeout_error:
            self.logger.warning(
                "Invalid default_request_timeout_seconds %r; using fallback %.3f.",
                configured_request_timeout,
                DEFAULT_PROXY_REQUEST_TIMEOUT_SECONDS,
            )
            default_request_timeout_s = DEFAULT_PROXY_REQUEST_TIMEOUT_SECONDS
        self.default_request_timeout_s = default_request_timeout_s
        self.current_listen_aspect = config.get("listen_on_aspect", "default_proxy_service")
        self.is_proxy_node = bool(config.get("is_proxy_node", False))

        self.logger.info(
            "ProxyMan cfg update. IsProxyNode:%s, Proto:%s, MaxPayload:%s, DefaultReqTimeout:%s",
            self.is_proxy_node,
            self.proxy_protocol_version,
            self.max_payload_size_bytes,
            self.default_request_timeout_s,
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
                    "target_network_prefix": (route_cfg.get("target_network_prefix") or "").strip() or None,
                    "allow_all_targets": bool(route_cfg.get("allow_all_targets", False)),
                    "allowed_target_aspects": [
                        aspect.strip()
                        for aspect in route_cfg.get("allowed_target_aspects", [])
                        if isinstance(aspect, str) and aspect.strip()
                    ],
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
        target_links_to_close = []

        with self.lock:
            if link_id in self.active_client_links:
                del self.active_client_links[link_id]
                self.logger.info("Client link closed: %s", link_id)

            for request_id, request_link in list(self.pending_client_requests.items()):
                if request_link.link_id == link.link_id:
                    del self.pending_client_requests[request_id]
                    target_link = self.pending_target_links.pop(request_id, None)
                    started_at = self.pending_request_started_at.pop(request_id, None)
                    target_request_started_at = self.pending_target_request_started_at.pop(request_id, None)
                    if target_link is not None:
                        target_links_to_close.append((request_id, target_link, started_at, target_request_started_at))
                    closed_reqs += 1

        for request_id, target_link, started_at, target_request_started_at in target_links_to_close:
            self._record_proxy_request_phase(
                "proxy_node_service",
                "request",
                "target_request_service",
                "closed",
                target_request_started_at,
            )
            self._record_proxy_request_terminal("proxy_node_service", "request", "closed", started_at)
            self.logger.debug("Closing target link for abandoned proxy request %s.", request_id)
            try:
                if hasattr(target_link, "is_active") and target_link.is_active():
                    target_link.close()
            except Exception as exc:
                self.logger.error("Error closing target link for request %s: %s", request_id, exc)

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
        request_path = message.get("request_path")
        request_timeout_s = message.get("request_timeout_s")
        request_started_at = time.monotonic() if message_type == "request" else None

        if not request_id or not target_hash_hex or not target_name or payload_b64 is None:
            self.logger.error("Invalid proxy request from %s: missing required fields.", client_link_id_hex)
            self._send_proxy_response(client_link, request_id, error="invalid_request_format")
            return

        if message_type not in {"data_oneway", "request"}:
            self.logger.error("Invalid proxy request type from %s: %s", client_link_id_hex, message_type)
            self._send_proxy_response(client_link, request_id, error="invalid_request_type")
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
            if message_type == "request":
                self._record_proxy_request_terminal("proxy_node_service", "request", "invalid", request_started_at)
            self._send_proxy_response(client_link, request_id, error="payload_too_large")
            return

        if message_type == "request":
            if not self._is_valid_request_path(request_path):
                self.logger.error("Invalid request path from %s: %s", client_link_id_hex, request_path)
                self._record_proxy_request_terminal("proxy_node_service", "request", "invalid", request_started_at)
                self._send_proxy_response(client_link, request_id, error="invalid_request_path")
                return

            request_timeout_value = request_timeout_s if request_timeout_s is not None else self.default_request_timeout_s
            request_timeout_s, timeout_error = self._coerce_request_timeout(request_timeout_value)
            if timeout_error:
                self.logger.error("Invalid request timeout from %s: %s", client_link_id_hex, timeout_error)
                self._record_proxy_request_terminal("proxy_node_service", "request", "invalid", request_started_at)
                self._send_proxy_response(client_link, request_id, error=timeout_error)
                return

        target_destination, target_error = self._build_outbound_destination(target_hash_hex, target_name)
        if target_error:
            self.logger.error(
                "Unable to build target destination for request %s from %s: %s",
                request_id,
                client_link_id_hex,
                target_error,
            )
            if message_type == "request":
                self._record_proxy_request_terminal(
                    "proxy_node_service",
                    "request",
                    self._classify_request_outcome(target_error),
                    request_started_at,
                )
            self._send_proxy_response(client_link, request_id, error=target_error)
            return

        if message_type == "request":
            self._forward_request_via_link(
                client_link,
                request_id,
                target_destination,
                request_path,
                payload_bytes,
                request_timeout_s,
                request_started_at,
            )
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
            self._record_proxy_request_outcome("proxy_node_service", "data_oneway", "success")
        except Exception as exc:
            self.logger.error(
                "Error sending proxied packet to target %s: %s",
                target_hash_hex[:8],
                exc,
                exc_info=True,
            )
            self._record_proxy_request_outcome("proxy_node_service", "data_oneway", "failed")
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
        request_path=None,
        request_timeout_s=None,
    ):
        if not RNS_AVAILABLE or not self.rns_instance:
            self.logger.error("RNS not available for proxy send.")
            self._invoke_response_callback(response_callback, None, "rns_unavailable")
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

        if response_callback is not None and not self._is_valid_request_path(request_path):
            error = "request_path is required when response_callback is provided"
            self.logger.error(error)
            self._invoke_response_callback(response_callback, None, error)
            return None

        request_mode = "request" if response_callback is not None else "data_oneway"

        route, route_error = self._select_proxy_route(proxy_alias, target_destination_name)
        if route is None:
            self.logger.error(route_error)
            self._invoke_response_callback(response_callback, None, route_error)
            return None

        self.logger.info(
            "Client sending to %s via proxy '%s' (entry: %s)",
            target_destination_name,
            route["alias"],
            route["entry_destination_name_str"],
        )

        request_timeout_value = request_timeout_s if request_timeout_s is not None else self.default_request_timeout_s
        request_timeout_s, timeout_error = self._coerce_request_timeout(request_timeout_value)
        if timeout_error:
            self.logger.error(timeout_error)
            self._invoke_response_callback(response_callback, None, timeout_error)
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

        request_state = {
            "completed": False,
            "route_alias": route["alias"],
            "mode": request_mode,
            "started_at": time.monotonic() if request_mode == "request" else None,
            "proxy_link_ready_at": None,
        }

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
                if request_mode == "request":
                    self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "failed", request_state["started_at"])
                    self._record_proxy_request_terminal(route["alias"], request_mode, "failed", request_state["started_at"])
                else:
                    self._record_proxy_request_outcome(route["alias"], request_mode, "failed")
                self._invoke_response_callback(
                    response_callback,
                    None,
                    f"proxy server identity is unknown for route '{route['alias']}'; wait for announce/path discovery and retry",
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
            if request_mode == "request":
                self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "failed", request_state["started_at"])
                self._record_proxy_request_terminal(route["alias"], request_mode, "failed", request_state["started_at"])
            else:
                self._record_proxy_request_outcome(route["alias"], request_mode, "failed")
            self._invoke_response_callback(response_callback, None, f"invalid proxy route identity hash for '{route['alias']}': {exc}")
            return None
        except Exception as exc:
            self.logger.error(
                "Failed to create RNS destination for proxy entry '%s': %s",
                route["entry_destination_name_str"],
                exc,
                exc_info=True,
            )
            if request_mode == "request":
                self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "failed", request_state["started_at"])
                self._record_proxy_request_terminal(route["alias"], request_mode, "failed", request_state["started_at"])
            else:
                self._record_proxy_request_outcome(route["alias"], request_mode, "failed")
            self._invoke_response_callback(
                response_callback,
                None,
                f"failed to create proxy destination for '{route['entry_destination_name_str']}': {exc}",
            )
            return None

        request_id = os.urandom(8).hex()
        proxy_req_data = {
            "version": self.proxy_protocol_version,
            "type": request_mode,
            "request_id": request_id,
            "target_destination_hash": target_dest_hash,
            "target_destination_name": target_destination_name,
            "payload": base64.b64encode(payload_bytes).decode("utf-8"),
        }
        if request_mode == "request":
            proxy_req_data["request_path"] = request_path
            proxy_req_data["request_timeout_s"] = request_timeout_s

        try:
            proxy_req_bytes = json.dumps(proxy_req_data).encode("utf-8")
        except Exception as exc:
            self.logger.error("Failed to encode proxy request %s: %s", request_id, exc)
            if request_mode == "request":
                self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "failed", request_state["started_at"])
                self._record_proxy_request_terminal(route["alias"], request_mode, "failed", request_state["started_at"])
            else:
                self._record_proxy_request_outcome(route["alias"], request_mode, "failed")
            self._invoke_response_callback(response_callback, None, f"failed to encode proxy request: {exc}")
            return None

        established_event = threading.Event()
        link_to_proxy = None
        try:
            link_to_proxy = Link(
                proxy_entry_dest,
                established_callback=lambda link: established_event.set(),
                closed_callback=lambda link: self._handle_proxy_link_closed(
                    link,
                    request_id,
                    response_callback,
                    request_state,
                ),
            )
            if response_callback is not None:
                link_to_proxy.set_resource_callback(
                    lambda resource: self._handle_proxy_response_on_client(
                        resource,
                        response_callback,
                        request_id,
                        request_state,
                    )
                )
            if not established_event.wait(timeout=timeout_s):
                self.logger.error(
                    "Timeout establishing link to proxy server %s.",
                    self._destination_preview(proxy_entry_dest),
                )
                if response_callback is not None:
                    self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "timeout", request_state["started_at"])
                self._fail_pending_proxy_request(response_callback, request_state, "timeout establishing link to proxy server")
                link_to_proxy.close()
                return None

            if response_callback is not None:
                request_state["proxy_link_ready_at"] = time.monotonic()
                self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "success", request_state["started_at"])

            link_to_proxy.send(proxy_req_bytes)
            if self.metrics_monitor:
                self.metrics_monitor.increment_proxied_packets(route["alias"], direction="sent_to_proxy")
            if response_callback is None:
                self._record_proxy_request_outcome(route["alias"], request_mode, "success")
                link_to_proxy.close()
                self.logger.debug(
                    "One-way data sent to proxy %s for request %s.",
                    self._destination_preview(proxy_entry_dest),
                    request_id,
                )
            else:
                self.logger.debug(
                    "Request/response proxy request %s sent to proxy %s using path %s.",
                    request_id,
                    self._destination_preview(proxy_entry_dest),
                    request_path,
                )
            return request_id
        except Exception as exc:
            self.logger.error("Error in send_via_proxy for '%s': %s", route["alias"], exc, exc_info=True)
            if link_to_proxy is not None and hasattr(link_to_proxy, "is_active") and link_to_proxy.is_active():
                try:
                    link_to_proxy.close()
                except Exception:
                    pass
            if response_callback is None:
                self._record_proxy_request_outcome(route["alias"], request_mode, "failed")
            elif request_state["proxy_link_ready_at"] is None:
                self._record_proxy_request_phase(route["alias"], request_mode, "proxy_link_setup", "failed", request_state["started_at"])
            self._fail_pending_proxy_request(response_callback, request_state, f"proxy send failed: {exc}")
            return None

    def _handle_proxy_response_on_client(self, resource, original_response_callback, original_request_id, request_state):
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
                self._complete_request_state(request_state, self._classify_request_outcome(error_msg))
                self._invoke_response_callback(original_response_callback, None, error_msg)
            elif "payload" in proxy_response:
                try:
                    actual_response_data = base64.b64decode(proxy_response["payload"], validate=True)
                except Exception as decode_error:
                    error_msg = f"Proxy response payload decode error: {decode_error}"
                    self.logger.error(error_msg)
                    self._complete_request_state(request_state, "invalid")
                    self._invoke_response_callback(original_response_callback, None, error_msg)
                    return

                self.logger.debug(
                    "Received response payload for request %s (size: %s bytes).",
                    original_request_id,
                    len(actual_response_data),
                )
                self._complete_request_state(request_state, "success")
                self._invoke_response_callback(original_response_callback, actual_response_data, None)
            else:
                self.logger.warning(
                    "Received proxy response for %s with no payload or error.",
                    original_request_id,
                )
                self._complete_request_state(request_state, "failed")
                self._invoke_response_callback(original_response_callback, None, "empty_proxy_response")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            error_msg = f"Proxy response JSON decode error: {exc}"
            self.logger.error(error_msg)
            self._complete_request_state(request_state, "invalid")
            self._invoke_response_callback(original_response_callback, None, error_msg)
        except Exception as exc:
            error_msg = f"Unexpected proxy response processing error: {exc}"
            self.logger.error(error_msg, exc_info=True)
            self._complete_request_state(request_state, "failed")
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
            target_links = list(self.pending_target_links.items())
            self.active_client_links.clear()
            self.pending_client_requests.clear()
            self.pending_target_links.clear()
            self.pending_request_started_at.clear()
            self.pending_target_request_started_at.clear()

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

        for request_id, target_link in target_links:
            self.logger.debug("Closing target link for pending request %s during shutdown.", request_id)
            try:
                if hasattr(target_link, "is_active") and target_link.is_active():
                    target_link.close()
            except Exception as exc:
                self.logger.error("Error closing target link for request %s: %s", request_id, exc)

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

    def _record_proxy_request_outcome(self, proxy_alias, mode, outcome):
        if self.metrics_monitor and hasattr(self.metrics_monitor, "increment_proxy_request_outcome"):
            self.metrics_monitor.increment_proxy_request_outcome(proxy_alias, mode, outcome)

    def _record_proxy_request_latency(self, proxy_alias, mode, outcome, started_at):
        if started_at is None:
            return
        if self.metrics_monitor and hasattr(self.metrics_monitor, "record_proxy_request_duration"):
            self.metrics_monitor.record_proxy_request_duration(proxy_alias, mode, outcome, max(0.0, time.monotonic() - started_at))

    def _record_proxy_request_phase(self, proxy_alias, mode, phase, outcome, started_at):
        if started_at is None:
            return
        if self.metrics_monitor and hasattr(self.metrics_monitor, "record_proxy_request_phase_duration"):
            self.metrics_monitor.record_proxy_request_phase_duration(proxy_alias, mode, phase, outcome, max(0.0, time.monotonic() - started_at))

    def _record_proxy_request_terminal(self, proxy_alias, mode, outcome, started_at):
        self._record_proxy_request_outcome(proxy_alias, mode, outcome)
        self._record_proxy_request_latency(proxy_alias, mode, outcome, started_at)

    def _record_proxy_policy_denial(self, proxy_alias, reason):
        if self.metrics_monitor and hasattr(self.metrics_monitor, "increment_proxy_policy_denial"):
            self.metrics_monitor.increment_proxy_policy_denial(proxy_alias, reason)

    @staticmethod
    def _invoke_response_callback(callback, payload, error):
        if callback is None:
            return
        callback(payload, error)

    @staticmethod
    def _classify_request_outcome(error):
        if error is None:
            return "success"
        error_text = str(error).lower()
        if "timeout" in error_text:
            return "timeout"
        if "closed" in error_text:
            return "closed"
        if "invalid" in error_text or "decode" in error_text:
            return "invalid"
        return "failed"

    def _complete_request_state(self, request_state, outcome):
        if request_state["completed"]:
            return
        request_state["completed"] = True
        self._record_proxy_request_phase(
            request_state["route_alias"],
            request_state["mode"],
            "proxy_roundtrip",
            outcome,
            request_state.get("proxy_link_ready_at"),
        )
        self._record_proxy_request_terminal(
            request_state["route_alias"],
            request_state["mode"],
            outcome,
            request_state.get("started_at"),
        )

    def _forward_request_via_link(
        self,
        client_link,
        request_id,
        target_destination,
        request_path,
        payload_bytes,
        request_timeout_s,
        request_started_at,
    ):
        established_event = threading.Event()
        try:
            target_link = Link(
                target_destination,
                established_callback=lambda link: established_event.set(),
                closed_callback=lambda link: self._handle_target_link_closed(request_id, link),
            )
            if not established_event.wait(timeout=request_timeout_s):
                if hasattr(target_link, "close"):
                    target_link.close()
                self._record_proxy_request_phase("proxy_node_service", "request", "target_link_setup", "timeout", request_started_at)
                self._record_proxy_request_terminal("proxy_node_service", "request", "timeout", request_started_at)
                self._send_proxy_response(client_link, request_id, error="timeout_establishing_target_link")
                return

            self._record_proxy_request_phase("proxy_node_service", "request", "target_link_setup", "success", request_started_at)
            target_request_started_at = time.monotonic()

            with self.lock:
                self.pending_client_requests[request_id] = client_link
                self.pending_target_links[request_id] = target_link
                self.pending_request_started_at[request_id] = request_started_at
                self.pending_target_request_started_at[request_id] = target_request_started_at

            receipt = target_link.request(
                request_path,
                data=payload_bytes,
                response_callback=lambda request_receipt: self._handle_target_request_response(request_id, request_receipt),
                failed_callback=lambda request_receipt: self._handle_target_request_failure(request_id, request_receipt),
                timeout=request_timeout_s,
            )
            if receipt is False:
                _, _, pending_started_at, pending_target_request_started_at = self._clear_pending_request(request_id)
                if hasattr(target_link, "close") and target_link.is_active():
                    target_link.close()
                self._record_proxy_request_phase(
                    "proxy_node_service",
                    "request",
                    "target_request_service",
                    "failed",
                    pending_target_request_started_at,
                )
                self._record_proxy_request_terminal(
                    "proxy_node_service",
                    "request",
                    "failed",
                    pending_started_at if pending_started_at is not None else request_started_at,
                )
                self._send_proxy_response(client_link, request_id, error="target_request_send_failed")
        except Exception as exc:
            self.logger.error("Error forwarding proxy request %s via target link: %s", request_id, exc, exc_info=True)
            _, target_link, pending_started_at, pending_target_request_started_at = self._clear_pending_request(request_id)
            if target_link and hasattr(target_link, "is_active") and target_link.is_active():
                target_link.close()
            if pending_target_request_started_at is not None:
                self._record_proxy_request_phase(
                    "proxy_node_service",
                    "request",
                    "target_request_service",
                    "failed",
                    pending_target_request_started_at,
                )
            else:
                self._record_proxy_request_phase("proxy_node_service", "request", "target_link_setup", "failed", request_started_at)
            self._record_proxy_request_terminal(
                "proxy_node_service",
                "request",
                "failed",
                pending_started_at if pending_started_at is not None else request_started_at,
            )
            self._send_proxy_response(client_link, request_id, error=f"target_request_error: {exc}")

    def _handle_target_request_response(self, request_id, request_receipt):
        client_link, target_link, started_at, target_request_started_at = self._clear_pending_request(request_id)
        response_payload = request_receipt.get_response() if hasattr(request_receipt, "get_response") else None
        try:
            if response_payload is None:
                self._record_proxy_request_phase("proxy_node_service", "request", "target_request_service", "failed", target_request_started_at)
                self._record_proxy_request_terminal("proxy_node_service", "request", "failed", started_at)
                self._send_proxy_response(client_link, request_id, error="target_response_missing")
                return
            if len(response_payload) > self.max_payload_size_bytes:
                self._record_proxy_request_phase("proxy_node_service", "request", "target_request_service", "failed", target_request_started_at)
                self._record_proxy_request_terminal("proxy_node_service", "request", "failed", started_at)
                self._send_proxy_response(client_link, request_id, error="response_payload_too_large")
                return
            self._send_proxy_response(client_link, request_id, payload=response_payload)
            if self.metrics_monitor:
                self.metrics_monitor.increment_proxied_packets("proxy_node_service", direction="response_to_client")
            self._record_proxy_request_phase("proxy_node_service", "request", "target_request_service", "success", target_request_started_at)
            self._record_proxy_request_terminal("proxy_node_service", "request", "success", started_at)
        finally:
            if target_link and hasattr(target_link, "is_active") and target_link.is_active():
                target_link.close()

    def _handle_target_request_failure(self, request_id, request_receipt):
        client_link, target_link, started_at, target_request_started_at = self._clear_pending_request(request_id)
        status = request_receipt.get_status() if hasattr(request_receipt, "get_status") else "unknown"
        self._record_proxy_request_phase("proxy_node_service", "request", "target_request_service", "failed", target_request_started_at)
        self._record_proxy_request_terminal("proxy_node_service", "request", "failed", started_at)
        self._send_proxy_response(client_link, request_id, error=f"target_request_failed:{status}")
        if target_link and hasattr(target_link, "is_active") and target_link.is_active():
            target_link.close()

    def _handle_target_link_closed(self, request_id, target_link):
        client_link, pending_target_link, started_at, target_request_started_at = self._clear_pending_request(request_id)
        if client_link is not None:
            self._record_proxy_request_phase("proxy_node_service", "request", "target_request_service", "closed", target_request_started_at)
            self._record_proxy_request_terminal("proxy_node_service", "request", "closed", started_at)
            self._send_proxy_response(client_link, request_id, error="target_link_closed_before_response")
        if pending_target_link and pending_target_link is not target_link and hasattr(pending_target_link, "is_active") and pending_target_link.is_active():
            pending_target_link.close()

    def _handle_proxy_link_closed(self, link, request_id, response_callback, request_state):
        self.logger.info(
            "Link to proxy server %s closed.",
            self._destination_preview(getattr(link, "destination", None)),
        )
        if response_callback is not None and not request_state["completed"]:
            self._complete_request_state(request_state, "closed")
            self._invoke_response_callback(response_callback, None, f"proxy link closed before response for request {request_id}")

    def _fail_pending_proxy_request(self, response_callback, request_state, error):
        if response_callback is None or request_state["completed"]:
            return
        self._complete_request_state(request_state, self._classify_request_outcome(error))
        self._invoke_response_callback(response_callback, None, error)

    def _clear_pending_request(self, request_id):
        with self.lock:
            client_link = self.pending_client_requests.pop(request_id, None)
            target_link = self.pending_target_links.pop(request_id, None)
            started_at = self.pending_request_started_at.pop(request_id, None)
            target_request_started_at = self.pending_target_request_started_at.pop(request_id, None)
        return client_link, target_link, started_at, target_request_started_at

    @staticmethod
    def _coerce_request_timeout(request_timeout_s):
        if request_timeout_s is None:
            return 30.0, None
        try:
            timeout_value = float(request_timeout_s)
        except (TypeError, ValueError):
            return None, "invalid_request_timeout"
        if timeout_value <= 0:
            return None, "invalid_request_timeout"
        return timeout_value, None

    @staticmethod
    def _is_valid_request_path(request_path):
        return isinstance(request_path, str) and request_path.strip() != ""

    def _select_proxy_route(self, proxy_alias, target_destination_name):
        if proxy_alias is not None:
            route = next((route for route in self.proxy_routes if route["alias"] == proxy_alias), None)
            if route is None:
                return None, f"proxy route '{proxy_alias}' not found"
            allowed, error, denial_reason = self._route_allows_target(route, target_destination_name)
            if not allowed:
                self._record_proxy_policy_denial(route["alias"], denial_reason)
                return None, error
            return route, None

        matching_routes = []
        rejected_routes = []
        for route in self.proxy_routes:
            allowed, error, denial_reason = self._route_allows_target(route, target_destination_name)
            if allowed:
                matching_routes.append(route)
            else:
                rejected_routes.append((route, error, denial_reason))

        if not matching_routes:
            if not self.proxy_routes:
                return None, "proxy route 'default' not found"
            rejected_routes.sort(key=lambda entry: self._route_specificity(entry[0]), reverse=True)
            rejected_route, error, denial_reason = rejected_routes[0]
            self._record_proxy_policy_denial(rejected_route["alias"], denial_reason)
            return None, error

        matching_routes.sort(key=self._route_specificity, reverse=True)
        return matching_routes[0], None

    def _route_allows_target(self, route, target_destination_name):
        if route.get("allow_all_targets", False):
            return True, None, None

        target_parts = self._destination_name_parts(target_destination_name)
        target_prefix = route.get("target_network_prefix")
        remaining_parts = target_parts[1:]

        if target_prefix:
            prefix_parts = self._destination_name_parts(target_prefix)
            if target_parts[: len(prefix_parts)] != prefix_parts:
                return (
                    False,
                    f"target_destination_name '{target_destination_name}' is outside proxy route '{route['alias']}' prefix '{target_prefix}'",
                    "prefix",
                )
            remaining_parts = target_parts[len(prefix_parts) :]

        allowed_target_aspects = route.get("allowed_target_aspects") or []
        if allowed_target_aspects:
            if not remaining_parts or remaining_parts[0] not in allowed_target_aspects:
                return (
                    False,
                    f"target_destination_name '{target_destination_name}' is not allowed by proxy route '{route['alias']}' aspects {allowed_target_aspects}",
                    "aspect",
                )

        return True, None, None

    @staticmethod
    def _route_specificity(route):
        prefix = route.get("target_network_prefix")
        prefix_length = len(prefix.split(".")) if prefix else 0
        return (
            prefix_length,
            0 if route.get("allow_all_targets", False) else 1,
            len(route.get("allowed_target_aspects") or []),
        )

    @staticmethod
    def _destination_name_parts(destination_name):
        return destination_name.split(".")

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
