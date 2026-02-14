import json, os, base64, re, threading
from akita_ares.core.logger import get_logger
try:
    import RNS; from RNS import Identity, Destination, Packet, Link; RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False
    class Identity:
        pass
    class Destination:
        IN, OUT, SINGLE, GROUP, PLAIN = 0, 1, 2, 3, 4
        @staticmethod
        def ummutable(h, type=None, direction=None):
            return None
    class Packet:
        def __init__(self, dest, data, id=None):
            pass
        def send(self):
            pass
        def set_response_callback(self, cb):
            pass
    class Link:
        def __init__(self, dest, id=None):
            self.link_id = os.urandom(16)
            self.destination = dest
        def set_resource_callback(self, cb):
            pass
        def set_link_closed_callback(self, cb):
            pass
        def set_established_callback(self, cb):
            pass
        def send(self, d):
            pass
        def close(self):
            pass
        def is_active(self):
            return False
PROXY_PROTOCOL_VERSION_1_0 = "1.0"; RNS_HASH_REGEX = re.compile(r'^[a-f0-9]{32}$')
class ProxyManager:
    def __init__(self, config, rns_instance=None, metrics_monitor=None):
        self.logger = get_logger("Feature.ProxyManager"); self.rns_instance = rns_instance; self.metrics_monitor = metrics_monitor
        self.is_proxy_node = False; self.proxy_routes_config = []; self.proxy_routes = [] 
        self.service_destination = None; self.active_client_links = {}; self.pending_client_requests = {}
        self.proxy_protocol_version = PROXY_PROTOCOL_VERSION_1_0; self.lock = threading.Lock() 
        if not RNS_AVAILABLE: self.logger.error("RNS library not found. ProxyManager cannot function.")
        elif not self.rns_instance: self.logger.error("RNS instance not provided. ProxyManager cannot function.")
        self.update_config(config)
    def update_config(self, config):
        with self.lock: 
            self.config = config; new_is_proxy_node = self.config.get('is_proxy_node', False)
            self.proxy_routes_config = self.config.get('proxy_routes', [])
            self.proxy_protocol_version = self.config.get('proxy_protocol_version', PROXY_PROTOCOL_VERSION_1_0)
            self.logger.info(f"ProxyMan cfg update. IsProxyNode:{new_is_proxy_node}, Proto:{self.proxy_protocol_version}")
            role_changed = (new_is_proxy_node != self.is_proxy_node); self.is_proxy_node = new_is_proxy_node 
            if role_changed:
                if self.is_proxy_node: self._shutdown_client_proxy_resources(); self._setup_proxy_service_destination()
                else: self._shutdown_proxy_service_destination()
            else: 
                if self.is_proxy_node:
                    listen_aspect = self.config.get('listen_on_aspect','def_proxy_svc'); cur_aspect = getattr(self,"cur_listen_aspect",None)
                    if self.service_destination and listen_aspect != cur_aspect: self.logger.info("Proxy listen aspect changed. Re-init service dest."); self._shutdown_proxy_service_destination(); self._setup_proxy_service_destination()
                else: self._configure_routes() 
            if self.metrics_monitor: self.metrics_monitor.set_active_proxy_routes_count(len(self.proxy_routes))
    def _configure_routes(self): # Client-side
        new_routes = []
        for route_cfg in self.proxy_routes_config:
            alias = route_cfg.get('alias'); entry_name = route_cfg.get('entry_destination_name'); exit_hash = route_cfg.get('exit_node_identity_hash')
            if alias and entry_name and exit_hash:
                if RNS_HASH_REGEX.match(exit_hash): new_routes.append({"alias": alias, "entry_destination_name_str": entry_name, "exit_node_identity_hash_hex": exit_hash})
                else: self.logger.warning(f"Skipping invalid proxy route '{alias}': exit_node_identity_hash '{exit_hash}' invalid format.")
            else: self.logger.warning(f"Skipping invalid proxy route config: {route_cfg}")
        self.proxy_routes = new_routes; self.logger.info(f"Client proxy routes configured: {len(self.proxy_routes)} valid routes.")
    def _setup_proxy_service_destination(self): # Server-side
        if not RNS_AVAILABLE or not self.rns_instance: self.logger.error("RNS NA for proxy service."); return
        if self.service_destination: self.logger.info("Proxy service dest already exists."); return
        self.current_listen_aspect = self.config.get('listen_on_aspect', 'default_proxy_service'); dest_name_parts = ["ares", "proxy", self.current_listen_aspect]
        try:
            self.service_destination = Destination(self.rns_instance.identity, Destination.IN, Destination.SINGLE, *dest_name_parts)
            self.service_destination.set_link_established_callback(self._handle_client_link_established)
            self.logger.info(f"Proxy node listening on RNS Dest: {'.'.join(dest_name_parts)} ({self.service_destination.hash_hex()})")
        except Exception as e: self.logger.error(f"Failed to create proxy service destination: {e}", exc_info=True); self.service_destination = None
    def _handle_client_link_established(self, link: Link): # Server-side
        if not RNS_AVAILABLE: return; link_id = link.link_id.hex() 
        with self.lock: self.active_client_links[link_id] = link
        self.logger.info(f"New client link established to proxy service: {link_id}")
        link.set_resource_callback(lambda resource: self._handle_proxied_request_on_link(resource, link)); link.set_link_closed_callback(lambda closed_link: self._handle_client_link_closed(closed_link))
        if self.metrics_monitor: self.metrics_monitor.set_active_proxy_clients_count(len(self.active_client_links))
    def _handle_client_link_closed(self, link: Link): # Server-side
        if not RNS_AVAILABLE: return; link_id = link.link_id.hex(); closed_reqs = 0
        with self.lock:
            if link_id in self.active_client_links: del self.active_client_links[link_id]; self.logger.info(f"Client link closed: {link_id}")
            for req_id, req_info_link in list(self.pending_client_requests.items()):
                if req_info_link.link_id == link.link_id: del self.pending_client_requests[req_id]; closed_reqs += 1
        if closed_reqs > 0: self.logger.debug(f"Removed {closed_reqs} pending requests for closed link {link_id}.")
        if self.metrics_monitor: self.metrics_monitor.set_active_proxy_clients_count(len(self.active_client_links))
    def _handle_proxied_request_on_link(self, resource, client_link: Link): # Server-side
        if not RNS_AVAILABLE: return; client_link_id_hex = client_link.link_id.hex(); self.logger.debug(f"Proxy node received data from client link {client_link_id_hex} (size {len(resource.data)} bytes).")
        try:
            message = json.loads(resource.data.decode('utf-8'))
            if message.get("version") != self.proxy_protocol_version: self.logger.warning(f"Incompatible proto ver from {client_link_id_hex}. Got {message.get('version')}"); client_link.send(json.dumps({"error": "incompatible_protocol_version"}).encode('utf-8')) if client_link.is_active() else None; return
            target_hash_hex = message.get("target_destination_hash"); payload_b64 = message.get("payload"); client_request_id = message.get("request_id")
            if not target_hash_hex or not payload_b64 or not client_request_id: self.logger.error(f"Invalid proxy msg from {client_link_id_hex}: missing fields."); client_link.send(json.dumps({"request_id": client_request_id, "error": "invalid_request_format"}).encode('utf-8')) if client_link.is_active() else None; return
            if not RNS_HASH_REGEX.match(target_hash_hex): self.logger.error(f"Invalid target_hash format from {client_link_id_hex}: {target_hash_hex}"); client_link.send(json.dumps({"request_id": client_request_id, "error": "invalid_target_hash_format"}).encode('utf-8')) if client_link.is_active() else None; return
            actual_payload_bytes = base64.b64decode(payload_b64); target_destination_hash_bytes = bytes.fromhex(target_hash_hex)
        except Exception as e: self.logger.error(f"Error decoding/parsing proxy request from {client_link_id_hex}: {e}"); error_msg = {"request_id": client_request_id or "unknown", "error": f"request_decode_error: {e}"}; client_link.send(json.dumps(error_msg).encode('utf-8')) if client_link.is_active() else None; return
        self.logger.info(f"Proxying request (ID: {client_request_id}) from client link {client_link_id_hex} to target {target_hash_hex[:8]}...")
        with self.lock: self.pending_client_requests[client_request_id] = client_link 
        try:
            target_destination = Destination.ummutable(target_destination_hash_bytes, type=Destination.SINGLE, direction=Destination.OUT)
            packet_to_target = Packet(target_destination, actual_payload_bytes, self.rns_instance.identity) 
            packet_to_target.set_response_callback(lambda resp_pkt: self._handle_response_from_target(resp_pkt, client_request_id))
            packet_to_target.send()
            self.logger.debug(f"Packet sent from proxy to target {target_hash_hex[:8]} for request {client_request_id}")
            if self.metrics_monitor: self.metrics_monitor.increment_proxied_packets("proxy_node_service", direction='sent_to_target')
        except Exception as e:
            self.logger.error(f"Error sending proxied packet to target {target_hash_hex[:8]}: {e}", exc_info=True)
            with self.lock: original_client_link = self.pending_client_requests.pop(client_request_id, None)
            if original_client_link and original_client_link.is_active():
                error_response = {"version": self.proxy_protocol_version, "type": "response", "request_id": client_request_id, "error": f"Proxy failed to send to target: {e}"}
                try: original_client_link.send(json.dumps(error_response).encode('utf-8'))
                except Exception as send_e: self.logger.error(f"Failed to send error back to client {client_link_id_hex}: {send_e}")
    def _handle_response_from_target(self, response_packet: Packet, client_request_id: str): # Server-side
        if not RNS_AVAILABLE: return; self.logger.debug(f"Proxy node received response from target for client_request_id {client_request_id}")
        with self.lock: original_client_link = self.pending_client_requests.pop(client_request_id, None)
        if not original_client_link: self.logger.warning(f"Original client link for request_id {client_request_id} not found. Cannot forward response."); return
        if not original_client_link.is_active(): self.logger.warning(f"Original client link {original_client_link.link_id.hex()} for request_id {client_request_id} inactive. Cannot forward."); return
        try:
            payload_b64 = base64.b64encode(response_packet.data).decode('utf-8')
            proxy_response_msg = {"version": self.proxy_protocol_version, "type": "response", "request_id": client_request_id, "source_destination_hash": response_packet.source_hash.hex() if response_packet.source_hash else None, "payload": payload_b64}
            response_bytes = json.dumps(proxy_response_msg).encode('utf-8')
            original_client_link.send(response_bytes)
            self.logger.info(f"Forwarded response for request {client_request_id} to client link {original_client_link.link_id.hex()}")
            if self.metrics_monitor: self.metrics_monitor.increment_proxied_packets("proxy_node_service", direction='response_to_client')
        except Exception as e:
            self.logger.error(f"Error encoding/forwarding response to client for request_id {client_request_id}: {e}", exc_info=True)
            if original_client_link.is_active():
                 try: error_resp = {"version": self.proxy_protocol_version, "type":"response", "request_id": client_request_id, "error": f"Proxy failed to process target response: {e}"}; original_client_link.send(json.dumps(error_resp).encode('utf-8'))
                 except Exception as send_e: self.logger.error(f"Failed to send error back to client {original_client_link.link_id.hex()} after response processing error: {send_e}")
    def send_via_proxy(self, target_dest_hash, data_to_send, proxy_alias=None, response_callback=None, timeout_s=30): # Client-side
        if not RNS_AVAILABLE or not self.rns_instance: self.logger.error("RNS NA for proxy send."); return None
        route=next((r for r in self.proxy_routes if r['alias']==proxy_alias),self.proxy_routes[0] if self.proxy_routes else None)
        if not route: self.logger.error(f"Proxy route '{proxy_alias or 'default'}' not found."); return None
        if not RNS_HASH_REGEX.match(target_dest_hash): self.logger.error(f"Invalid target_destination_hash format: {target_dest_hash}"); return None
        self.logger.info(f"Client sending to {target_dest_hash[:8]} via proxy '{route['alias']}' (entry: {route['entry_destination_name_str']})")
        try:
            proxy_server_identity = Identity.recall(bytes.fromhex(route['exit_node_identity_hash_hex']))
            if not proxy_server_identity: self.logger.warning(f"Proxy server identity {route['exit_node_identity_hash_hex'][:8]}... not cached. Requesting..."); proxy_server_identity = Identity.request(bytes.fromhex(route['exit_node_identity_hash_hex']), timeout=timeout_s/2);
            if not proxy_server_identity: raise ValueError(f"Proxy server identity {route['exit_node_identity_hash_hex'][:8]}... unavailable.")
            proxy_entry_dest = Destination(proxy_server_identity, Destination.OUT, Destination.SINGLE, *route['entry_destination_name_str'].split('.'))
        except ValueError as e: self.logger.error(f"Invalid Identity hash for proxy '{route['alias']}': {route['exit_node_identity_hash_hex']}. Error: {e}"); return None
        except Exception as e: self.logger.error(f"Failed to create RNS Dest for proxy entry '{route['entry_destination_name_str']}': {e}", exc_info=True); return None
        request_id = os.urandom(8).hex()
        try: payload_b64 = base64.b64encode(data_to_send).decode('utf-8') 
        except Exception as e: self.logger.error(f"Failed to base64 encode data for proxy request {request_id}: {e}"); return None
        proxy_req_data = {"version": self.proxy_protocol_version, "type": "request" if response_callback else "data_oneway", "request_id": request_id, "target_destination_hash": target_dest_hash, "payload": payload_b64}
        try: proxy_req_bytes = json.dumps(proxy_req_data).encode('utf-8')
        except Exception as e: self.logger.error(f"Failed to JSON encode proxy request {request_id}: {e}"); return None
        try:
            link_to_proxy = Link(proxy_entry_dest, self.rns_instance.identity) 
            established_event = threading.Event()
            link_to_proxy.set_established_callback(lambda l: established_event.set())
            link_to_proxy.set_link_closed_callback(lambda l: self.logger.info(f"Link to proxy server {l.destination.hash_hex()[:8]} closed."))
            if response_callback: link_to_proxy.set_resource_callback(lambda res: self._handle_proxy_response_on_client(res, response_callback, request_id))
            self.logger.debug(f"Attempting to establish link to proxy {proxy_entry_dest.hash_hex()[:8]}...")
            if not established_event.wait(timeout=timeout_s): self.logger.error(f"Timeout establishing link to proxy server {proxy_entry_dest.hash_hex()[:8]}."); link_to_proxy.close(); return None
            self.logger.debug(f"Link to proxy {proxy_entry_dest.hash_hex()[:8]} established. Sending request {request_id}...")
            link_to_proxy.send(proxy_req_bytes)
            if self.metrics_monitor: self.metrics_monitor.increment_proxied_packets(route['alias'], direction='sent_to_proxy')
            if not response_callback: link_to_proxy.close(); self.logger.debug(f"One-way data sent to proxy {proxy_entry_dest.hash_hex()[:8]} for request {request_id}. Link closed.")
            return request_id # Success
        except Exception as e: self.logger.error(f"Error in send_via_proxy for '{route['alias']}': {e}", exc_info=True); return None
    def _handle_proxy_response_on_client(self, resource, original_response_callback, original_request_id): # Client-side
        if not RNS_AVAILABLE: return; link_id_hex = resource.link.link_id.hex(); self.logger.debug(f"Client received resource from proxy link {link_id_hex}. Size: {len(resource.data)}")
        try:
            proxy_response = json.loads(resource.data.decode('utf-8')); received_request_id = proxy_response.get("request_id")
            if received_request_id != original_request_id: self.logger.warning(f"Received proxy response with mismatched request_id ({received_request_id} != {original_request_id}). Ignoring."); return
            if "error" in proxy_response: error_msg = proxy_response['error']; self.logger.error(f"Proxy returned error for request_id {original_request_id}: {error_msg}"); original_response_callback(None, error_msg) if original_response_callback else None
            elif "payload" in proxy_response:
                try: actual_response_data = base64.b64decode(proxy_response["payload"]); self.logger.debug(f"Received response payload for request {original_request_id} (size: {len(actual_response_data)} bytes)."); original_response_callback(actual_response_data, None) if original_response_callback else None
                except Exception as decode_err: self.logger.error(f"Error decoding payload from proxy response for {original_request_id}: {decode_err}"); original_response_callback(None, f"Proxy response payload decode error: {decode_err}") if original_response_callback else None
            else: self.logger.warning(f"Received proxy response for {original_request_id} with no payload or error."); original_response_callback(None, "Empty proxy response") if original_response_callback else None
        except (json.JSONDecodeError, UnicodeDecodeError) as e: self.logger.error(f"Error decoding/parsing proxy response JSON: {e}"); original_response_callback(None, f"Proxy response JSON decode error: {e}") if original_response_callback else None
        except Exception as e: self.logger.error(f"Unexpected error processing proxy response: {e}", exc_info=True); original_response_callback(None, f"Unexpected error: {e}") if original_response_callback else None
        finally:
            if resource.link and resource.link.is_active(): self.logger.debug(f"Closing link to proxy {link_id_hex} after receiving response for {original_request_id}."); resource.link.close()
    def periodic_check(self):
        with self.lock: 
            if self.is_proxy_node: count=len(self.active_client_links); self.logger.debug(f"ProxyMan (node) check. Active links:{count}"); self.metrics_monitor.set_active_proxy_clients_count(count) if self.metrics_monitor else None
            else: self.logger.debug(f"ProxyMan (client) check. Config routes:{len(self.proxy_routes)}")
    def _shutdown_client_proxy_resources(self): self.logger.info("Shutting down client proxy resources."); self.proxy_routes=[]
    def _shutdown_proxy_service_destination(self):  # Server-side cleanup
        if not RNS_AVAILABLE:
            return
        with self.lock:
            if self.service_destination:
                self.logger.info(f"Closing proxy service destination {self.service_destination.hash_hex()}...")
                try:
                    self.service_destination.close()
                except Exception as e:
                    self.logger.error(f"Error closing proxy service destination: {e}")
                self.service_destination = None
            for link_id, link in list(self.active_client_links.items()):
                self.logger.debug(f"Closing active client link {link_id} during shutdown.")
                try:
                    if link.is_active():
                        link.close()
                except Exception as e:
                    self.logger.error(f"Error closing client link {link_id}: {e}")
            self.active_client_links.clear()
            self.pending_client_requests.clear()
            if self.metrics_monitor:
                self.metrics_monitor.set_active_proxy_clients_count(0)
    def shutdown(self):
        self.logger.info("ProxyManager shutting down...")
        if self.is_proxy_node:
            self._shutdown_proxy_service_destination()
        else:
            self._shutdown_client_proxy_resources()
