"""mini-swe-agent 固定容器适配器；无需继承 DockerEnvironment。

由 wangcj 的 Python 3.11 执行。密钥、宿主机环境变量不传给网关。
"""
import json
import platform
import posixpath
import subprocess


class FixedSandboxEnvironment:
    def __init__(self, *, cwd='/workspace', timeout=30):
        self.cwd = self._cwd(cwd)
        self.timeout = self._timeout(timeout)

    @staticmethod
    def _cwd(value):
        if not isinstance(value, str) or '\x00' in value or len(value) > 1024:
            raise ValueError('cwd 格式不正确')
        value = posixpath.normpath(value)
        if value != '/workspace' and not value.startswith('/workspace/'):
            raise ValueError('cwd 必须位于容器 /workspace')
        return value

    @staticmethod
    def _timeout(value):
        if type(value) is not int or not 1 <= value <= 120:
            raise ValueError('timeout 必须为 1–120 的整数')
        return value

    def execute(self, action, cwd='', *, timeout=None):
        command = action.get('command')
        if not isinstance(command, str) or not command.strip() or '\x00' in command:
            raise ValueError('command 必须为非空且不含 NUL 的字符串')
        seconds = self._timeout(self.timeout if timeout is None else timeout)
        request = {'command': command, 'cwd': self._cwd(cwd or self.cwd), 'timeout': seconds}
        wire = json.dumps(request, ensure_ascii=False) + '\n'
        if len(command.encode('utf-8')) > 8192 or len(wire.encode('utf-8')) > 16384:
            raise ValueError('请求过长')
        try:
            result = subprocess.run(
                ['/usr/bin/sudo', '-n', '--', '/usr/local/libexec/wangcj-agent-exec'],
                input=wire, text=True, encoding='utf-8', errors='replace',
                capture_output=True, timeout=seconds + 75,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            # 通信异常时中止 Agent；绝不把命令交给 LocalEnvironment。
            raise RuntimeError('沙箱入口不可用或失联，请管理员检查；停止当前 Agent 任务') from error
        try:
            reply = json.loads(result.stdout)
            valid = (isinstance(reply, dict) and type(reply.get('returncode')) is int
                     and isinstance(reply.get('output'), str)
                     and isinstance(reply.get('exception_info'), str)
                     and type(reply.get('fatal')) is bool)
            if not valid:
                raise ValueError('响应字段不匹配')
        except (ValueError, TypeError) as error:
            raise RuntimeError('沙箱响应无效；先检查精确 sudo 授权和入口安装') from error
        if result.returncode != 0 or reply['fatal']:
            raise RuntimeError('停止当前 Agent 任务：' + reply['exception_info'])
        self._check_finished(reply)
        return reply

    @staticmethod
    def _check_finished(reply):
        lines = reply['output'].lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' and reply['returncode'] == 0:
            from minisweagent.exceptions import Submitted
            submission = ''.join(lines[1:])
            raise Submitted({'role': 'exit', 'content': submission,
                             'extra': {'exit_status': 'Submitted', 'submission': submission}})

    def get_template_vars(self, **kwargs):
        return {**platform.uname()._asdict(), 'cwd': self.cwd, 'timeout': self.timeout, **kwargs}

    def serialize(self):
        return {'info': {'config': {
            'environment': {'cwd': self.cwd, 'timeout': self.timeout},
            'environment_type': f'{type(self).__module__}.{type(self).__name__}',
        }}}
