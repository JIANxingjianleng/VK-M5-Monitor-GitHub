# 把 VK M5 Monitor V24 上传到 GitHub

本机现状（已检查）：

- 没有安装 `git` / `gh`；
- `github.com:443` TCP 能通，但 HTTPS TLS 握手失败（`schannel: SEC_E_NO_CREDENTIALS`），`curl`、`Invoke-WebRequest` 都连不上；
- 因此**无法从这台机器直接 push**，也没有你的 GitHub 凭据。

已经准备好的文件（在本目录）：

| 文件 | 用途 |
|---|---|
| `VK-M5-Monitor/` | 干净的源码仓库（README、LICENSE、requirements、build.ps1、vk_m5、docs） |
| `VK-M5-Monitor-source-v24.zip` | 源码压缩包，适合网页上传 |
| `VK_M5_v24.exe` | Release 附件，不要提交进 git 仓库 |
| `VK_M5_v24.exe.sha256` | 校验值 |

exe SHA256：`7A9F9C1FEB7E0240481B6980ABB31E3254AD03CF75E1AE0FD16FF8FB022024D0`

---

## 方案 A：GitHub Desktop（最省事，推荐）

1. 在能上网的电脑安装 **GitHub Desktop**，登录你的账号。
2. `File` → `Add local repository`，选择解压后的 `VK-M5-Monitor` 文件夹。
3. 如果提示不是 Git 仓库，点 `create a repository`：
   - Name：`VK-M5-Monitor`
   - 不要勾 “Initialize with README”（目录里已有 README）
   - Create repository
4. 右上角 `Publish repository`：
   - 取消勾选 `Keep this code private`（如果要公开）
   - Publish repository
5. 打开 `https://github.com/<你的用户名>/VK-M5-Monitor/releases`：
   - `Draft a new release`
   - Tag：`v24.0`
   - Title：`VK M5 Monitor v24.0`
   - 上传 `VK_M5_v24.exe`
   - 正文粘贴 `RELEASE_NOTES.md` 内容
   - `Publish release`

---

## 方案 B：网页上传（不装任何软件）

1. 打开 GitHub，右上角 `+` → `New repository`。
2. Repository name：`VK-M5-Monitor`；选择 `Public`；不要勾初始化 README；Create。
3. 解压 `VK-M5-Monitor-source-v24.zip`。
4. 进入新仓库，点 `Add file` → `Upload files`。
5. 把解压后 `VK-M5-Monitor` 文件夹**里面的内容**拖进去：
   - `README.md`、`LICENSE`、`requirements.txt`、`build.ps1`、`.gitignore`
   - `run_vk_m5.py`、`VK_M5_v24.spec`、`vk.ico`
   - `vk_m5/`、`docs/`
6. Commit changes。
7. 进入 `Releases` → `Draft a new release` → tag `v24.0` → 上传 `VK_M5_v24.exe` → 发布。

> 不要把 24 MB 的 exe 提交进源码仓库；放 Release 附件，下载更快，也不会让仓库历史变重。

---

## 方案 C：命令行（在已安装 Git 的电脑）

```powershell
cd VK-M5-Monitor
git init
git add .
git commit -m "VK M5 Monitor v24.0"
git branch -M main
git remote add origin https://github.com/<你的用户名>/VK-M5-Monitor.git
git push -u origin main
```

然后在网页创建 Release，上传 exe。

如果之后想更新代码：

```powershell
git add .
git commit -m "describe change"
git push
```

---

## 仓库建议设置

- Description：`Windows tray/overlay monitor for Valkyrie VK M5: 2.4G / Bluetooth / wired battery, DPI, three-mode auto detection`
- Topics：`windows` `hid` `mouse` `battery` `dpi` `bluetooth` `valkyrie` `vk-m5`
- 如果要公开，保留 MIT LICENSE；如果不想别人复用，删掉 LICENSE 并改为 private。
- 可以在 About 里勾选 Releases，下载入口更明显。

---

## 需要我直接推送？

需要同时满足：

1. 这台机器能正常访问 GitHub HTTPS（目前 TLS 失败）；
2. 安装 Git；
3. 你提供 GitHub 用户名和仓库名，并完成认证（推荐你自己运行 `gh auth login`，不要把 PAT 贴在聊天里）。

满足后我可以把本地仓库 `git init`、提交、加 remote、push，并帮你整理 Release 文案。
