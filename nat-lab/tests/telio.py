from utils.connection import Connection, TargetOS
from utils.process import Process
from utils import asyncio_util
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses_json import dataclass_json, DataClassJsonMixin
from enum import Enum
from mesh_api import Node
from typing import List, Dict, Any, Set, Optional, AsyncIterator
from utils import Router, new_router, OutputNotifier, connection_util
from config import DERP_PRIMARY, DERP_SERVERS
import asyncio
import json
import os
import re
import shlex
import utils.testing as testing
from collections import Counter
from telio_features import TelioFeatures


# Equivalent of `libtelio/telio-wg/src/uapi.rs`
class State(Enum):
    Disconnected = "disconnected"
    Connecting = "connecting"
    Connected = "connected"


# Equivalent of `libtelio/crates/telio-model/src/api_config.rs:PathType`
class PathType(Enum):
    Relay = "relay"
    Direct = "direct"

    # Any should always be last, because it is only introduced in nat-lab, not in libtelio itself
    Any = "any"


@dataclass_json
@dataclass
class DerpServer(DataClassJsonMixin):
    region_code: str
    name: str
    hostname: str
    ipv4: str
    relay_port: int
    stun_port: int
    stun_plaintext_port: int
    public_key: str
    weight: int
    use_plain_text: bool
    conn_state: State


# Equivalent of `libtelio/crates/telio-model/src/mesh.rs:Node`
@dataclass_json
@dataclass
class PeerInfo(DataClassJsonMixin):
    identifier: str
    public_key: str
    state: State
    is_exit: bool
    is_vpn: bool
    ip_addresses: List[str]
    allowed_ips: List[str]
    endpoint: Optional[str]
    hostname: Optional[str]
    allow_incoming_connections: bool
    allow_peer_send_files: bool
    path: PathType

    def __init__(
        self,
        identifier="",
        public_key="",
        state=State.Disconnected,
        is_exit=False,
        is_vpn=False,
        ip_addresses=[],
        allowed_ips=[],
        endpoint=None,
        hostname=None,
        allow_incoming_connections=False,
        allow_peer_send_files=False,
        path=PathType.Relay,
    ):
        self.identifier = identifier
        self.public_key = public_key
        self.state = state
        self.is_exit = is_exit
        self.is_vpn = is_vpn
        self.ip_addresses = ip_addresses
        self.allowed_ips = allowed_ips
        self.endpoint = endpoint
        self.hostname = hostname
        self.allow_incoming_connections = allow_incoming_connections
        self.allow_peer_send_files = allow_peer_send_files
        self.path = path

    def __hash__(self):
        return hash(
            (
                self.identifier,
                self.public_key,
                self.state,
                self.is_exit,
                self.is_vpn,
                self.ip_addresses,
                self.allowed_ips,
                self.endpoint,
                self.hostname,
                self.allow_incoming_connections,
                self.allow_peer_send_files,
                self.path,
            )
        )

    def __eq__(self, other):
        if not isinstance(other, PeerInfo):
            return False
        return (
            self.identifier == other.identifier
            and self.public_key == other.public_key
            and self.state == other.state
            and self.is_exit == other.is_exit
            and self.is_vpn == other.is_vpn
            and self.ip_addresses == other.ip_addresses
            and self.allowed_ips == other.allowed_ips
            and (
                self.endpoint is None
                or other.endpoint is None
                or self.endpoint == other.endpoint
            )
            and (
                self.hostname is None
                or other.hostname is None
                or self.hostname == other.hostname
            )
            and self.allow_incoming_connections == other.allow_incoming_connections
            and self.allow_peer_send_files == other.allow_peer_send_files
            and self.path == other.path
        )


# Equivalent of `libtelio/telio-wg/src/adapter/mod.rs`
class AdapterType(Enum):
    Default = ""
    BoringTun = "boringtun"
    LinuxNativeWg = "linux-native"
    WireguardGo = "wireguard-go"
    WindowsNativeWg = "wireguard-nt"


class Runtime:
    _output_notifier: OutputNotifier
    _peer_info: Dict[str, PeerInfo]
    _peer_state_events: Dict[str, List[asyncio.Event]]
    _derp_state_events: Dict[str, List[asyncio.Event]]
    _derp_state: Dict[str, DerpServer]
    _started_tasks: List[str]
    _stopped_tasks: List[str]
    allowed_pub_keys: Set[str]

    def __init__(self) -> None:
        self._output_notifier = OutputNotifier()
        self._peer_info = {}
        self._peer_state_events = {}
        self._derp_state_events = {}
        self._derp_state = {}
        self._started_tasks = []
        self._stopped_tasks = []
        self.allowed_pub_keys = set()

    def handle_output_line(self, line) -> bool:
        return (
            self._handle_node_event(line)
            or self._output_notifier.handle_output(line)
            or self._handle_derp_event(line)
            or self._handle_task_information(line)
        )

    def _handle_task_information(self, line) -> bool:
        if line.startswith("task started - "):
            tokens = line.split("task started - ")
            self._started_tasks.append(tokens[1].strip())
            return True

        if line.startswith("task stopped - "):
            tokens = line.split("task stopped - ")
            self._stopped_tasks.append(tokens[1].strip())
            return True

        return False

    def get_output_notifier(self) -> OutputNotifier:
        return self._output_notifier

    def notify_peer_state(self, public_key: str, event: asyncio.Event) -> None:
        if public_key not in self._peer_state_events:
            self._peer_state_events[public_key] = [event]
        else:
            self._peer_state_events[public_key].append(event)

    def get_peer_info(self, public_key: str) -> Optional[PeerInfo]:
        return self._peer_info.get(public_key)

    def notify_derp_state(self, server_ip: str, event: asyncio.Event) -> None:
        if server_ip not in self._derp_state_events:
            self._derp_state_events[server_ip] = [event]
        else:
            self._derp_state_events[server_ip].append(event)

    def get_derp_info(self, server_ip: str) -> Optional[DerpServer]:
        return self._derp_state.get(server_ip)

    def _set_peer(self, peer: PeerInfo) -> None:
        self._peer_info[peer.public_key] = peer
        assert peer.public_key in self.allowed_pub_keys
        events = self._peer_state_events.pop(peer.public_key, None)
        if events:
            for event in events:
                event.set()

    def _handle_node_event(self, line) -> bool:
        if not line.startswith("event node: "):
            return False
        tokens = line.split("event node: ")
        json_string = tokens[1].strip()
        result = re.search("{(.*)}", json_string)
        if result:
            node_state = PeerInfo.schema().loads(
                "{" + result.group(1).replace("\\", "") + "}"
            )
            assert isinstance(node_state, PeerInfo)
            self._set_peer(node_state)
            return True
        return False

    def _set_derp(self, derp: DerpServer) -> None:
        self._derp_state[derp.ipv4] = derp
        events = self._derp_state_events.pop(derp.ipv4, None)
        if events:
            for event in events:
                event.set()

    def _handle_derp_event(self, line) -> bool:
        if not line.startswith("event relay: "):
            return False
        tokens = line.split("event relay: ")
        json_string = tokens[1].strip()
        result = re.search("{(.*)}", json_string)
        if result:
            derp_server_json = DerpServer.schema().loads(
                "{" + result.group(1).replace("\\", "") + "}"
            )
            assert isinstance(derp_server_json, DerpServer)
            self._set_derp(derp_server_json)
            return True
        return False

    def get_started_tasks(self) -> List[str]:
        return self._started_tasks

    def get_stopped_tasks(self) -> List[str]:
        return self._stopped_tasks


class Events:
    _runtime: Runtime

    def __init__(
        self,
        runtime: Runtime,
    ) -> None:
        self._runtime = runtime

    async def message_done(self, message_idx: int) -> None:
        event = asyncio.Event()
        self._runtime.get_output_notifier().notify_output(
            f"MESSAGE_DONE={message_idx}", event
        )
        await event.wait()

    async def wait_for_state(
        self,
        public_key: str,
        state: State,
        path: PathType = PathType.Relay,
    ) -> None:
        peer_info = self._runtime.get_peer_info(public_key)
        if (
            peer_info
            and peer_info.state == state
            and (peer_info.path == path or path == PathType.Any)
        ):
            return

        while True:
            event = asyncio.Event()
            self._runtime.notify_peer_state(public_key, event)
            await event.wait()
            peer_info = self._runtime.get_peer_info(public_key)
            if (
                peer_info
                and peer_info.state == state
                and (peer_info.path == path or path == PathType.Any)
            ):
                return

    async def wait_for_derp_state(
        self,
        server_ip: str,
        state: list[State],
    ) -> None:
        derp_info = self._runtime.get_derp_info(server_ip)
        if derp_info and derp_info.conn_state in state:
            return

        while True:
            event = asyncio.Event()
            self._runtime.notify_derp_state(server_ip, event)
            await event.wait()
            derp_info = self._runtime.get_derp_info(server_ip)
            if derp_info and derp_info.conn_state in state:
                return


class Client:
    def __init__(
        self,
        connection: Connection,
        node: Node,
        adapter_type: AdapterType = AdapterType.Default,
        telio_features: TelioFeatures = TelioFeatures(),
    ) -> None:
        self._router: Optional[Router] = None
        self._events: Optional[Events] = None
        self._runtime: Optional[Runtime] = None
        self._process: Optional[Process] = None
        self._interface_configured = False
        self._message_idx = 0
        self._node = node
        self._connection = connection
        self._adapter_type = adapter_type
        self._telio_features = telio_features
        self._quit = False

    @asynccontextmanager
    async def run(self) -> AsyncIterator["Client"]:
        async def on_stdout(stdout: str) -> None:
            supress_print_list = [
                "MESSAGE_DONE=",
                "- no login.",
                "- telio running.",
                "- telio nodes",
                "task stopped - ",
                "task started - ",
            ]
            for line in stdout.splitlines():
                if not any(string in line for string in supress_print_list):
                    print(f"[{self._node.name}]: {line}")
                if self._runtime:
                    self._runtime.handle_output_line(line)

        tcli_path = connection_util.get_libtelio_binary_path("tcli", self._connection)

        self._runtime = Runtime()
        self._events = Events(self._runtime)
        self._router = new_router(self._connection)
        self._process = self._connection.create_process(
            [tcli_path, "--less-spam", f"-f {self._telio_features.to_json()}"]  # type: ignore
        )

        async with self._process.run(stdout_callback=on_stdout):
            try:
                await self._process.wait_stdin_ready()
                await self._write_command(
                    [
                        "dev",
                        "start",
                        self._adapter_type.value,
                        self._router.get_interface_name(),
                        self._node.private_key,
                    ],
                )
                async with asyncio_util.run_async_context(self._event_request_loop()):
                    yield self
            finally:
                if not self._quit:
                    await testing.wait_normal(self.stop_device())
                if self._router:
                    await self._router.delete_vpn_route()
                    await self._router.delete_exit_node_route()
                    await self._router.delete_interface()
                await self.save_logs(self._connection)

    @asynccontextmanager
    async def run_meshnet(self, meshmap: Dict[str, Any]) -> AsyncIterator["Client"]:
        async with self.run():
            await self.set_meshmap(meshmap)
            yield self

    async def quit(self):
        self._quit = True
        await self._write_command(["quit"])

    async def simple_start(self):
        await self._write_command(
            [
                "dev",
                "start",
                self._adapter_type.value,
                self.get_router().get_interface_name(),
                self._node.private_key,
            ],
        )

    async def handshake(
        self,
        public_key,
        path=PathType.Relay,
    ) -> None:
        await self.get_events().wait_for_state(
            public_key,
            State.Connected,
            path,
        )

    async def disconnect(self, public_key, path=PathType.Relay) -> None:
        await self.get_events().wait_for_state(public_key, State.Disconnected, path)

    async def connecting(self, public_key, path=PathType.Relay) -> None:
        await self.get_events().wait_for_state(public_key, State.Connecting, path)

    async def wait_for_any_node_event(self, public_key) -> None:
        event = asyncio.Event()
        self.get_runtime().notify_peer_state(public_key, event)
        await event.wait()

    async def wait_for_any_derp_event(self) -> None:
        event = asyncio.Event()
        for derp in DERP_SERVERS:
            self.get_runtime().notify_derp_state(str(derp["ipv4"]), event)
        await event.wait()

    async def wait_for_derp_state(
        self,
        server_ip: str,
        state: list[State],
    ) -> None:
        await self.get_events().wait_for_derp_state(server_ip, state)

    async def wait_for_any_derp_state(self, state: list[State]) -> None:
        futures = []
        for derp in DERP_SERVERS:
            futures.append(
                self.get_events().wait_for_derp_state(
                    str(derp["ipv4"]),
                    state,
                )
            )
        _, pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
        for future in pending:
            await asyncio_util.cancel_future(future)

    async def set_meshmap(self, meshmap: Dict[str, Any]) -> None:
        made_changes = await self._configure_interface()

        # Linux native WG takes ~1.5s to setup listen port for WG interface. Since
        # listen port is required for mesh connection, there is no other way to
        # bypass this waiting period. Another option would be to retry `mesh config`
        # multiple times until the command succeeds.
        if made_changes and self._adapter_type == AdapterType.LinuxNativeWg:
            await asyncio.sleep(2)

        if "peers" in meshmap:
            peers = meshmap["peers"]
            for peer in peers:
                self.get_runtime().allowed_pub_keys.add(peer["public_key"])

        await self._write_command(["mesh", "config", json.dumps(meshmap)])

    async def set_mesh_off(self):
        await self._write_command(["mesh", "off"])

    async def receive_ping(self):
        await self._write_command(["mesh", "ping"])

    async def send_stun(self):
        await self._write_command(["mesh", "stun"])

    async def igd(self):
        await self._write_command(["mesh", "igd"])

    async def connect_to_vpn(self, ip, port, public_key) -> None:
        await self._configure_interface()
        await self.get_router().create_vpn_route()
        self.get_runtime().allowed_pub_keys.add(public_key)
        await self._write_command(
            [
                "dev",
                "con",
                public_key,
                "{}:{}".format(ip, port),
            ],
        )

    async def disconnect_from_vpn(self, public_key, path=PathType.Relay) -> None:
        await self._write_command(["vpn", "off"])
        await self.disconnect(public_key, path)
        await self.get_router().delete_vpn_route()

    async def disconnect_from_exit_nodes(self) -> None:
        await self._write_command(["vpn", "off"])
        await self.get_router().delete_vpn_route()

    async def enable_magic_dns(self, forward_servers: List[str]) -> None:
        await self._write_command(
            ["dns", "on"] + forward_servers,
        )

    async def disable_magic_dns(self) -> None:
        await self._write_command(
            ["dns", "off"],
        )

    async def notify_network_change(self) -> None:
        await self._write_command(
            ["dev", "notify-net-change"],
        )

    async def _configure_interface(self) -> bool:
        if not self._interface_configured:
            await self.get_router().setup_interface(
                self._node.ip_addresses[0],
            )

            await self.get_router().create_meshnet_route()
            self._interface_configured = True
            return True

        return False

    async def connect_to_exit_node(self, public_key: str) -> None:
        await self._configure_interface()
        await self.get_router().create_vpn_route()
        await self._write_command(["dev", "con", public_key])

    def get_router(self) -> Router:
        assert self._router
        return self._router

    def get_runtime(self) -> Runtime:
        assert self._runtime
        return self._runtime

    def get_process(self) -> Process:
        assert self._process
        return self._process

    def get_events(self) -> Events:
        assert self._events
        return self._events

    def get_stdout(self) -> str:
        assert self._process
        return self._process.get_stdout()

    async def stop_device(self) -> None:
        await self._write_command(["dev", "stop"])
        self._interface_configured = False
        assert Counter(self.get_runtime().get_started_tasks()) == Counter(
            self.get_runtime().get_stopped_tasks()
        ), f"started tasks and stopped tasks differ!"

    def get_node_state(self, public_key: str) -> Optional[PeerInfo]:
        return self.get_runtime().get_peer_info(public_key)

    def get_derp_state(self, server_ip: str) -> Optional[DerpServer]:
        return self.get_runtime().get_derp_info(server_ip)

    async def _event_request_loop(self) -> None:
        while True:
            try:
                await self._write_command(["events"])
                await asyncio.sleep(1)
            except:
                if self._quit:
                    return
                raise

    async def create_fake_derprelay_to_derp01(self, sk: str, allowed_pk: str) -> None:
        derp01_server = (
            '{"host":"'
            + str(DERP_PRIMARY["hostname"])
            + '","ipv4":"'
            + str(DERP_PRIMARY["ipv4"])
            + '","port":'
            + str(DERP_PRIMARY["relay_port"])
            + ',"pk":"'
            + str(DERP_PRIMARY["public_key"])
            + '"}'
        )

        await self._write_command(
            [
                "derp",
                "on",
                sk,
                derp01_server,
                allowed_pk,
            ]
        )

    async def disconnect_fake_derprelay(self) -> None:
        await self._write_command(["derp", "off"])

    async def recv_message_from_fake_derp_relay(self) -> None:
        await self._write_command(["derp", "recv"])

    async def fake_derp_events(self) -> None:
        await self._write_command(["derp", "events"])

    async def send_message_from_fake_derp_relay(self, pk: str, data: List[str]) -> None:
        await self._write_command(["derp", "send", pk] + data)

    async def trigger_event_collection(self) -> None:
        await self._write_command(["dev", "analytics"])

    async def _write_command(self, command: List[str]) -> None:
        idx = self._message_idx
        cmd = (
            f"MESSAGE_ID={str(idx)} "
            + " ".join([shlex.quote(arg) for arg in command])
            + "\n"
        )
        await self.get_process().write_stdin(cmd)
        self._message_idx += 1
        await self.get_events().message_done(idx)

    def get_endpoint_address(self, public_key: str) -> str:
        node = self.get_node_state(public_key)
        if node is None:
            raise Exception(f"Node {public_key} doesn't exist")
        if node.endpoint is None:
            raise Exception(f"Node {public_key} endpoint doesn't exist")
        return node.endpoint.split(":")[0]

    def wait_for_output(self, what: str) -> asyncio.Event:
        event = asyncio.Event()
        self.get_runtime().get_output_notifier().notify_output(what, event)
        return event

    @staticmethod
    async def save_logs(connection: Connection) -> None:
        if os.environ.get("NATLAB_SAVE_LOGS") is None:
            return

        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        process = (
            connection.create_process(["type", "tcli.log"])
            if connection.target_os == TargetOS.Windows
            else connection.create_process(["cat", "./tcli.log"])
        )
        await process.execute()
        log_content = process.get_stdout()

        if connection.target_os == TargetOS.Linux:
            process = connection.create_process(["cat", "/etc/hostname"])
            await process.execute()
            container_id = process.get_stdout().strip()
        else:
            container_id = str(connection.target_os)

        test_name = os.environ.get("PYTEST_CURRENT_TEST")
        if test_name is not None:
            test_name = "".join(
                [x if x.isalnum() else "_" for x in test_name.split(" ")[0]]
            )
        with open(
            os.path.join(log_dir, str(test_name) + "_" + container_id + ".log"), "w"
        ) as f:
            f.write(log_content)
