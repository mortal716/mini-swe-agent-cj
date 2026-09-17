# ============================================================
# Agent 通用代码实验沙箱：C / C++ / Python
#
# 支持：
#   - C/C++ 编译、测试
#   - Python 脚本、虚拟环境
#   - Bash 命令、Git 操作、代码搜索
#
# 注意：
#   - 这里只准备工具环境，不自动运行 Agent。
#   - 不写入 API Key，不安装 Docker 服务。
#   - 不修改宿主机系统或其他容器。
#   - CPU、内存、网络、目录挂载在创建容器时配置。
# ============================================================

# 容器内使用 Ubuntu 24.04。
# 容器仍共享宿主机内核，这不是安装一台新的虚拟机。
# 通过 DaoCloud 代理获取 Ubuntu 24.04 基础镜像
FROM m.daocloud.io/docker.io/library/ubuntu:24.04

# 安装基础工具。
#
# build-essential：GCC、G++、Make 等编译工具
# cmake、ninja-build：C/C++ 项目构建工具
# python3：Python 解释器
# python3-venv：创建 Python 虚拟环境，环境内可使用 pip
# git：版本管理和代码差异查看
# ripgrep：提供 rg 命令，用于搜索代码
# ca-certificates：HTTPS 证书验证
# curl：下载文件、测试 HTTP 接口；实际联网能力由容器网络决定
#
# -y：自动确认安装
# --no-install-recommends：不安装额外的推荐软件包
# DEBIAN_FRONTEND=noninteractive：避免等待交互输入
#
# 清理操作只发生在镜像构建环境的 apt 缓存目录内，
# 不会清理宿主机文件或其他镜像。
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        ninja-build \
        python3 \
        python3-venv \
        git \
        ripgrep \
        ca-certificates \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建容器内部的普通用户和用户组。
#
# UID/GID 与宿主机 wangcj 的 1020:1020 对齐，
# 方便以后读写挂载的实验目录。
# 这不会修改宿主机账号。
#
# /workspace：实验代码目录
# /home/sandbox：容器内普通用户的主目录
RUN groupadd --gid 1020 sandbox \
    && useradd --uid 1020 --gid 1020 --create-home --shell /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

# 默认使用 UTF-8 编码。
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# 容器内部的主目录，与宿主机的 home 目录不是一回事。
ENV HOME=/home/sandbox

# Python 输出及时写入日志，减少缓冲导致的输出延迟。
ENV PYTHONUNBUFFERED=1

# 不保存 pip 下载缓存，减少实验期间的磁盘占用。
# 这不是磁盘配额，也不会限制其他文件的大小。
ENV PIP_NO_CACHE_DIR=1

# 默认工作目录。
WORKDIR /workspace

# 默认以普通用户运行，不使用容器内 root。
USER 1020:1020

# 保持容器运行，等待后续通过受限入口提交任务。
# 此处不启动 Agent、不调用 API、不执行实验代码。
CMD ["sleep", "infinity"]