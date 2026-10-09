# macOS 迁移包使用说明

本目录由原 Windows 账号导出，包含程序、设备池、工程、生成包和部署历史。`cloud-edge-workbench-macos.cew` 是加密包，`migration-key.bin` 是单独的 32 字节密钥；两者都需要带到 Mac。不要公开分享密钥或生成包。

## 在 MacBook Pro 上安装

1. 安装 Python 3.11 或更新版本，并确保终端可使用 `python3`。首次安装依赖需要联网。
2. 将本目录完整复制到 Mac，打开终端进入该目录。
3. 执行 `bash install_macos_bundle.sh`。默认安装到 `~/cloud-edge-workbench`；也可以把目标目录作为参数传入。
4. 执行 `cd ~/cloud-edge-workbench && bash install-macos.sh`。
5. 执行 `bash start-macos.sh`，浏览器访问 `http://127.0.0.1:18768/`。停止时执行 `bash stop-macos.sh`。

安装器不会覆盖已有目标目录。当前工程中指向 Windows 的内置模板路径会在副本中升级到 Mac 路径，并生成新的工程版本；旧版本、计划、生成包和部署记录保留。自定义模板若位于项目外部，请另行复制到 Mac，再在界面中修改路径并重新计算计划。

迁移包不会复制 Windows 虚拟环境或后台运行进程。Windows DPAPI 数据在导出时转换为可由 Mac 读取的 AES-GCM 格式；原 Windows 数据不修改。历史部署记录可查看，正在运行或恢复中的远端任务不能依靠迁移包直接续接，须先在原主机完成或核实远端状态。
