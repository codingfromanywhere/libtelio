import asyncio
from aiodocker import Docker
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from utils.connection import Connection, DockerConnection
from utils.router.linux_router import INTERFACE_NAME


async def _prepare(connection: Connection) -> None:
    await connection.create_process(["conntrack", "-F"]).execute()
    await connection.create_process(
        ["iptables-save", "-f", "iptables_backup"]
    ).execute()
    await connection.create_process(
        ["ip6tables-save", "-f", "ip6tables_backup"]
    ).execute()

    try:
        await connection.create_process(
            ["ip", "link", "delete", INTERFACE_NAME]
        ).execute()
    except:
        pass  # Most of the time there will be no interface to be deleted


async def _reset(connection: Connection) -> None:
    await connection.create_process(["conntrack", "-F"]).execute()

    for table in ["filter", "nat", "mangle", "raw", "security"]:
        await connection.create_process(["iptables", "-t", table, "-F"]).execute()
        await connection.create_process(["ip6tables", "-t", table, "-F"]).execute()
    await connection.create_process(["iptables-restore", "iptables_backup"]).execute()
    await connection.create_process(["ip6tables-restore", "ip6tables_backup"]).execute()


@asynccontextmanager
async def get(docker: Docker, container_name: str) -> AsyncIterator[DockerConnection]:
    connection = DockerConnection(
        await docker.containers.get(container_name), container_name
    )
    try:
        await _prepare(connection)
        yield connection
    finally:
        await _reset(connection)


class ContainerRestartError(Exception):
    name: str
    last_exception: Exception

    def __init__(self, name, last_exception) -> None:
        self.name = name
        self.last_exception = last_exception


async def wait_for_docker(container_name):
    last_exception = None
    print(datetime.now(), "Will wait for", container_name)
    for _i in range(50):
        async with Docker() as docker:
            try:
                connection = DockerConnection(
                    await docker.containers.get(container_name), container_name
                )
                await connection.create_process(
                    ["true"]
                ).execute()  # Just a random command
                print(datetime.now(), f"{container_name} is up and reachable")
                return
            except Exception as e:  # pylint: disable=broad-exception-caught
                last_exception = e
                await asyncio.sleep(0.2)
    raise ContainerRestartError(container_name, last_exception)
