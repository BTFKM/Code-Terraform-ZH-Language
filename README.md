# Code Terraform 中文补丁

本补丁为《Code: Terraform》正式版（非 Demo）提供简体中文界面汉化。

> **现状说明**：译文字句来自机器翻译（经术语表约束与占位符校验），**未经人工逐条审校**，
> 可能存在误译、生硬或不一致；游戏内也可能存在字典之外的英文文本。
> 本补丁定位是"可用的中文汉化"，而非成品官方翻译——欢迎按下方方法自行修订润色，
> 欢迎提交 issue/PR 修正错译。

## 适用版本

- **正式版**：Steam App ID 868160（"Code: Terraform"），depot build 25164444（2025-09-12 版）
- 判定依据：`code-terraform.exe` 大小 `316,687,360` 字节（若 Steam 更新导致 exe 大小或资产结构变化，需重新应用补丁）

**不适用**：Demo 版（187MB）。Demo 版汉化内容虽 98% 相同，但字典宿主 bundle 结构不同（index.js vs 5 个副本），需单独打包。

---

## 快速使用

### 方法一：脚本一键应用（推荐）

假设你已克隆本仓库到本地：

```bash
cd Code-Terraform-ZH-Language
python ctzh.py
```

脚本会：
1. 自动找到 Steam 库中的 `code-terraform.exe`
2. 提取 853 个资产
3. 定位 5 个字典宿主 bundle（simWorker/editorAutocomplete/editorAnalysisWorker/persistenceCodecWorker/trainingExecutionWorker）
4. 注入 `language.json`（9045 条中文）
5. 对 5 个 bundle 做零位移重打包（备份原版到 `code-terraform.exe.bak`，大小一字节不差）
6. 验证加载器与中文渲染

预计耗时：本仓库已附带预计算的重打包参数（`solutions/`，针对原版 exe + 原版 language.json），命中缓存时 **几分钟内完成**；若你改过 `language.json` 或游戏版本不同，需现场求解 brotli 填充长度，耗时约 **20~40 分钟**（纯本地 CPU 计算，无网络请求）。

### 方法二：手动编辑翻译后重新打包

如果你想自己润色某些句子，或补充官方新出的 DLC/更新内容：

1. 编辑 `language.json`（JSON 数组格式，每条 `{"id":<索引>,"key":"<路径>","zh":"<译文>"}`）
   - `id` 对应原英文字典叶子索引（从 `ctzh.py --extract-only` 生成的 `leaves.json` 获取）
   - `key` 是字典路径，如 `"menu.new_game"`、`"briefing.day001.m1"`
   - `zh` 为中文，空字符串表示不翻译该条
   - 占位符 `{xxx}` 必须保留，不能增删

2. 重新打包：
```bash
python ctzh.py --lang language.json --inplace
```

---

## 回滚

若需恢复原版：
```bash
cp code-terraform.exe.bak code-terraform.exe
```

---

## 技术细节（供想自己魔改的人）

### 字典定位

正式版 i18n 字典被构建工具复制进 5 个独立 worker bundle：
- `simWorker-B1ZsIn3s.js` (5.6MB)
- `editorAutocomplete-DeUm7vMA.js` (3.0MB)
- `editorAnalysisWorker-DLibwCgc.js` (5.3MB)
- `persistenceCodecWorker-BOgTcLvg.js` (3.1MB)
- `trainingExecutionWorker-DFont1Vm.js` (3.1MB)

5 份内容完全一致（键路径、英文原文、叶子索引），所以注入时按 key 替换，5 份同步更新。

### 零位移重打包

Tauri 把前端资产用 brotli 压缩后嵌入 PE 的 `.rdata` 节，资产表在重定位表里（指针 + 长度）。

若改变压缩后大小，需：
1. 更新资产表的 `data_len` 字段
2. 移动后续资产数据（导致指针全要改）
3. 更新 `.reloc` 重定位块
4. 重新签名 Authenticode（若有）

本方案：**用随机填充让压缩后大小恰好等于原始 `data_len`**，所有指针保持不变，零位移，PE 结构完好。

实现：修改后的 JS 内容 = `翻译后 JS` + `换行 /*随机字符 pad*/`（JS 注释对解析器无害，但 brotli 视为高熵数据）。求解 pad 长度 R，使 `brotli.compress(内容)` 长度 == 原始 `data_len`。

求解器：quality 11 → 10 → 9 逐级回退（不同质量抖动相位不同，总有一个能命中），括号搜索 + 密扫，单 bundle 5~15 分钟。

### 依赖

- Python 3.10+
- `pip install brotli pefile`

---

## 翻译来源

- 2378 条直接复用 Demo 版已审校译文（键路径 + 英文原文双重匹配）
- 6667 条增量机翻（qwen3.8-flash，带游戏背景提示词确保术语一致）
- 1 条超长文档（17891 字符）分段翻译拼接
- 占位符校验：100% 通过
- 游戏内中文渲染：已验证（菜单、会话选择、剧情对白、设置界面）

---

## 许可

本补丁仅供个人学习使用。游戏本体版权归开发者所有。

若你基于此补丁制作了修改版（如繁体转换、方言版），请：
1. 保留本 README 说明
2. 注明修改内容
3. 不要商用

---

## 常见问题

**Q: Steam 验证完整性会覆盖补丁吗？**  
A: 会。Steam > 库 > 右键游戏 > 属性 > 已安装文件 > 验证游戏文件的完整性 → 会还原原版。需重新跑脚本。

**Q: 游戏更新后怎么办？**  
A: 若 exe 大小/资产数量变化，字典结构可能改（新增条目、key 变化）。需：
1. `python ctzh.py --extract-only` 重新提取
2. 对比新 `leaves.json` 与旧 `language.json`，手动补充新增 key
3. 重新打包

**Q: 某些词翻译得不好，怎么改？**  
A: 方法二编辑 `language.json` 对应条目，改 `zh` 字段后重新打包。

**Q: 能不能只汉化 UI 不汉化剧情？**  
A: 可以。编辑 `language.json`，删除或清空剧情类 key（如 `"briefing.*"`, `"transmissions.*"`, `"endgame.*"`）的 `zh` 字段。

**Q: 支持 Mac/Linux 版吗？**  
A: 本补丁为 Windows x64 PE。Mac/Linux 的 Tauri 产物结构不同（Mach-O / ELF，资产表发现逻辑要改），需另写。

**Q: 为什么求解这么慢？**  
A: brotli quality 11 压缩 5MB 文本要 7~10 秒 CPU，求解一个资产要试几百次。已用括号法优化（先粗扫框定交叉区间，再密扫），比纯盲扫快 5~10 倍。
