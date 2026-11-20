# Browndust2 Spine Viewer

一个用于浏览 **BrownDust2 (棕色尘埃2 / BD2)** 游戏 Spine 骨骼动画的预览器。

- **Python 启动器**（`BD2pineViewer.py`）：角色列表界面，扫描 mods 文件夹，选择角色/动画后启动预览。
- **Electron 预览器**（`SpineViewer-anosu/`）：基于 pixi.js + pixi-spine，负责加载 `.skel`/`.json` 骨骼、`.atlas` 图集、播放动画、导出 GIF/MP4。

---

## ✨ 关键更新：支持 Spine **4.2.43**

游戏更新后 Spine 版本升到 **4.2.43**，旧版预览器无法加载（`Unsupported version of spine model`）。本次修复为预览器内置运行时新增了 **Spine 4.2 二进制解析器**（`SkeletonBinary42`），并让 JSON 解析器兼容 4.2 字段：

- **二进制 `.skel`**：4.2 是完整的二进制格式重写（不是简单版本号变更）——头部新增 `referenceScale`，骨骼 `transformMode`→`inherit`，IK/Transform/Path 约束改为位标志打包，新增 **Physics 约束** 段，附件类型移入 flags 字节，时间轴新增 `InheritTimeline` 与 Physics 时间轴等。
- **JSON `.json`**：新增 `referenceScale`、骨骼 `inherit`、槽位 `visible`、`audio`（而非 `audioPath`）等字段，未知字段安全忽略。
- **向后兼容**：旧的 3.x / 4.0 / 4.1 文件仍能正常加载（未改动 3.8/4.0 解析器）。

### 验证

以下真实游戏文件已在本地通过解析 + 渲染全链路测试：

| 文件 | 类型 | 结果 |
|------|------|------|
| `char060301.skel`（艾瑞克-毁灭之星） | 4.2.43 二进制 | ✅ 解析 + 渲染，11 个动画，171 骨骼 |
| `char060302.skel`（艾瑞克-破剑士） | 4.2.43 二进制 | ✅ 解析，IK/Event 时间轴正常 |
| `char021291.skel`（联动-出云天花） | 3.x 旧格式 | ✅ 无回归 |
| `cutscene_char020102.json`（洛琪希-小魅魔） | 4.2.43 JSON | ✅ 解析，568 骨骼，5 个动画 |

---

## 📁 目录结构

```
browndust2-spine-viewer/
├── BD2pineViewer.py          # Python 启动器（角色列表界面）
├── BD2pineViewer.spec        # PyInstaller 打包配置
├── BD2pineViewer.bat         # Windows 一键启动脚本
├── requirements.txt          # Python 依赖
├── spine_viewer_settings.json # 示例配置
├── BD2 Characters - Characters.csv  # 角色/动画元数据
└── SpineViewer-anosu/        # Electron 预览器（源码）
    ├── main.js               # Electron 主进程
    ├── preload.js            # 预加载脚本
    ├── package.json          # 依赖与打包脚本
    ├── src/                  # 预览器源码（含修复后的 pixi-spine.js）
    │   ├── index.html
    │   ├── renderer.js
    │   ├── assets/js/pixi-spine.js   # ← 修复版（新增 SkeletonBinary42）
    │   ├── assets/js/pixi.min.js
    │   └── ...
```

---

## 🚀 使用方式

### 方式一：直接运行（已有打包版）
1. 解压到任意目录。
2. 双击 `BD2pineViewer.exe`。
3. 选择 mods 文件夹（或编辑 `spine_viewer_settings.json` 里的 `mods_folder`）。

### 方式二：从源码运行
```bash
# Python 启动器（需 Python 3.13 + PyQt6）
pip install -r requirements.txt
python BD2pineViewer.py

# Electron 预览器（需 Node.js）
cd SpineViewer-anosu
npm install
npm start
```

### 打包（Electron 预览器）
```bash
cd SpineViewer-anosu
npm install
npm run package-portable   # 生成 dist-portable/SpineViewer-win32-x64/
```

### 打包（Python 启动器）
```bash
pyinstaller BD2pineViewer.spec
```

---

## ⚙️ 配置

`spine_viewer_settings.json`：
```json
{
  "mods_folder": "E:/Neowiz/Browndust2/BrownDust2_10000002/BepInEx/plugins/BrownDustX/mods",
  "sort_mode": "Modified Time",
  "switch_whitelist": ["E:\\Neowiz\\...\\mods\\ZZip剧情立绘"]
}
```

- `mods_folder`：游戏 mods 根目录（`.skel`/`.json` + `.atlas` + `.png` 所在）。
- `sort_mode`：角色列表排序方式。
- `switch_whitelist`：允许一键切换的 mod 文件夹白名单。

---

## 📄 许可

- 本项目基于上游 [KXDE 的 browndust2-spine-viewer](https://github.com/kxdekxde/browndust2-spine-viewer)（Electron 预览器）。
- Spine 运行时基于 [Esoteric Software spine-runtimes](https://github.com/EsotericSoftware/spine-runtimes)。
- 请勿用于商业用途；游戏资源版权归 Neowiz 所有。

---

## 🙏 致谢

- 上游预览器作者：KXDE
- Spine 运行时：Esoteric Software
