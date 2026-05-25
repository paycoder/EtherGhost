import os
import pwd
import subprocess
import tempfile


def test_ssh_askpass_file_owner():
    askpass_fd, askpass_path = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(askpass_fd, "w") as f:
        f.write("#!/bin/sh\necho testssh123\n")
    os.chmod(askpass_path, 0o755)

    test_file = "/tmp/ssh_test_file.txt"
    env = os.environ.copy()
    env["SSH_ASKPASS"] = askpass_path
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["DISPLAY"] = ":0"

    result = subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "testssh@127.0.0.1",
            f"touch {test_file}",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"SSH failed: {result.stderr}"

    stat_info = os.stat(test_file)
    owner = pwd.getpwuid(stat_info.st_uid).pw_name
    assert owner == "testssh", f"Expected owner testssh, got {owner}"

    os.unlink(askpass_path)
    os.unlink(test_file)
