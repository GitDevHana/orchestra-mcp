"""Explicit command execution policies; container failure never falls back to host."""
from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from .environment import child_environment
from .process import spawn


def check_policy(spec):
    if not spec.validation_commands and not spec.execution_commands:
        return
    if spec.command_backend == "container":
        if not spec.command_image:
            raise ValueError("검증 명령에 사용할 컨테이너 이미지를 지정하세요. 호스트 실행은 trusted_host를 명시하고 승인해야 합니다.")
        if not shutil.which("docker"):
            raise ValueError("Docker is not installed; isolated commands cannot run. No host fallback.")


def container_argv(spec, argv, name):
    workspace = str(Path(spec.workspace).resolve())
    if "," in workspace:
        raise ValueError("Container workspace path cannot contain a comma")
    return [shutil.which("docker") or "docker", "run", "--rm", "--pull=never", "--init", "--name", name,
            "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--pids-limit=128", "--memory=1g", "--cpus=2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--mount",
            f"type=bind,src={workspace},dst=/workspace", "--workdir", "/workspace",
            spec.command_image, *argv]


async def launch_command(spec, argv):
    check_policy(spec)
    name = "orchestra-" + uuid.uuid4().hex if spec.command_backend == "container" else None
    command = container_argv(spec, argv, name) if name else argv
    process, tree = await spawn(command, spec.workspace, child_environment())
    return process, tree, name


async def remove_container(name, workspace):
    if not name:
        return
    process, tree = await spawn([shutil.which("docker") or "docker", "rm", "-f", name], workspace, child_environment())
    try:
        async with asyncio.timeout(10):
            await process.communicate()
    finally:
        tree.close()
        await process.wait()
