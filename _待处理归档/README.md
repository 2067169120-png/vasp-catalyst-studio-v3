# 待处理归档 (quarantine)

本目录存放**过程性 / 已完成使命的文件**——阶段性笔记、一次性完成报告、SDD 中间产物、旧打包产物等非长期资产。
移入此处只为清理根目录、让仓库分块清晰,**绝不删除**;等用户逐条定夺(保留 / 移回 / 删除)。

> 本目录内容默认**不入 git**(见同级 `.gitignore`,与 `.superpowers/sdd/.gitignore` 同理);
> 仅 `README.md` 与 `.gitignore` 入库记录去向。个别经 `git mv` 迁入的文件保留其 git 历史。

## 清单

| 原路径 | 为何判定为过程性 | 建议处理 |
|---|---|---|
| `docs/2026-07-05-夜间优化完成报告.md` → 本目录 | 某夜间自动优化的一次性完成报告,内容已并入 git 历史与后续文档,非长期维护文档(经 git mv,保留历史) | 确认无用后删除 |
| `继续开发-交接总结.md`(原根目录,未跟踪) | 2026-07-02 过期会话交接笔记,内容已全部落地;其 §5 的 `git add -A` 指令如今有害;当初已故意不入库 | 确认后删除 |
| `sdd-artifacts/task-*-brief.md`(共约 12 个) | SDD 各任务的下发 brief,一次性调度产物;活的跨会话生命线 `progress.md` 已留在 `.superpowers/sdd/` 原位 | 确认后删除 |
| `sdd-artifacts/task-*-report.md`(共约 12 个) | SDD 各任务的回执报告,一次性产物;信息已汇入 progress.md / git 历史 | 确认后删除 |
| `sdd-artifacts/review-*.diff`(共约 36 个) | 各次评审的 diff 快照,一次性中间产物;git 历史即真源 | 确认后删除 |
| `sdd-artifacts/final-review-branch.diff` | 分支终审 diff 快照,一次性中间产物 | 确认后删除 |
| `dist/VASP Catalyst Studio Web.exe` → 本目录 | P1a 旧打包产物(约 21.6 MB),已被新默认 `dist/VASP Catalyst Studio.exe` 取代;dist/ 本就 gitignore | 确认后删除(或按需回迁重测) |
| `_回收待删/`(原根目录,**空目录**) | 早前遗留的空占位目录,内含 0 文件;已被上一提交 f3f2f15 `rmdir` 清除(未删除任何文件,符合"绝不删文件") | 无需处理(已空,无内容可恢复) |

> `sdd-artifacts/` 全部来自 `.superpowers/sdd/`(该目录被 `*` 全量 gitignore),迁入前后均未入 git;
> `.superpowers/sdd/progress.md`(跨会话恢复生命线)**留在原位不动**。

## 约定

- 迁入用 `git mv`(已跟踪)或普通 `mv`(未跟踪/被忽略);被忽略的文件迁入后由本目录 `.gitignore` 继续忽略。
- 保护线:`vcstudio/`、`results/`、`tests/`、`docs/superpowers/`、`dist/` 两个现役 EXE、`.superpowers/sdd/progress.md` 均**不进本目录**。
- 如需恢复某文件,移回原路径即可(经 git mv 的文件有历史可查;被忽略的文件直接移回)。
