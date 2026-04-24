import subprocess

def ssh_command(target_cfg, command):

    cmd = [
        "ssh",
        "-i",
        target_cfg["key_path"],
        f'{target_cfg["username"]}@{target_cfg["host"]}',
        command
    ]

    r = subprocess.run(cmd, capture_output=True, text=True)

    return {
        "stdout": r.stdout,
        "stderr": r.stderr,
        "returncode": r.returncode
    }
