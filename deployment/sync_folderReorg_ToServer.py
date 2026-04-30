#!/usr/bin/env python3
"""
folderReorg - Sync to Server Deployment Script
Mirrors the insAPI sync/deploy flow for deployment on aizh.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_PATH = "/home/michael.gerber/folderReorg"
SSH_USER = "michael.gerber"
SERVER_NAME = "aizh"
SSH_CONNECT_TIMEOUT = 7

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
TAR_NAME = "folderReorg-project.tar"
DEPLOY_SCRIPT_NAME = "deploy_folderReorg.sh"


def run(cmd: list[str], *, timeout: int | None = None, check: bool = False, cwd: Path | None = None):
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check, cwd=cwd)


def convert_to_unix_line_endings(file_path: Path) -> None:
    content = file_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    file_path.write_bytes(content)


def ssh_cmd(remote_cmd: str, timeout: int = 90):
    cmd = [
        "ssh",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        f"{SSH_USER}@{SERVER_NAME}",
        remote_cmd,
    ]
    return run(cmd, timeout=timeout)


def test_ssh_connection(max_retries: int = 3) -> bool:
    print("[STEP 1] Testing SSH connection...")
    for i in range(1, max_retries + 1):
        try:
            result = ssh_cmd('echo "SSH_OK"', timeout=SSH_CONNECT_TIMEOUT + 3)
            if result.returncode == 0 and "SSH_OK" in result.stdout:
                print("[OK] SSH connection successful")
                return True
            print(f"[WARNING] SSH attempt {i}/{max_retries} failed: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"[WARNING] SSH attempt {i}/{max_retries} timed out")
    print("[ERROR] SSH connection failed")
    print(f"[INFO] Try manually: ssh {SSH_USER}@{SERVER_NAME}")
    return False


def ensure_server_directory() -> bool:
    print(f"[STEP 2] Ensuring server directory exists: {SERVER_PATH}")
    result = ssh_cmd(f"mkdir -p {SERVER_PATH} && test -w {SERVER_PATH} && echo READY", timeout=60)
    if result.returncode == 0 and "READY" in result.stdout:
        print("[OK] Server directory ready")
        return True
    print(f"[ERROR] Could not prepare server directory: {result.stderr.strip()}")
    return False


def clear_server_directory() -> bool:
    print("[STEP 3] Clearing old server files (keep runtime state + qdrant data)...")
    keep_names = {
        "data",
        "kb",
        "qdrant_data_personal",
        "qdrant_data_360f",
        "deployment",
        ".env",
        "docker",
    }
    keep_expr = " ".join(f"! -name '{name}'" for name in keep_names)
    cmd = f"find {SERVER_PATH} -mindepth 1 -maxdepth 1 {keep_expr} -exec rm -rf {{}} + 2>/dev/null; echo CLEARED"
    result = ssh_cmd(cmd, timeout=120)
    if result.returncode == 0 and "CLEARED" in result.stdout:
        print("[OK] Server directory cleanup completed")
        return True
    print("[WARNING] Cleanup returned warnings; continuing")
    return True


def create_tarball(temp_dir: Path, include_docs: bool) -> Path:
    print("[STEP 4] Creating project tar archive...")
    tar_file = temp_dir / TAR_NAME
    if tar_file.exists():
        tar_file.unlink()

    excludes = [
        "--exclude=.git",
        "--exclude=.venv",
        "--exclude=__pycache__",
        "--exclude=.pytest_cache",
        "--exclude=.ruff_cache",
        "--exclude=.cursor",
        "--exclude=agent-transcripts",
        "--exclude=plans",
        "--exclude=source_local",
        "--exclude=target_local",
        "--exclude=data",
        "--exclude=logs",
        "--exclude=qdrant_data*",
        "--exclude=kb/data",
        "--exclude=*.log",
        "--exclude=*.tmp",
        "--exclude=.env",
    ]
    if not include_docs:
        excludes.append("--exclude=docs")

    cmd = ["tar", "-cf", str(tar_file), *excludes, "."]
    result = run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"[ERROR] Failed to create tar: {result.stderr}")
        sys.exit(1)
    print(f"[OK] Tar created: {tar_file} ({tar_file.stat().st_size / (1024 * 1024):.2f} MB)")
    return tar_file


def prepare_deploy_script(temp_dir: Path) -> Path:
    source = SCRIPT_DIR / DEPLOY_SCRIPT_NAME
    if not source.exists():
        print(f"[ERROR] Missing deploy script: {source}")
        sys.exit(1)
    target = temp_dir / DEPLOY_SCRIPT_NAME
    shutil.copy2(source, target)
    convert_to_unix_line_endings(target)
    print("[OK] Prepared deploy script")
    return target


def scp_file(local: Path, remote: str, desc: str, timeout: int = 600) -> bool:
    cmd = [
        "scp",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        str(local),
        f"{SSH_USER}@{SERVER_NAME}:{remote}",
    ]
    try:
        result = run(cmd, timeout=timeout)
        if result.returncode == 0:
            print(f"[OK] Copied {desc}")
            return True
        print(f"[ERROR] Failed copying {desc}: {result.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"[ERROR] SCP timeout for {desc}")
        return False


def run_remote_deploy(timeout_minutes: int, no_deploy: bool, chat_personal_port: int, chat_360f_port: int) -> bool:
    if no_deploy:
        print("[INFO] --no-deploy set, skipping remote deployment")
        return True

    print("[STEP 6] Running remote deployment script...")
    remote_cmd = (
        f"cd {SERVER_PATH} && "
        f"chmod +x {DEPLOY_SCRIPT_NAME} && "
        f"CHAT_PERSONAL_HOST_PORT={chat_personal_port} "
        f"CHAT_360F_HOST_PORT={chat_360f_port} "
        f"RUNPY_REVIEW_URL=http://127.0.0.1:8051 "
        f"./{DEPLOY_SCRIPT_NAME}"
    )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                f"{SSH_USER}@{SERVER_NAME}",
                remote_cmd,
            ],
            timeout=timeout_minutes * 60,
            text=True,
            capture_output=False,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Deployment timed out after {timeout_minutes} minutes")
        return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Sync folderReorg to aizh and optionally deploy",
    )
    p.add_argument("--no-deploy", action="store_true", help="Only sync files; do not execute remote deploy script")
    p.add_argument("--timeout", type=int, default=40, metavar="MIN", help="Remote deploy timeout in minutes")
    p.add_argument("--include-docs", action="store_true", help="Include docs in the synced tar")
    p.add_argument("--chat-personal-port", type=int, default=8052, choices=range(8051, 8061))
    p.add_argument("--chat-360f-port", type=int, default=8053, choices=range(8051, 8061))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print("========================================")
    print("folderReorg - Sync to Server Deployment")
    print("========================================")
    print(f"[INFO] Server: {SERVER_NAME}")
    print(f"[INFO] Server path: {SERVER_PATH}")
    print(f"[INFO] Project root: {PROJECT_ROOT}")
    print(f"[INFO] Ports: personal={args.chat_personal_port} 360f={args.chat_360f_port}")
    print()

    if not test_ssh_connection():
        return 1
    if not ensure_server_directory():
        return 1
    clear_server_directory()

    temp_dir = Path(tempfile.gettempdir())
    tar_file = create_tarball(temp_dir, include_docs=args.include_docs)
    deploy_script = prepare_deploy_script(temp_dir)

    print("[STEP 5] Copying files to server...")
    if not scp_file(tar_file, f"{SERVER_PATH}/{TAR_NAME}", "project tar"):
        return 1
    if not scp_file(deploy_script, f"{SERVER_PATH}/{DEPLOY_SCRIPT_NAME}", "deploy script"):
        return 1

    if not run_remote_deploy(
        timeout_minutes=args.timeout,
        no_deploy=args.no_deploy,
        chat_personal_port=args.chat_personal_port,
        chat_360f_port=args.chat_360f_port,
    ):
        return 1

    print()
    print("========================================")
    print("SYNC COMPLETED")
    print("========================================")
    if args.no_deploy:
        print("Next steps:")
        print(f"1. ssh {SSH_USER}@{SERVER_NAME}")
        print(f"2. cd {SERVER_PATH}")
        print(f"3. chmod +x {DEPLOY_SCRIPT_NAME} && ./{DEPLOY_SCRIPT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
