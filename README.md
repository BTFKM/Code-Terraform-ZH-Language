# Code Terraform 中文补丁

本补丁为《Code: Terraform》正式版（非 Demo）提供简体中文界面汉化。

本增强版基于 [BTFKM/Code-Terraform-ZH-Language](https://github.com/BTFKM/Code-Terraform-ZH-Language)
提交 `ff88365606765ba6bfb914c4b8af2b10d51367df`，新增逐文件持久缓存、全部 CPU 逻辑线程支持、
Brotli 元数据直接填充和线性时间译文注入。原项目的译文及许可说明保留。

> **现状说明**：译文字句来自机器翻译（经术语表约束与占位符校验），**未经人工逐条审校**，
> 可能存在误译、生硬或不一致；游戏内也可能存在字典之外的英文文本。
> 本补丁定位是"可用的中文汉化"，而非成品官方翻译——欢迎按下方方法自行修订润色，
> 欢迎提交 issue/PR 修正错译。

## 适用版本

- **正式版**：Steam App ID 868160（"Code: Terraform"），depot build 25164444（2025-09-12 版）
- 判定依据：`code-terraform.exe` 大小 `316,687,360` 字节（若 Steam 更新导致 exe 大小或资产结构变化，需重新应用补丁）

以上为原项目验证版本。本增强版另在本机 `319,938,560` 字节的游戏文件上完成 5 个字典宿主的
等长压缩和内容校验；对应 869 个资产、9036 条可注入译文、1 条占位符不合规而跳过的译文。
新版游戏可能仍有语言包未覆盖的文本；本次测试不等同于游戏内界面验收。

**不适用**：Demo 版（187MB）。Demo 版汉化内容虽 98% 相同，但字典宿主 bundle 结构不同（index.js vs 5 个副本），需单独打包。

---

## 快速使用

### 方法一：脚本一键应用（推荐）

假设你已克隆本仓库到本地：

```bash
cd Code-Terraform-ZH-Language
python -m pip install -r requirements.txt
python ctzh.py --workers 32
```

脚本会：
1. 自动找到 Steam 库中的 `code-terraform.exe`
2. 扫描当前版本的嵌入资产
3. 定位 5 个字典宿主 bundle（simWorker/editorAutocomplete/editorAnalysisWorker/persistenceCodecWorker/trainingExecutionWorker）
4. 注入 `language.json`（9045 条中文）
5. 并行求解，每个成功结果立即校验并写入磁盘缓存
6. 全部完成后做零位移重打包（备份原版到 `code-terraform.exe.bak`，大小一字节不差）

实测：2026-09-14，在本机 32 逻辑线程、Python 3.12.14、Brotli 1.2.0 环境下，
上述 5 个真实文件的首次求解共 **4.55 秒**，重新运行全部命中缓存时为 **0.03 秒**。
这是求解阶段的用时，不包含启动、资产扫描、译文注入和最后写入；不是与旧脚本的同条件速度比。
实际耗时随硬件、游戏文件和译文而变化。

### 并发、断点续跑与缓存

```bash
python ctzh.py --workers 32
python ctzh.py --workers 32 --cache-only
python ctzh.py --cache-dir "D:/ctzh-cache"
python ctzh.py --method search --workers 32
```

- `--workers` 省略时自动使用可用 CPU 逻辑线程数，已移除原来的 8 进程上限。可指定 1、8、16、32 等。
- 默认 `--method auto` 并行处理各个文件，依次尝试 quality 4、6、9、10、11，找到能装入目标尺寸的压缩结果即直接补齐。
  通常只有 5 个独立文件，因此这一阶段最多同时运行 5 个压缩任务，不需要持续占满 32 线程。
- 快速方式不能完成时，先验证旧版预计算参数，再回退传统搜索；每个搜索任务可用满设定的线程上限。
  Brotli 的 Python 原生绑定在压缩时释放 GIL，因此这里的线程能进行多核计算，并共享输入数据。
- `--method metadata` 只用快速填充；`--method search` 强制使用传统随机注释方式，便于兼容性对照。
- `--cache-only` 完成求解和缓存后退出。随后去掉此参数即可应用汉化。
- 每个成功文件立即存入 `solutions/v2/`，包括完整 `.br` 压缩结果和 JSON 校验信息。下次直接读取和解压校验，无需重新压缩。
- 缓存按实际注入后 JS 的 SHA-256 与目标长度区分；修改译文或游戏文件不会错误复用其他内容的结果。
  独立条目避免覆盖其他版本的缓存。原 `solutions/<exe大小>.json` 仍可作为传统求解提示使用。
- 每个缓存文件采用临时文件、`flush`、`fsync`、原子替换；完整压缩文件先落盘，索引后提交。
  损坏或缺失的条目会提示并重新计算。磁盘写入失败会报错，不会把未保存的结果宣布为成功。
- 中断或某个文件失败后，已完成的结果保留，重新运行相同命令即可继续。命中后停止投递搜索任务并取消排队任务；
  已进入原生压缩的任务会结束当前一次调用。全部资产成功前不会改写游戏 exe。

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

本方案让压缩后大小恰好等于原始 `data_len`，所有指针保持不变。

默认快速方式使用 [RFC 7932 第 9.2 节](https://www.rfc-editor.org/rfc/rfc7932.html#section-9.2)
规定的元数据块：先流式压缩翻译后的 JS，flush 到字节边界，再插入精确长度的元数据和结束块。
这些元数据不进入解压结果，因此不需要随机注释或密集试压缩。1、2 字节的小间隙也可以用空元数据块填满。
脚本检查编码器的结束块行为，并对最终长度和解压后的完整内容做严格校验；快速方式不适用时可回退。

传统方式仍使用 `翻译后 JS + 换行 /*随机字符 pad*/`，按 quality 11 → 10 → 9 搜索。
它修复了原父进程以空 `_JS` 估算压缩尺寸的问题，先压缩真实输入，再用插值缩小范围和局部密扫。
最多保留 `--workers` 个在途压缩任务，不再一次提交几百或几千个候选。
随机填充序列与原版一致，但只在传统路径需要时生成。

译文注入改为一次拼接片段，避免对大 bundle 做上千次整串复制；扫描阶段也减少了整个 exe 的内存副本。

### 依赖

- Python 3.10+
- `pip install brotli pefile`

测试：`python -m unittest -v`。测试涵盖元数据边界、内容校验、缓存损坏、强制退出后复用、
原子写入失败、逐项保存、旧参数迁移、线程上限和整体打包流程。

---

## 翻译来源（原项目说明）

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

**Q: 为什么默认方式没有把 CPU 一直跑满？**

A: 快速方式只需为每个文件进行少量压缩，补齐长度本身不需要搜索。5 个文件最多同时需要 5 个压缩任务；
省去多余计算可以更快完成。只有回退或强制传统搜索时，才会同时运行更多候选来使用设定的线程数。
