import pytest
import telio
from contextlib import AsyncExitStack
from helpers import SetupParameters, setup_environment
from utils.connection_util import ConnectionTag
from utils.vm.windows_vm_util import _get_network_interface_tunnel_keys


@pytest.mark.asyncio
@pytest.mark.windows
@pytest.mark.parametrize(
    "adapter_type",
    [
        telio.AdapterType.WireguardGo,
        telio.AdapterType.WindowsNativeWg,
    ],
)
async def test_get_network_interface_tunnel_keys(adapter_type) -> None:
    async with AsyncExitStack() as exit_stack:
        env = await exit_stack.enter_async_context(
            setup_environment(
                exit_stack,
                [
                    SetupParameters(
                        connection_tag=ConnectionTag.WINDOWS_VM_1,
                        adapter_type=adapter_type,
                    ),
                    SetupParameters(
                        connection_tag=ConnectionTag.DOCKER_CONE_CLIENT_2,
                        adapter_type=telio.AdapterType.LinuxNativeWg,
                    ),
                ],
            )
        )

        connection_alpha = env.connections[0].connection

        # This function is used during test startup to remove interfaces
        # that might have managed to survive the end of the previous test.
        assert [
            "HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}\\0005"
        ] == await _get_network_interface_tunnel_keys(connection_alpha)
