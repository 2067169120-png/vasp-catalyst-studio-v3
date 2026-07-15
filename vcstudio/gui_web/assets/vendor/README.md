# 第三方前端资源 (vendor)

本目录存放**离线打包**的第三方前端库。运行时零 CDN;仅在此以本地文件引入。
如需升级,构建期重新下载并更新下表(记来源 URL / 版本 / license / SHA256)。

| 文件 | 库 | 版本 | License | 来源 | SHA256 |
|---|---|---|---|---|---|
| `echarts.min.js` | Apache ECharts | 5.5.1 | Apache-2.0 | https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js | `e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf` |
| `3Dmol-min.js` | 3Dmol.js | 2.4.2 | BSD-3-Clause | https://cdn.jsdelivr.net/npm/3dmol@2.4.2/build/3Dmol-min.js | `06b6d2fc7d418e8bef62a32cca44373ff21b2b787bd377613dd4039394a4e9ff` |

## 用途

- `echarts.min.js`:C1 收敛过程可视化(E0/ΔE/|F|max vs 离子步曲线,`converge.js`)。
- `3Dmol-min.js`:C2 结构 3D 预览(POSCAR/CONTCAR 球棍模型 + 间隙标注,`structure.js`)。

## 打包

`packaging/build_exe.py` 的 web 分支以 `--add-data <assets> vcstudio_assets` **递归**带入整个
`assets/` 目录树,`vendor/` 一并打入 exe,无需额外配置。
