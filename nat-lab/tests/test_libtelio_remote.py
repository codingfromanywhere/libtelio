
import asyncio
import pytest
import subprocess
import telio
import sys
from contextlib import AsyncExitStack
from helpers import SetupParameters, setup_mesh_nodes, setup_environment
from telio import PathType, State
from telio_features import TelioFeatures, Direct, LinkDetection
from timeouts import TEST_MESH_STATE_AFTER_DISCONNECTING_NODE_TIMEOUT
from utils.connection import TargetOS
from utils.connection_util import ConnectionTag, container_id
from utils.ping import Ping
from Pyro5.errors import ConnectionClosedError # type: ignore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        ("15", ConnectionTag.DOCKER_CONE_CLIENT_1, telio.AdapterType.LinuxNativeWg), # SIGTERM
        ("2", ConnectionTag.DOCKER_CONE_CLIENT_1 , telio.AdapterType.LinuxNativeWg), # SIGINT
        ("1", ConnectionTag.DOCKER_CONE_CLIENT_1 , telio.AdapterType.LinuxNativeWg), # SIGHUP
        pytest.param(
            ("IGNORE", ConnectionTag.WINDOWS_VM_1, telio.AdapterType.WindowsNativeWg), # Ctrl+c
            marks=pytest.mark.windows,
        ),
    ],
)
async def test_libtelio_remote(config) -> None:
    try:
        async with AsyncExitStack() as exit_stack:
            env = await exit_stack.enter_async_context(setup_environment(
                exit_stack,
                [
                    SetupParameters(
                        connection_tag=config[1],
                        adapter_type=config[2],
                    ),
                    SetupParameters(
                        connection_tag=ConnectionTag.DOCKER_CONE_CLIENT_2,
                        adapter_type=telio.AdapterType.LinuxNativeWg,
                    ),
                ],
            ))
            alpha, beta = env.nodes

            client_alpha, client_beta = env.clients
            connection_alpha, connection_beta = [
                conn.connection for conn in env.connections
            ]
            path_type = PathType.Direct
            asyncio.gather(
                client_alpha.wait_for_state_peer(
                    beta.public_key, [State.Connected], [path_type]
                ),
                client_beta.wait_for_state_peer(
                    alpha.public_key, [State.Connected], [path_type]
                ),
            )
            
            if connection_alpha.target_os == TargetOS.Windows:
                # taskkill /IM notepad.exe /F
                await connection_alpha.create_process(["taskkill", "/IM", "python.exe", "/F"]).execute()
                print("killed on windows")
                sys.stdout.flush()
            else:
                await connection_alpha.create_process(["killall", "-s", config[0], "python3"]).execute()
                
            await asyncio.sleep(10)
    except ConnectionClosedError as e:
        assert "Stopping the device" in get_tcli_from(ConnectionTag.DOCKER_CONE_CLIENT_1)
    except Exception as e:
        # Windows ...
        print(f"XXXXXXX e type {e.__class__.__name__}")
        print(f"XXXXXXX e", e)
        # raise e


def get_tcli_from(container_tag):
    result = subprocess.run([
        "docker",
        "exec",
        container_id(container_tag),
        "cat",
        "/tcli.log"
    ], capture_output=True, text=True, check=True)
    return result.stdout
