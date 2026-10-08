# FileTools v0.1.0

本地部署的文件格式转换工具。所有转换都在你自己的机器上跑，文件不上传、不出内网。

## 这个版本装了哪些平台

| 平台 | 这一版给了什么 | 怎么装 |
|---|---|---|
| Web | 静态站点（`FileTools-Web-0.1.0.zip`） | 解压后**挂在服务器根路径**（见下方说明）；或直接由后端 `frontend/dist` 托管 |
| Windows | NSIS 安装包（`FileTools-Setup-x64.exe`）×64 位 | 双击装，装到当前用户目录、免 UAC |
| Android | APK（`FileTools-0.1.0-release.apk`） | 侧载安装，需在系统设置里允许「安装未知来源应用」 |
| iOS | **这一版没有** | 见下方「已知限制」 |

**Web 那份要挂在服务器根路径。** 打包时用的是站点根（产物里的引用形如 `/assets/…`，
不是 `./assets/…`），所以解压后要放到服务器的根目录、或配一条把 `/` 指过去的规则；
放在子目录（比如 `https://example.com/filetools/`）会**白屏**，因为浏览器会去
`https://example.com/assets/…` 找那些文件。
（桌面版走的是另一条构建路径，产物里的引用是相对的，和这一份互不影响。）

## 用之前要知道的

- **Web 版和 Windows 桌面版都需要后端在 `127.0.0.1:8000`。** 桌面版的地址是**构建时烤进去的**，
  换服务器得重新构建。后端怎么起见仓库 README。
- **Android 版可以直接连你指定的后端地址**（不烤死），首次启动时填。

## 已知限制（如实列出，不藏）

1. **Android 那份 APK 是 debug keystore 签名的。** 它能正常安装、正常使用，但**不能上架应用商店**。
   这一版把它当「可侧载的测试包」发，不当正式商店包。正式签名需要一份 release keystore，
   而 keystore 属于你的私钥、不该由构建流程生成后随便躺着。
2. **Windows 只有 x64，没有 ARM64。** 构建机只装了 `x86_64-pc-windows-msvc` 这一个 target，
   没有 ARM64 产物 —— 与其发一个没验过的 ARM 包，不如不发。
3. **iOS 没有任何产物。** 构建机上没有 macOS，一行都编不了。配置与图标资产是齐的，
   在一台 Mac 上跑一次 `expo prebuild` + Xcode 就能接上。
4. **没有 Docker 镜像。** 构建机上没有 docker，本轮未构建。
5. **Windows 桌面版未启用 CSP**（`csp: null`）。原因是本版用 `WebView2` 的远程调试端口
   实测打不开，CSP 只验到一半，与其开一个验不全的策略不如先不开，等能验了再开。
6. **Windows 桌面版的拖拽只在文件选择区内生效**，不是窗口任意位置。

## 校验下载的文件

每个产物旁边都有 `SHA256SUMS.txt`。在下载目录里跑：

```bash
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell 里：

```powershell
Get-FileHash .\FileTools-Setup-x64.exe -Algorithm SHA256
```

逐个和 `SHA256SUMS.txt` 里的值对。对不上就别装。

## 这次的产物是从哪个提交出来的

`RELEASE-MANIFEST.json` 里记了完整的 commit、构建时间、每个文件的字节数与 sha256，
以及各平台**签名状态**（`signed` 字段）。那份清单和 `SHA256SUMS.txt` 是同一批事实的两种写法，
`python scripts/verify_release.py` 会断言两者一致。

更完整的功能说明、环境要求与开发方式见仓库根目录的 `README.md`。
