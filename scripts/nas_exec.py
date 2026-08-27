#!/usr/bin/env python
"""NAS 远程命令执行辅助脚本。

凭证从 ~/.kiro/nas_credentials.json 读取（该文件在项目外，不会进 git），
避免把明文密码写进仓库。密码通过 paramiko 参数传递，不经过 shell，
因此不存在 shell 注入与历史扩展（`!`）问题。

用法:
    python scripts/nas_exec.py "docker ps"
    python scripts/nas_exec.py --file cmds.sh          # 逐行执行
    python scripts/nas_exec.py --script probe.sh       # 整体上传后执行（支持多行 python）
    python scripts/nas_exec.py --timeout 600 "长任务"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import paramiko

CRED_PATH = Path.home() / ".kiro" / "nas_credentials.json"

# NAS 输出含中文，Windows 控制台默认 GBK 会在写入时抛 UnicodeEncodeError，
# 这里强制标准流走 UTF-8 并对无法编码的字符降级替换。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def load_credentials() -> dict[str, object]:
    if not CRED_PATH.exists():
        raise SystemExit(f"凭证文件不存在: {CRED_PATH}")
    return json.loads(CRED_PATH.read_text(encoding="utf-8"))


def _connect(cred: dict[str, object]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # NAS 是内网固定主机，首次连接自动接受主机密钥
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=str(cred["host"]),
        port=int(cred.get("port", 22)),
        username=str(cred["user"]),
        password=str(cred["password"]),
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    return client


def _drain(client: paramiko.SSHClient, cmd: str, timeout: int) -> int:
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out, end="", flush=True)
    if err:
        print(f"[stderr] {err}", end="", file=sys.stderr, flush=True)
    if code != 0:
        print(f"[exit={code}]", flush=True)
    return code


def run_script(local_path: Path, timeout: int) -> int:
    """把整个脚本上传到 NAS 后一次性执行，避免按行拆分破坏多行语法。"""
    cred = load_credentials()
    client = _connect(cred)
    remote = f"/tmp/nas_exec_{local_path.stem}.sh"
    try:
        sftp = client.open_sftp()
        # 统一 LF，避免 Windows CRLF 导致 bash 报错
        body = local_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        with sftp.file(remote, "w") as handle:
            handle.write(body)
        sftp.close()
        print(f"$ bash {remote}", flush=True)
        code = _drain(client, f"bash {remote}", timeout)
        client.exec_command(f"rm -f {remote}")
        return code
    finally:
        client.close()


def run(commands: list[str], timeout: int) -> int:
    cred = load_credentials()
    client = _connect(cred)
    exit_code = 0
    try:
        for cmd in commands:
            if not cmd.strip():
                continue
            print(f"\n$ {cmd}", flush=True)
            code = _drain(client, cmd, timeout)
            if code != 0:
                exit_code = code
    finally:
        client.close()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="", help="要执行的远程命令")
    parser.add_argument("--file", default="", help="从文件按行读取命令")
    parser.add_argument("--script", default="", help="整体上传并执行的脚本文件")
    parser.add_argument("--timeout", type=int, default=300, help="单条命令超时秒数")
    parser.add_argument(
        "--out",
        default="",
        help="把输出直接写入该文件（UTF-8）。避免 PowerShell 重定向破坏中文编码。",
    )
    args = parser.parse_args()

    # 由 Python 自己以 UTF-8 打开文件，绕开 shell 重定向的编码转换
    handle = None
    if args.out:
        handle = open(args.out, "w", encoding="utf-8", newline="\n")
        sys.stdout = handle
        sys.stderr = handle

    try:
        if args.script:
            return run_script(Path(args.script), args.timeout)
        if args.file:
            commands = Path(args.file).read_text(encoding="utf-8").splitlines()
        elif args.command:
            commands = [args.command]
        else:
            parser.error("需要提供 command / --file / --script")
        return run(commands, args.timeout)
    finally:
        if handle is not None:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
