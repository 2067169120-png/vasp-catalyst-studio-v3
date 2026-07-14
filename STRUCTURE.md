# 目录结构 (STRUCTURE)

> 顶层布局速查。代码分区块的详细说明见 **[使用说明.md](使用说明.md)** 与 **[README.md](README.md)**。

## 顶层目录

| 路径 | 职责 |
|---|---|
| `vcstudio/` | 主 Python 包(全部业务逻辑,分区块见下) |
| `tests/` | pytest 测试套件(与 `vcstudio/` 模块一一对应,离线可跑) |
| `docs/` | 文档:`superpowers/`(SDD 规格/计划/报告)+ 工作日志 |
| `packaging/` | 打包脚本 `build_exe.py`(无参=web 入口,`--legacy`=旧 tkinter) |
| `tools/` | 独立辅助脚本(如 `generate_word_report.py` Word 报告导出) |
| `results/` | 作业/项目输出根(内容 gitignore,仅留 `.gitkeep` 占位) |
| `dist/` | 打包产物(gitignore):现役 `VASP Catalyst Studio.exe`(默认 Web)+ `…Legacy.exe`(旧 tkinter) |
| `.superpowers/sdd/` | SDD 工作区(gitignore):`progress.md` 跨会话生命线;中间产物已迁 `_待处理归档/` |
| `_待处理归档/` | 过程性文件暂存区,等用户定夺;README 有清单,内容不入 git,不删除 |

## 顶层文件

| 文件 | 职责 |
|---|---|
| `README.md` | 项目总览、架构、科学约定、快速开始 |
| `使用说明.md` | 完整用法 + 代码分区块地图 |
| `STRUCTURE.md` | 本文件,目录结构速查 |
| `pyproject.toml` | 包元数据 + 依赖 + CLI 入口(`vcs` / `vcs-gui`) |
| `config.yaml` | 示例/默认配置(用户敏感 `clusters.yaml` 另存,gitignore) |
| `重新打包EXE.bat` | 一键重打包 EXE(Windows) |
| `清理临时文件.bat` | 清理本地临时/缓存文件(Windows) |

## `vcstudio/` 包分区块

| 子包 | 职责 |
|---|---|
| `generate/` | 输入生成:POSCAR + 用户 INCAR → 四件套;缺项才补,绝不改用户键 |
| `cluster/` | 提交/监控/诊断/有界恢复:PBS·Slurm 双方言、preflight、批量操作、认领 |
| `project/` | 吸附能项目:批量组态生成、ΔE 门控、Li-S 放电路径分析 |
| `external/` | 出图与报告:Origin/SVG 图表、POV-Ray 结构图、LLM 双语分析 |
| `shared/` | 跨层基础:配置、清单(manifest)、凭据(keyring)、赝势/资源解析 |
| `gui/` | 旧版 tkinter 桌面 GUI(`--legacy` 打包保留) |
| `gui_web/` | 新版 pywebview Web GUI(默认入口):`api.py` 薄处理器 + `assets/` 离线前端 |
| `cli/` | 命令行入口 `vcs`(`gen` 等子命令) |

## 入口点

- **Web GUI(默认)**:`python -m vcstudio.gui_web` 或双击 `dist/VASP Catalyst Studio.exe`
- **旧版 GUI**:`python -m vcstudio.gui`(打包 `--legacy`)
- **CLI**:`vcs gen --poscar … --incar … -o results/job1/`
- **测试**:`python -m pytest`
