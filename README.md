# QOJ Submitter

一个直接与 QOJ 通信的命令行工具，支持登录、绑定比赛、下载样例和提交代码。无需常驻服务、浏览器窗口或油猴脚本。

支持 `qoj.ac` 和 `contest.ucup.ac`；每个站点的登录会话和比赛绑定分别保存。本项目 fork 自 [Minstdfx/qoj-submitter](https://github.com/Minstdfx/qoj-submitter)，不是 QOJ 官方工具。

## 安装

需要 Python 3.10 或更新版本、Git 和 [pipx](https://pipx.pypa.io/)。Ubuntu 可以这样安装：

```bash
sudo apt install git pipx
pipx ensurepath
```

根据 `pipx ensurepath` 的提示重新打开终端，然后从 GitHub 安装：

```bash
pipx install 'git+https://github.com/LittleDrinks/qoj-submitter.git'
```

更新到 GitHub 的最新版本：

```bash
pipx reinstall qoj-submitter-direct
```

安装后提供 `qoj` 和 `qoj-submit` 两个命令，不会替换系统已有的 `submit`。

## 快速开始

```bash
qoj login                 # 在本机输入用户名和密码
qoj contest 3387          # 绑定比赛，也可传比赛 URL
qoj status                # 查看当前站点、登录状态和绑定比赛
qoj samples               # 下载当前比赛样例到 ./samples.zip
qoj submit --dry-run A.cpp
qoj submit A.cpp
```

登录 Universal Cup 站点时显式指定地址：

```bash
qoj login --url https://contest.ucup.ac
```

`qoj login` 默认登录 `https://qoj.ac`，成功后切换当前站点。比赛绑定按站点独立保存；绑定比赛不会报名或启动虚拟比赛。

## 提交

`qoj submit` 和 `qoj-submit` 使用相同参数。默认从文件名推断题号和语言，例如 `A.cpp` 对应 A 题、C++20。

```bash
qoj-submit A.cpp
qoj-submit -p B -l C++23 solution.cpp
qoj-submit -c 3387 -p A -y A.cpp
```

`-c` 临时指定比赛，`-p` 指定题号，`-l` 指定题目支持的语言，`-y` 跳过确认。当前支持单文件提交；`--dry-run` 检查提交信息但不发送提交。

提交后如无法确认结果，请先在站点的提交列表检查，避免重复提交。

## 样例

```bash
qoj samples                     # 当前绑定比赛，写入当前目录
qoj samples -c 3575             # 临时指定比赛，不改变绑定
qoj samples -o ~/contest        # 指定已存在的输出目录
qoj samples --force             # 允许覆盖已有输出文件
```

`samples.zip` 中按 `samples-A/1.in`、`samples-A/1.ans` 的格式保存样例。遇到无答案的输入、交互题或其他特殊附件时，命令会输出警告，并将原始内容保存在 `attachment-extras.zip`；不会生成不存在的答案。无附件的题目会在命令输出中标明。已有压缩包默认不会被覆盖。

## 会话与安全

密码只用于交互式登录，不保存到配置文件。站点会话和配置保存在 `~/.local/share/qoj-submitter/`，若设置 `XDG_DATA_HOME` 则保存在其 `qoj-submitter/` 子目录。会话文件包含登录凭据，请勿分享。会话过期后重新登录。

安全验证或双因素认证可能需要在站点上处理；当前 CLI 不支持双因素认证。使用本工具时请遵守比赛和站点规则。

## 开发与帮助

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
qoj --help
```

问题和建议请提交至 [GitHub Issues](https://github.com/LittleDrinks/qoj-submitter/issues)。许可见 [LICENSE](LICENSE)。
