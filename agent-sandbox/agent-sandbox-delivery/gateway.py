#!/usr/bin/python3 -I
"""Root 安装的固定沙箱入口。兼容宿主机 Python 3.8，仅使用标准库。

不接受命令行参数；请求从 stdin 的一行 JSON 读取。
修改这些常量后必须由管理员重新审阅并安装，普通用户不能修改已安装副本。
"""
import json
import os
from pathlib import Path
import posixpath
import queue
import signal
import stat
import subprocess
import sys
import threading
import time

CONTAINER_ID = '8b016967e4c39a5c29ee1b2d304b7c1ec6b280a4abc74fcaec9f2529f56bffef'
IMAGE_ID = 'sha256:a5a75f3e116082fe7a9d4b56269636515fc51e095c66874bee4e9e615828a8a5'
WORKSPACE = '/home/wangcj/ai-learn/agent-learn/agent-sandbox/workspace'
STATE_DIR = Path('/var/lib/wangcj-agent-gateway')
TMPFS = {
    '/tmp': 'rw,nosuid,nodev,size=2g,mode=1777',
    '/home/sandbox': 'rw,nosuid,nodev,size=512m,uid=1020,gid=1020,mode=700',
}
MAX_OUTPUT = 256 * 1024
HANDLED_SIGNALS = tuple(getattr(signal, name) for name in ('SIGINT', 'SIGTERM', 'SIGHUP') if hasattr(signal, name))
# 不继承调用者的 DOCKER_HOST、配置路径、代理、Python 路径或 API Key。
CLEAN_ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}


class ExecutionFault(Exception):
    pass


def validate_request(data):
    if not isinstance(data, dict) or set(data) - {'command', 'cwd', 'timeout'}:
        raise ValueError('请求只能包含 command、cwd、timeout')
    command = data.get('command')
    if not isinstance(command, str) or not command.strip() or '\x00' in command:
        raise ValueError('command 必须为非空、无 NUL 字符的字符串')
    if len(command.encode('utf-8')) > 8192:
        raise ValueError('命令超过 8 KiB')
    timeout = data.get('timeout', 30)
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('timeout 必须为 1–120 的整数')
    cwd = data.get('cwd', '/workspace')
    if not isinstance(cwd, str) or '\x00' in cwd or len(cwd) > 1024:
        raise ValueError('cwd 格式不正确')
    cwd = posixpath.normpath(cwd)
    if cwd != '/workspace' and not cwd.startswith('/workspace/'):
        raise ValueError('cwd 必须在容器 /workspace 下')
    return {'command': command, 'cwd': cwd, 'timeout': timeout}


def validate_container(data):
    """匹配失败直接拒绝；容器是管理员预先创建的，不在这里修正配置。"""
    cfg, host, state = data.get('Config', {}), data.get('HostConfig', {}), data.get('State', {})
    expected_host = {
        'Privileged': False, 'ReadonlyRootfs': True, 'NetworkMode': 'none',
        'PidMode': '', 'IpcMode': 'private', 'UTSMode': '',
        'Memory': 34359738368, 'MemorySwap': 34359738368,
        'NanoCpus': 4000000000, 'PidsLimit': 512, 'Init': True,
    }
    for key, expected in expected_host.items():
        if host.get(key) != expected:
            raise ValueError('容器配置不匹配：' + key)
    for key, expected in [('Id', CONTAINER_ID), ('Name', '/agent-sandbox'), ('Image', IMAGE_ID)]:
        if data.get(key) != expected:
            raise ValueError('容器身份不匹配：' + key)
    if cfg.get('User') != '1020:1020':
        raise ValueError('容器默认用户不匹配')
    labels = cfg.get('Labels') or {}
    if labels.get('owner') != 'wangcj' or labels.get('purpose') != 'agent-learning':
        raise ValueError('容器所有者标签不匹配')
    if not state.get('Running') or state.get('Paused') or state.get('Restarting'):
        raise ValueError('沙箱必须处于正常 running 状态')
    if host.get('CapAdd') or {v.upper() for v in host.get('CapDrop') or []} != {'ALL'}:
        raise ValueError('容器能力设置不匹配')
    if set(host.get('SecurityOpt') or []) != {'no-new-privileges:true'}:
        raise ValueError('容器 SecurityOpt 不匹配')
    if (host.get('RestartPolicy') or {}).get('Name') != 'no':
        raise ValueError('沙箱必须禁用自动重启')
    if host.get('Devices') or host.get('DeviceRequests') or host.get('PortBindings'):
        raise ValueError('沙箱不能附加设备或端口映射')
    # 当前部署参数要求精确匹配；管理员改参数时同步审阅策略。
    if host.get('Tmpfs') != TMPFS:
        raise ValueError('tmpfs 配置不匹配')
    mounts = data.get('Mounts', [])
    if len(mounts) != 1:
        raise ValueError('只允许唯一 workspace 挂载')
    mount = mounts[0]
    if (mount.get('Type'), mount.get('Source'), mount.get('Destination'), mount.get('RW')) != (
        'bind', WORKSPACE, '/workspace', True
    ):
        raise ValueError('workspace 挂载不匹配')


def docker_argv(args):
    return ['/usr/bin/docker', '--config', str(STATE_DIR), '--host', 'unix:///var/run/docker.sock'] + args


def exec_argv(request):
    # command 永远是容器 shell 的一个参数；宿主机 subprocess 不使用 shell=True。
    return docker_argv([
        'exec', '--user', '1020:1020', '--workdir', request['cwd'], CONTAINER_ID,
        '/usr/bin/env', '-i', 'HOME=/home/sandbox', 'PATH=/usr/local/bin:/usr/bin:/bin',
        'LANG=C.UTF-8', 'LC_ALL=C.UTF-8', 'CMAKE_BUILD_PARALLEL_LEVEL=4',
        'OMP_NUM_THREADS=4', 'PYTHONUNBUFFERED=1', 'PIP_NO_CACHE_DIR=1',
        '/bin/bash', '--noprofile', '--norc', '-c', request['command'],
    ])


def run_bounded(argv, timeout, limit):
    """限制读取字节数和墙钟时间。这里只终止客户端；调用者负责停止沙箱。"""
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=CLEAN_ENV, cwd=os.path.abspath(os.sep), bufsize=0, close_fds=True,
    )
    chunks = queue.Queue(maxsize=8)
    done = threading.Event()

    def read_output():
        try:
            while not done.is_set():
                chunk = os.read(process.stdout.fileno(), 8192)
                while not done.is_set():
                    try:
                        chunks.put(chunk, timeout=0.05)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    return
        except (OSError, ValueError):
            # 主线程超时后关闭了管道；错误时仍由主线程的墙钟限制收尾。
            return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionFault('执行超过时间上限')
            try:
                chunk = chunks.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if not chunk:
                try:
                    code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise ExecutionFault('执行超过时间上限')
                return code, output.decode('utf-8', errors='replace')
            if len(output) + len(chunk) > limit:
                raise ExecutionFault('输出超过上限')
            output.extend(chunk)
    finally:
        done.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        reader.join(timeout=0.2)
        process.stdout.close()


def control(args):
    code, output = run_bounded(docker_argv(args), 10, 1024 * 1024)
    if code != 0:
        raise ExecutionFault('Docker 管理查询或故障清理失败，请管理员检查专用沙箱')
    return output


def inspect_container():
    data = json.loads(control(['inspect', CONTAINER_ID]))
    if not isinstance(data, list) or len(data) != 1 or data[0].get('Id') != CONTAINER_ID:
        raise ExecutionFault('Docker 返回的容器 ID 不匹配')
    return data[0]


def process_ids():
    lines = control(['top', CONTAINER_ID, '-eo', 'pid']).splitlines()
    if not lines or lines[0].strip() != 'PID':
        raise ExecutionFault('无法解析沙箱进程列表')
    return {int(line.strip()) for line in lines[1:] if line.strip()}


def stop_sandbox():
    # 唯一允许的生命周期动作；只处理固定完整 ID，不查询或匹配其他容器。
    control(['stop', '--time', '2', CONTAINER_ID])
    if inspect_container().get('State', {}).get('Running') is not False:
        raise ExecutionFault('未能确认沙箱已经停止；请管理员立即检查')


def execute_checked(request, active):
    """在调用者持有 root 锁时执行。active 标记只有成功收尾后才删除。"""
    validate_container(inspect_container())
    baseline = process_ids()
    if len(baseline) != 2:
        raise ExecutionFault('沙箱不是空闲的 init + sleep 状态，请管理员检查')
    # crash/SIGKILL 后留下标记；后续请求不能盲目再次进入。
    active.write_text(json.dumps({'container_id': CONTAINER_ID, 'started_at': time.time()}), encoding='utf-8')
    try:
        code, output = run_bounded(exec_argv(request), request['timeout'], MAX_OUTPUT)
        if code < 0 or code >= 125:
            raise ExecutionFault('执行异常退出，返回码 ' + str(code))
        if process_ids() != baseline:
            raise ExecutionFault('命令结束后仍有额外进程，禁止后台服务或残留任务')
        validate_container(inspect_container())
    except BaseException as failure:
        # 收尾阶段忽略重复终端信号，尽力完成对固定沙箱的停止。
        for sig in HANDLED_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        try:
            stop_sandbox()
            suffix = '；专用沙箱已停止，需管理员检查并恢复'
        except Exception:
            suffix = '；无法确认沙箱停止，请管理员立即检查，后续执行已锁定'
        raise ExecutionFault(str(failure) + suffix)
    active.unlink()
    return {'output': output, 'returncode': code, 'exception_info': '', 'fatal': False}


def read_request():
    import select
    deadline = time.monotonic() + 5
    data = bytearray()
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([sys.stdin.fileno()], [], [], remaining)[0]:
            raise ValueError('输入超时，请提供一行 JSON')
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 16384:
            raise ValueError('输入超过 16 KiB')
    # json.loads 同时拒绝多行中夹带第二个 JSON 请求。
    return validate_request(json.loads(data.decode('utf-8')))


def interrupt(signum, frame):
    raise ExecutionFault('执行入口收到中断信号')


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise ValueError('必须通过管理员安装的无参数 sudo 入口运行')
    if os.environ.get('SUDO_USER', 'root') not in ('root', 'wangcj'):
        raise ValueError('调用用户不匹配')
    os.umask(0o077)
    info = STATE_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('状态目录必须为 root 所有的 0700 普通目录')
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, interrupt)
    request = read_request()
    import fcntl
    with (STATE_DIR / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExecutionFault('已有任务正在执行，请等待结束')
        active = STATE_DIR / 'active'
        if active.exists():
            raise ExecutionFault('上次任务未正常收尾，需管理员检查 active 状态')
        return execute_checked(request, active)


if __name__ == '__main__':
    try:
        response = main()
    except Exception as error:
        response = {'output': '', 'returncode': -1, 'exception_info': str(error), 'fatal': True}
    print(json.dumps(response, ensure_ascii=True), flush=True)
    sys.exit(2 if response['fatal'] else 0)
