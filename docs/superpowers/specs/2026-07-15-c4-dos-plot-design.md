# C4 DOS 出图 — 设计 (spec / 实施报告)

> 对齐 `2026-07-14-ui-redesign-pywebview-design.md` §C.4(收尾期,做不完不阻塞)。
> vasprun.xml → 出版风格总 DOS 图。**MVP 范围明确收窄**:总 DOS(含自旋分辨);
> PDOS/能带 → backlog(文件大解析重,spec 原文即此优先级)。

## 关键架构决策

- **零 matplotlib**:项目出图双引擎 = Origin(出版级)+ `project/charts.py` 纯 SVG
  (零依赖兜底,EXE 零增重)。DOS 图走 **charts.py 同款 SVG**——web GUI 天然渲染,
  且可直接落 .svg 文件(矢量,期刊可用)。不引入任何新依赖。
- vasprun.xml 可达几十 MB → **ElementTree.iterparse 流式**,只抓 `<dos>` 段
  (efermi + total 两自旋),读完 `</dos>` 即停,不解析后面的投影/波函数大段。

## 分层

- 解析 `vcstudio/project/dosparse.py`:`parse_vasprun_dos(fileobj) -> dict`
  `{'efermi':float, 'energies':[...], 'spin_up':[...], 'spin_down':[...]|None}`
  (file-like 输入,测试 io.StringIO 离线可测;缺 <dos> 段 → ValueError"该 vasprun.xml
  无 DOS 数据(需 static/DOS 计算)")。
- 渲染 `vcstudio/project/charts.py` 追加:`render_dos_svg(data, *, title) -> str`
  出版风格:x = E − E_F(eV),y = DOS(states/eV);自旋向下取负画镜像;
  E_F 处竖虚线 + 0 水平线;PUB_COLORS;`_nice_ticks` 复用。
- api `dos_view(job_dir) -> {'ok','svg','saved','warnings'|'error'}`:
  找 job_dir/vasprun.xml(缺→error 提示先拉回;拉回清单 FETCH_FILES 暂无 vasprun.xml,
  warnings 提示可手动拉/后续加入拉回选项——**不改 FETCH_FILES,超本期范围**);
  解析+渲染,同时落 `job_dir/dos.svg`(saved=路径;写失败只 warnings 不挡显示);注入 `dos_mod`。
- 前端 jobs.js「DOS」行内按钮 → modal 内嵌 SVG(自产内容,受控)+ 显示已存路径。

## 测试策略

TDD:手工最小 vasprun.xml 夹具(ISPIN=1 单自旋 / ISPIN=2 双自旋 / 无 dos 段 /
截断 XML)断言 efermi/序列/ValueError;render_dos_svg 断言 SVG 结构关键元素
(viewBox/两条 path/E_F 虚线);api happy+缺文件+写失败降级+注入。全量 pytest 全绿
(基线 401 passed 1 skipped)。前端真浏览器 harness 验 SVG 渲染。

## 约束(同 C1-C3)

零新依赖、零 CDN;api 薄+注入+异常兜底;UI 零 emoji 中文文案;方法学键只读;窄 pathspec。

## Backlog(明示不做)

PDOS(按元素/轨道投影)、能带(line-mode)、拉回清单加 vasprun.xml 选项、Origin 引擎版 DOS。
