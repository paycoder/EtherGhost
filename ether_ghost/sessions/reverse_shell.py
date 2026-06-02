import asyncio
import typing as t
import logging
import uuid

from ..core import exceptions, SessionInterface

from ..core.base import (
    register_session,
    Option,
    OptionGroup,
    ProcessProtocol,
)

from .linux_shell_helper import LinuxShellHelper

logger = logging.getLogger("core.sessions.reverse_shell")

REVERSE_SHELL_SESSION_TYPE = "REVERSE_SHELL"


@register_session
class ReverseShellSession(SessionInterface):
    session_type = REVERSE_SHELL_SESSION_TYPE
    readable_name = "反弹Shell"
    conn_options: t.List[OptionGroup] = [
        {
            "name": "高级连接配置",
            "options": [
                Option(
                    id="chunk_size",
                    name="文件上传下载分块大小",
                    type="text",
                    placeholder="文件上传下载的分块大小，单位为字节，建议在1KB-1024KB之间",
                    default_value="1024",
                    alternatives=None,
                ),
                Option(
                    id="encoder",
                    name="命令编码器",
                    type="select",
                    placeholder="raw",
                    default_value="raw",
                    alternatives=[
                        {"name": "raw", "value": "raw"},
                        {"name": "base64_quote", "value": "base64_quote"},
                        {"name": "base64_ifs", "value": "base64_ifs"},
                    ],
                ),
                Option(
                    id="decoder",
                    name="解码器",
                    type="select",
                    placeholder="raw",
                    default_value="raw",
                    alternatives=[
                        {"name": "raw", "value": "raw"},
                        {"name": "base64", "value": "base64"},
                    ],
                ),
            ],
        }
    ]

    def __init__(
        self,
        config: dict,
        drop_self: t.Callable[[], None],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.drop_self = drop_self
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()

        self._shell = LinuxShellHelper(
            transport_fn=self._transport,
            encoder=str(config.get("encoder", "raw")),
            decoder=str(config.get("decoder", "raw")),
            chunk_size=int(config.get("chunk_size", 1024)),
            max_coro=None,
        )

    async def _transport(self, code: str) -> str:
        return await self.submit_socket(code)

    async def execute_cmd(self, cmd: str):
        return await self._shell.execute_cmd(cmd)

    async def test_usablility(self):
        return await self._shell.test_usablility()

    async def get_pwd(self):
        return await self._shell.get_pwd()

    async def list_dir(self, dir_path: str):
        return await self._shell.list_dir(dir_path)

    async def mkdir(self, dir_path: str):
        return await self._shell.mkdir(dir_path)

    async def get_file_contents(self, filepath: str, max_size: int = 1024 * 200):
        return await self._shell.get_file_contents(filepath, max_size)

    async def put_file_contents(self, filepath: str, content: bytes):
        return await self._shell.put_file_contents(filepath, content)

    async def modify_file(
        self,
        filepath: str,
        old_str: str,
        new_str: str,
        replace_strategy: t.Union[str, None] = None,
    ) -> None:
        return await self._shell.modify_file(
            filepath, old_str, new_str, replace_strategy
        )

    async def delete_file(self, filepath: str):
        return await self._shell.delete_file(filepath)

    async def move_file(self, filepath: str, new_filepath: str):
        return await self._shell.move_file(filepath, new_filepath)

    async def copy_file(self, filepath: str, new_filepath: str):
        return await self._shell.copy_file(filepath, new_filepath)

    async def upload_file(
        self, filepath: str, content: bytes, callback: t.Union[t.Callable, None] = None
    ) -> bool:
        return await self._shell.upload_file(filepath, content, callback)

    async def download_file(self, filepath: str, callback=None):
        return await self._shell.download_file(filepath, callback)

    async def open_reverse_shell(self, host: str, port: int) -> None:
        return await self._shell.open_reverse_shell(host, port)

    async def create_process(
        self,
        argv: t.List[str],
        overrides_env: t.Union[t.Dict[str, str], None] = None,
    ) -> "ProcessProtocol":
        raise NotImplementedError("反弹Shell session暂不支持创建进程")

    async def send_bytes_over_tcp(
        self,
        host: str,
        port: int,
        content: bytes,
        send_method: t.Union[str, None] = None,
    ) -> t.Union[bytes, None]:
        return await self._shell.send_bytes_over_tcp(host, port, content, send_method)

    async def get_send_tcp_support_methods(self) -> t.List[str]:
        return await self._shell.get_send_tcp_support_methods()

    async def get_basicinfo(self):
        return await self._shell.get_basicinfo()

    async def submit_socket(self, payload: t.Union[str, bytes]) -> str:
        command_end_marker = str(uuid.uuid4())
        async with self.lock:
            if isinstance(payload, str):
                payload = payload.encode()
            try:
                self.writer.write(bytes(payload) + b"\n")
                self.writer.write(
                    f"echo '{command_end_marker[:6]}' '{command_end_marker[6:]}'\n".encode()
                )
                await self.writer.drain()
                data = await self.reader.readuntil(
                    separator=command_end_marker.encode()
                )
                return data.decode()
            except ConnectionResetError as e:
                self.drop_self()
                raise exceptions.NetworkError("连接重置") from e
            except Exception as e:
                self.drop_self()
                raise e
