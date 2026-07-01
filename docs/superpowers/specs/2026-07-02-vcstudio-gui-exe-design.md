# VASP Catalyst Studio 桌面 GUI（EXE）设计

- 日期：2026-07-02
- 状态：已与用户确认，待写实现计划
- 目标里程碑：M1（生成）图形化 + M2（集群）连接配置前置

## 1. 目标与动机

把现有 `vcs gen` 命令行封装成一个**面向计算化学大众用户**的 Windows 桌面程序，打包为**自包含单文件 EXE**（内置 Python，双击即用、无需装 Python）。用户在图形界面里：

1. **方便地配置信息**：一次性设好本地赝势库路径（POTCAR lib），自动记住；配置集群连接方式（SSH）。
2. **一键开跑**：界面里每次浏览选择 POSCAR / INCAR / 输出目录，点一个按钮生成 VASP 输入四件套。
3. **点击查看文件位置**：一个按钮直接在资源管理器里打开输出目录。

核心原则延续 M1：**尊重用户 INCAR，绝不强改方法学**。GUI 只是壳，生成逻辑一律复用已测过的 `build_job_dir()`，**不重写任何科学逻辑**。

## 2. 目标用户与约束

- 目标用户：做 DFT/催化计算的研究者，Windows 环境，**可能没有 Python**。
- 因此必须：自包含 EXE（PyInstaller 冻结）、双击即用、中文界面、错误友好（绝不弹 Python traceback）、跨会话记住配置。
- 明确不做：赝势库不打进 EXE（VASP 版权 + 体积 + 每人一份），用户仍需指向自己的 PAW_PBE 库。

## 3. 技术选型（已确认）

| 决策 | 选择 | 理由 |
|---|---|---|
| 界面库 | **Tkinter（标准库）** | 冻结体积最小、无授权问题、最稳；PyQt 大 30MB+ 且有授权顾虑。 |
| 打包 | **PyInstaller `--onefile --windowed`** | 单个 `.exe`，发给别人不丢文件；首次启动慢 1-2 秒可接受。 |
| 瘦身 | **去掉 numpy** | 全项目只用 1 行 `np.linalg.norm`（`kpoints.py`），换成标准库 `math.sqrt`，冻结少 ~30MB。 |
| SSH | **paramiko** | 测连接需要；也是 M2 计划用的 DPDispatcher 的底层。 |
| 密码存储 | **keyring（Windows 凭据库）** | 绝不明文存密码；凭据库不可用则退化为每次现输。 |

## 4. 整体形态：两个标签页

单窗口 + 顶部 `ttk.Notebook` 两页：**「生成」** 与 **「集群」**。

### 4.1 「生成」页（M1 图形化）

字段与控件：

- **赝势库 POTCAR**（全局）：文本框 + [浏览…]（选目录）。启动时从 config 读入回填；变更时自动写回 config，跨会话记住。
- **POSCAR 结构**：文本框 + [浏览…]（选文件，每次可不同）。
- **INCAR 参数**：文本框 + [浏览…]（选文件，每次可不同）。
- **计算类型**：单选 `molecule / slab / bulk`（默认 slab）。
- **KPOINTS**：文本框，默认「自动」；填 `5 5 1` 则手动指定（解析成 3 个整数，格式错就地报错）。
- **输出目录**：文本框 + [浏览…]。
- **INCAR 校验补全**：复选框，默认勾选（对应 CLI 的 `validate`，取消 = `--no-validate`）。
- **[▶ 一键生成]**：后台线程调 `build_job_dir(...)`，界面不冻；运行时按钮禁用。
- **[📂 打开输出文件夹]**：`os.startfile(out_dir)`（Windows）；生成成功后启用。
- **运行日志**：只读多行文本框，实时打印 `已生成 / 元素 / KPOINTS / 警告 / 错误`（复用 CLI 中文文案）。

### 4.2 「集群」页（M2 连接配置前置）

**本版范围：连接 + 测连接 + 调度器类型。** 提交任务与资源参数（节点/核数/墙钟/队列/VASP 命令）推迟到 M2。

字段与控件：

- **集群配置**：下拉选择已存 profile + [新建] [删除]（支持多集群，如「北京超算」「组里小集群」）。
- **主机名 / 端口 / 用户名**。
- **认证**：单选 `SSH 密钥 / 密码`（两者都支持）。
  - 密钥：私钥文件路径文本框 + [浏览…]（只存路径）。
  - 密码：密码框；**不写进 yaml**，存 keyring 或每次现输。
- **跳板机（可选）**：host / user / port（有些集群要先过登录节点）。
- **远程工作目录** `remote_root`。
- **调度器**：下拉 `Slurm / PBS(Torque) / LSF / Shell`。
- **[🔌 测试连接]**：后台线程用 paramiko 真连 → 回显 `whoami` + 探测调度器；连通才算配好。
- **[📤 提交任务]**：本版**禁用（置灰）**，标注「M2 上线」。
- **连接日志**：只读文本框。

## 5. 架构与数据流

### 5.1 模块结构

```
vcstudio/
  gui/
    __init__.py
    app.py            # Tk root + ttk.Notebook 两页；main() 入口
    generate_tab.py   # 生成页：控件 + 事件
    cluster_tab.py    # 集群页：控件 + 事件
    runner.py         # 后台线程包装（build_job_dir / ssh_test），经 queue 回传结果，主线程轮询更新 UI
    widgets.py        # 复用小组件：文件选择行、只读日志框
  cluster/
    __init__.py
    profiles.py       # clusters.yaml 读写 + ClusterProfile 数据模型（密码除外）
    ssh_test.py       # paramiko 连接测试：连接 + whoami + 调度器探测
  shared/
    config.py         # 增：save_config()/set_potcar_lib_root() + 冻结版可写路径解析
    secrets.py        # keyring 封装：存/取/删 密码，缺库时降级（返回 None → 触发现输）
  cli/main.py         # 增：可选 `vcs gui` 子命令启动界面（复用 app.main）
packaging/
  vcstudio.spec       # PyInstaller 配方：onefile/windowed/excludes/名称
  build_exe.py        # 构建脚本：调 PyInstaller，输出 dist/ 下 EXE
```

入口：`pyproject.toml` 增 `vcs-gui = "vcstudio.gui.app:main"`；亦支持 `python -m vcstudio.gui`。

**隔离原则**：`gui/` 只做界面与事件编排；生成逻辑在 `generate/`（不动），集群逻辑在 `cluster/`。每个单元职责单一、可脱离界面单测。

### 5.2 生成页数据流

1. 启动：`load_config()` → 回填赝势库路径。
2. 用户改赝势库 → `save_config()` 持久化。
3. 点[一键生成] → 预检（POSCAR/INCAR 存在？赝势库已填？KPOINTS 格式对？）→ 起后台线程 → `build_job_dir(poscar, incar, out, calc_type=, kpoints=, validate=, lib_root=)`。
4. 成功：把返回的 `warnings` 与 `已生成/元素/KPOINTS` 打进日志，启用[打开输出文件夹]。
5. 失败：捕获 `ValueError/PotcarError/OSError`，把中文错误信息红色打进日志，**不弹 traceback**。

### 5.3 集群页数据流

1. 启动：读 `clusters.yaml` → 填 profile 下拉。
2. 选 profile → 回填字段（密码从 keyring 取或留空）。
3. 保存 → 写 `clusters.yaml`（**不含密码**）；密码存 keyring（键 = profile 名）。
4. 点[测试连接] → 后台线程 `ssh_test.connect(profile, secret)`（secret 来自 keyring 或现输）→ 回传 `whoami` + 调度器 → 日志。

### 5.4 配置与密钥存储位置

- **冻结（EXE）时**：config.yaml 的搜索/写入位置增加 `%APPDATA%/vcstudio/config.yaml`（EXE 常在只读目录，不能就地写）。`config.py` 增写入与冻结感知路径解析（`getattr(sys, 'frozen', False)`）。
- **源码运行时**：沿用现有优先级（env `VCSTUDIO_CONFIG` > cwd > 包旁），写入落到 cwd/包旁或 `%APPDATA%`。
- `clusters.yaml`：`%APPDATA%/vcstudio/clusters.yaml`，每台机器本地。
- **安全**：
  - SSH 密钥：只存**私钥文件路径**，绝不存密钥内容。
  - 密码：**绝不明文写 yaml**；存 Windows 凭据库（`keyring`）；不可用则每次连接现输（仅内存）。
  - `known_hosts`：首次遇未知主机指纹**提示用户确认信任**后再存（`%APPDATA%/vcstudio/known_hosts`），不静默 AutoAdd（防 MITM）。
  - `config.yaml`（若落在项目内）、`clusters.yaml`、`known_hosts` 一律 **gitignore**。

## 6. 新增依赖

- `paramiko`（SSH 测连接）
- `keyring`（密码加密存储）
- **移除** `numpy`（改 `kpoints.py` 一行为标准库）

`pyproject.toml` 的 `dependencies` 相应更新。冻结后单 EXE 预计 ~25-30MB。

## 7. 打包与清理

- `packaging/vcstudio.spec`：`--onefile --windowed`，`excludes` 掉 sklearn/PIL/matplotlib/tests 等无关重包，指定 EXE 名 `VASP Catalyst Studio`。
- 产物：`dist/VASP Catalyst Studio.exe`，单文件，双击即用。
- **删除仓库里残留的 312MB `_internal/`**（旧的、不完整的 PyInstaller 产物，且未含 vcstudio）。
- `.gitignore` 增：`_internal/`、`build/`、`dist/`、`clusters.yaml`、`known_hosts`。
- `packaging/vcstudio.spec` 与 `packaging/build_exe.py` **保留入 git**（是构建配方，不忽略）。

## 8. 错误处理

- 复用 CLI 已有的干净中文报错（`ENMAX 超过 ENCUT`、`元素未登记`、`ENCUT 非数字`、`POSCAR 缺元素符号行` 等），原样显示在日志区。
- 生成/连接前做友好预检，不合格就地提示，不进核心。
- 后台线程内所有异常都捕获并转成日志消息，**界面永不崩溃、永不弹 traceback**。

## 9. 测试策略

- **config**：`save_config` → `load_config` 往返；冻结路径解析（monkeypatch `sys.frozen`）。
- **secrets**：keyring 存/取/删往返（或 mock）；缺库降级返回 None。
- **profiles**：`clusters.yaml` 读写往返；断言 yaml 里**不含密码**。
- **ssh_test**：mock `paramiko.SSHClient`，断言用正确参数连接、`whoami` 解析、调度器探测；一个可选真连集成测试用环境变量门控。
- **runner（生成）**：用冒烟测试那 5 个用例（3 正：molecule/bulk-Fe/slab-Pt；2 负：ENCUT 过低、元素未登记）跑，断言 queue 回传的警告/报错——无需开窗口。
- **纯函数**：KPOINTS 解析 `"5 5 1"→[5,5,1]`、字段校验等抽成纯函数，脱离 Tk 单测。
- **手动验收**：真打出 EXE → 双击 → 生成页跑 5 个用例 + 集群页测连接（需一台真集群或本地 sshd）→ 确认日志与打开文件夹。

## 10. 已知前提与坑（需向用户说明）

1. **赝势库不打进 EXE**：用户仍需自有 PAW_PBE 库，GUI 只是让「指向它」变方便。
2. **未签名 EXE**：Windows 首次弹 SmartScreen「未知发布者」，点「仍要运行」即可；代码签名需买证书，暂不做。
3. **onefile 首次启动慢**：自解压到临时目录，约 1-2 秒。
4. **测连接需要网络可达**：跳板机/校园网/VPN 未通时会失败，日志给出原因。

## 11. 本版明确不做（推迟到 M2）

- 集群「提交任务」（按钮置灰）。
- 资源参数：节点数 / 每节点核数 / 墙钟 / 队列 / VASP 执行命令 / module 加载。
- 作业查询 / 回收 / 监控（M2-M3）。
- LLM 分析出图（M4）。

## 12. 验收标准

- 双击 `dist/VASP Catalyst Studio.exe` 打开两页界面。
- 生成页：设一次赝势库并被记住；浏览选 POSCAR/INCAR/输出，点[一键生成]产出正确四件套；[打开输出文件夹]能打开；错误友好显示。
- 集群页：新建/保存/切换多个集群；密码不落 yaml；[测试连接]对真集群能连上并回显 whoami + 调度器；[提交任务]置灰标注 M2。
- 仓库无 312MB `_internal/`；构建脚本可复现出 EXE。
