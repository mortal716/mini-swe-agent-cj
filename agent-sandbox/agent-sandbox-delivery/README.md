# 把 mini-swe-agent 接到已创建的固定沙箱

状态：本地交付代码，尚未在你的服务器安装或联调。不要把本地测试通过理解为远程权限配置已经成功。

## 这次增加什么

```text
wangcj 的 mini-swe-agent
    → fixed_sandbox.py：把 command/cwd/timeout 编码成 JSON
    → sudo 精确授权的 /usr/local/libexec/wangcj-agent-exec
    → Docker exec 固定完整容器 ID，用户 1020:1020
    → agent-sandbox 内运行 Bash、编译器、测试
    → 标准输出和退出码返回 Agent
```

- `gateway.py`：受限入口源码。安装后的副本由 root 所有，宿主机系统 Python 3.8 以 `-I` 隔离模式运行。
- `fixed_sandbox.py`：mini-swe-agent 的新环境适配器，使用现有 Python 3.11 虚拟环境。
- `wangcj-agent-sandbox.sudoers`：只允许无参数执行这个入口，不授权任意 Docker 命令。
- `tests/test_gateway.py`：标准库测试，无需安装 pytest，不调用真实 Docker 或 API。

正常命令结束不会停止容器。编译返回 1 时把错误交给 Agent。超过时间、输出超过 256 KiB、发现后台进程等异常时，入口只停止固定 ID 的 agent-sandbox，保留故障标记并要求人工恢复。默认 30 秒，最长 120 秒；每次一条命令，不能并行运行 Agent，也不能同时手工 docker exec。

> 故障停止会清空容器 tmpfs 中的临时内容；workspace 挂载文件保留。没有创建、删除、自动重启容器的功能。不会对另外两个容器执行操作。共享硬件资源及内核仍存在，本版本不是对抗恶意代码的安全执行平台。

## 1. 从 Windows 复制文件（Windows PowerShell）

这条命令在 **Windows 本地终端** 执行，不是在服务器里。它只上传到你的 Agent 仓库下的新目录，不更改运行中的容器。

```powershell
scp -r "D:\Desktop\ai-learn\agent-sandbox-delivery" wangcj@192.168.186.33:/home/wangcj/ai-learn/agent-learn/mini-swe-agent/agent-sandbox/
```

若本地没有 scp，使用 MobaXterm 左侧 SFTP，把整个 `agent-sandbox-delivery` 文件夹拖到服务器的 `mini-swe-agent/agent-sandbox/` 下。

## 2. 先测试源码（服务器 wangcj 终端）

```bash
cd /home/wangcj/ai-learn/agent-learn/mini-swe-agent
source .venv/bin/activate
python -m unittest discover -s agent-sandbox/agent-sandbox-delivery/tests -v
```

预期所有测试通过。测试用模拟 Docker 接口验证策略与异常控制流，同时真实运行短小 Python 子进程验证输出限制和超时；不会运行模型，不接触任何 Docker 容器。

将适配器放入项目源码（只新增文件，已有同名文件则停止）：

```bash
test ! -e src/minisweagent/environments/fixed_sandbox.py && install -m 0644 agent-sandbox/agent-sandbox-delivery/fixed_sandbox.py src/minisweagent/environments/fixed_sandbox.py
python -c "from minisweagent.environments.fixed_sandbox import FixedSandboxEnvironment; print(FixedSandboxEnvironment().serialize())"
```

这是新增加的环境实现，不继承官方 DockerEnvironment，不触发官方的自动创建/删除流程。源码通过 editable install 已经生效，不需要重新 pip install。

## 3. 管理员检查、安装入口（服务器 root 终端）

先确认机器和固定目标：

```bash
whoami
hostname
docker --host unix:///var/run/docker.sock inspect --format 'ID={{.Id}} 名称={{.Name}} 镜像={{.Image}} 状态={{.State.Status}}' 8b016967e4c39a5c29ee1b2d304b7c1ec6b280a4abc74fcaec9f2529f56bffef
namei -l /usr/local/libexec
```

必须是 fpga-2288H-V5，容器是 agent-sandbox，镜像 ID 是 `sha256:a5a75f3e116082fe7a9d4b56269636515fc51e095c66874bee4e9e615828a8a5`。`/usr/local/libexec` 可以尚不存在；已有路径各级必须由 root 管理，普通用户不可写。如果不符先停止，不要修改网关 ID 去指向其他容器。

先复制入口到 root 管理的路径。以下整体带括号执行，失败只退出这段安装操作，不退出 root 登录。已有安装会拒绝覆盖；不要为重试随意删除已有文件。

```bash
(
set -eu
test "$(id -u)" = 0
test ! -e /usr/local/libexec/wangcj-agent-exec
test ! -e /var/lib/wangcj-agent-gateway
test ! -e /etc/sudoers.d/wangcj-agent-sandbox
test ! -L /usr/local/libexec
if [ ! -d /usr/local/libexec ]; then
    install -d -o root -g root -m 0755 /usr/local/libexec
fi
install -d -o root -g root -m 0700 /var/lib/wangcj-agent-gateway
install -o root -g root -m 0755 /home/wangcj/ai-learn/agent-learn/mini-swe-agent/agent-sandbox/agent-sandbox-delivery/gateway.py /usr/local/libexec/wangcj-agent-exec
install -o root -g root -m 0440 /home/wangcj/ai-learn/agent-learn/mini-swe-agent/agent-sandbox/agent-sandbox-delivery/wangcj-agent-sandbox.sudoers /var/lib/wangcj-agent-gateway/sudoers.candidate
)
```

审阅安装后的 root 副本，再授权。源码核心函数：`validate_container` 检查容器；`exec_argv` 固定 Docker 参数；`execute_checked` 执行及收尾；`stop_sandbox` 是唯一停止入口。

```bash
less /usr/local/libexec/wangcj-agent-exec
cat /var/lib/wangcj-agent-gateway/sudoers.candidate
namei -l /usr/local/libexec/wangcj-agent-exec
```

`less` 中按 q 退出。确认没有与交付源码不同的修改，路径由 root 控制，再运行：

```bash
(
set -eu
test ! -e /etc/sudoers.d/wangcj-agent-sandbox
visudo -cf /var/lib/wangcj-agent-gateway/sudoers.candidate
install -o root -g root -m 0440 /var/lib/wangcj-agent-gateway/sudoers.candidate /etc/sudoers.d/wangcj-agent-sandbox
visudo -c
)
```

`visudo` 若报告错误，保留当前 root 会话，停止后续操作，把错误发回来。这里没有修改 Docker daemon、重启服务或添加 docker 用户组。

## 4. 第一次真实执行（服务器 wangcj 终端）

先只执行我们手写的只读命令：

```bash
printf '%s\n' '{"command":"id && pwd && g++ --version | head -n 1","cwd":"/workspace","timeout":10}' | sudo -n -- /usr/local/libexec/wangcj-agent-exec
```

预期是一个 JSON，output 里有 UID 1020、/workspace、G++ 版本，returncode 为 0、fatal 为 false。

如果说需要密码：不要把 wangcj 加进 docker 组；检查精确 sudoers 文件是否被系统加载。如果说某项容器配置不匹配：返回原始错误，管理员核对对应 inspect 字段；不要删校验来绕过。

再通过适配器调用同一入口：

```bash
cd /home/wangcj/ai-learn/agent-learn/mini-swe-agent
source .venv/bin/activate
python -c "from minisweagent.environments.fixed_sandbox import FixedSandboxEnvironment; print(FixedSandboxEnvironment().execute({'command': 'printf sandbox-adapter-ok'}))"
```

预期 output 为 sandbox-adapter-ok。这一步成功才说明普通用户 → 适配器 → 受限入口 → 容器真正打通。这里仍没有调用 DeepSeek，也不产生 API 费用。

## 5. 故障验收及恢复

先完成第 4 步，再单独测试超时。这个测试会**故意停止我们自己的 agent-sandbox**，暂时不能再编译；不会停止其他容器。

wangcj 执行：

```bash
printf '%s\n' '{"command":"sleep 20","timeout":1}' | sudo -n -- /usr/local/libexec/wangcj-agent-exec
```

预期 fatal 为 true，提示专用沙箱已停止，active 标记保留。root 检查精确目标：

```bash
docker --host unix:///var/run/docker.sock inspect --format 'ID={{.Id}} 状态={{.State.Status}}' 8b016967e4c39a5c29ee1b2d304b7c1ec6b280a4abc74fcaec9f2529f56bffef
cat /var/lib/wangcj-agent-gateway/active
```

确认是刚才的可控超时测试、容器已经 stopped/exited，再由 root 手动恢复。首次恢复把 active 改名保留记录；若已有 active.reviewed 则命令拒绝继续，先检查旧记录。

```bash
(
set -eu
test "$(docker --host unix:///var/run/docker.sock inspect --format '{{.State.Running}}' 8b016967e4c39a5c29ee1b2d304b7c1ec6b280a4abc74fcaec9f2529f56bffef)" = false
test -f /var/lib/wangcj-agent-gateway/active
test ! -e /var/lib/wangcj-agent-gateway/active.reviewed
docker --host unix:///var/run/docker.sock start 8b016967e4c39a5c29ee1b2d304b7c1ec6b280a4abc74fcaec9f2529f56bffef
mv -n /var/lib/wangcj-agent-gateway/active /var/lib/wangcj-agent-gateway/active.reviewed
)
```

未知原因的故障不要直接套用恢复命令；先查清任务、进程、容器状态。若停止失败，故障标记会继续拒绝新请求，但不代表旧进程已停止。

## 6. 与 Agent 的集成边界

环境类路径是：`minisweagent.environments.fixed_sandbox.FixedSandboxEnvironment`。本地 ZIP 的环境工厂支持完整类路径，但仍需以服务器实际版本为准核验：

```bash
sed -n '1,180p' src/minisweagent/environments/__init__.py
```

正式运行配置要指定这个环境，不能继续用默认 local。现有模型配置和密钥无需改动；不要输出 .env 或把 key 放入配置/轨迹。先验证环境工厂与正式启动方式，再设置单任务小步数和 token 上限接 DeepSeek。此交付不提供未经核验的自动模型启动命令。

第一项实际工程任务建议：在 workspace 下放一个独立 Git 管理的 C++ 小项目，给出一个失败单测，让 Agent 读取、修改、编译、重跑测试并输出 diff。环境适配完成后再扩展评测集。

## 7. 限制与后续维护

- 模型可以读写整个 workspace，只放你授权它访问的实验副本；不要放原始科研数据、密钥、SSH 配置。
- cwd 限制是默认工作位置限制，不是把容器文件系统缩小到某个工程。符号链接也不是新的权限边界。
- 100 GiB 尚无文件系统硬配额；检查 `du -sh /home/wangcj/ai-learn/agent-learn/agent-sandbox/workspace`，避免大量生成文件。
- root 网关最多保留 256 KiB 输出；构建日志很长时重定向到 workspace 的日志文件，再用 tail 读取，并主动管理日志大小。
- root 入口被 SIGKILL、Docker 不响应等情况下，持久 active 标记用于拒绝续跑，不承诺实时清理已失联的任务。宿主机资源限制继续依赖 Docker/cgroup。
- 修改资源配置或重建容器后，需要管理员同步审阅入口策略。绝不允许 Agent 自己选择容器或调高限制。
- API Key 在宿主机上，网络调用由 Agent 执行；容器仍是 network=none。
- 这批文件尚未帮你提交或推送 Git。服务器测试完成后可分别提交网关、适配器、测试与说明。

官方行为参考：[Docker exec](https://docs.docker.com/reference/cli/docker/container/exec/)、[Docker stop](https://docs.docker.com/reference/cli/docker/container/stop/)。已部署参数仍以你服务器 Docker 26 的真实输出为准。
