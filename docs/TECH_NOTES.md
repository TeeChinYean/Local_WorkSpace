# 技术储备与前沿演进备忘 (Tech Notes & Future Roadmap)

> 本文档用于归档和记录可用于本项目（4GB 显存 / Windows 原生 / 本地 RAG & IDE）的技术调研、低资源微调方案及推理加速方案，供后续扩展备用。

---

## 目录
1. [4GB 显卡微调 8B 大模型：Soup (Layer Streaming)](#一4gb-显卡微调-8b-大模型soup-layer-streaming)
2. [Prompt-Lookup Decoding (PLD 零显存无损投机解码)](#二prompt-lookup-decoding-pld-零显存无损投机解码)
3. [商业化与私有化落地模式备忘 (Monetization)](#三商业化与私有化落地模式备忘-monetization)

---

## 一、4GB 显卡微调 8B 大模型：Soup (Layer Streaming)

* **开源项目**：[Soup (GitHub)](https://github.com/MakazhanAlpamys/Soup)
* **核心突破**：利用 **Layer Streaming（层流式传输）** 突破显存墙，在 4GB 笔记本 GPU（如 RTX 3050）上完成 **Llama 3.1 8B / Qwen 8B** 等 8B 参数模型的 LoRA SFT 微调。

### 1. 核心技术原理
传统微调需要将全部基座模型权重、梯度与优化器状态放入显存（即使 4-bit 量化也需要 5.5GB+ 显存）。  
Soup 将**冻结的 8B 基座模型存放在物理内存（Host RAM）中**，显存中只保留：
* 常驻层：Embedding 层 + LM Head 输出层（约 2.1GB）
* 双缓冲池：每次仅将 1 层解码器送入显存计算，并提前预取下一层（2 × 113MB ≈ 0.23GB）
* 正在训练的 LoRA 权重与激活值（约 1.0GB）
* **峰值显存锁定在约 3.32GB**，完全不超出 4GB 物理显存限制。

### 2. 硬件与环境要求
* **显存 (VRAM)**：≥ 4GB (如 RTX 3050 Laptop / 4GB 桌面卡)。
* **物理内存 (RAM)**：建议 **≥ 16GB（推荐 32GB）**（基座模型需锁页常驻内存）。
* **Python 版本**：Python 3.10 ~ 3.12（推荐在 WSL2 或独立虚拟环境中运行）。

### 3. 操作流程备忘

#### 步骤 1：安装依赖
```bash
pip install "soup-cli[train]"
```

#### 步骤 2：初始化配置 `soup.yaml`
```yaml
base: meta-llama/Llama-3.1-8B-Instruct
task: sft
data:
  train: ./data/train.jsonl
  format: alpaca
  val_split: 0.1
training:
  stream_layers: true     # 关键配置：开启层流式加载
  epochs: 3
  lr: 2e-5
  batch_size: auto
  lora:
    r: 64
    alpha: 16
  quantization: 4bit
output: ./output
```

#### 步骤 3：启动微调
```bash
soup train --config soup.yaml
```

#### 步骤 4：融合导出为 GGUF（直接供给本项目使用）
```bash
# 验证对话
soup chat --model ./output

# 导出为标准 GGUF Q4_K_M 格式
soup export --model ./output --format gguf --quant q4_k_m
```

### 4. 与本项目的联动闭环
1. **数据生产**：在当前项目的 Web IDE / RAG 对话中积累专业领域、特定代码规范或格式问答，导出为 `train.jsonl`。
2. **本地微调**：在本地运行 Soup 完成 8B 模型的轻量微调。
3. **即插即用**：将导出的 `.gguf` 放入本项目的 `models/` 目录，并在 `llm_models.json` 注册，即可在 Web UI / Web IDE 中直接作为主力模型运行。

---

## 二、Prompt-Lookup Decoding (PLD 零显存无损投机解码)

* **技术定位**：推理加速（Inference Acceleration）。
* **核心优势**：
  * **0 MB 额外显存**：无需加载额外的草稿小模型（Draft Model）。
  * **数学级等价保真**：采用大模型“一票否决”前向验证机制，**输出结果与正常逐字推理 100% 完全一致（Bit-for-bit identical）**，绝不引发额外幻觉或结果偏离。

### 1. 适用场景与收益
* **高命中场景（提速 1.5x ~ 2.2x）**：
  * **代码重构与补全**：生成的代码片段大量复用已有代码库和 Prompt 提示。
  * **RAG 知识检索问答**：回答大量引用检索切片与文档原文。
  * **结构化数据生成**：如严格输出固定 Schema 的 JSON / Markdown 报表。
* **低命中场景（速度持平）**：
  * 纯凭空自由创作或闲聊，验证失败后自动退化为正常逐字单步输出，无负面副作用。

### 2. llama.cpp 启用方式备忘
llama-server 原生支持 n-gram lookup 投机解码。在 `llm_models.json` 的 `extra_args` 中追加参数即可启用：
```json
"--draft-max", "16"
```
（或配合 `--lookup` 相关策略参数使用）。

---

## 三、商业化与私有化落地模式备忘 (Monetization)

本项目具备 **“纯 Windows 原生、完全不依赖 Docker、4GB 极低硬件成本、自带安全沙盒与 Web IDE”** 的特点，具备以下商业变现路径：

| 模式 | 目标客户 | 交付形态 | 商业逻辑 |
| :--- | :--- | :--- | :--- |
| **1. 离线合规知识库系统** | 律师事务所、财税公司、涉密外包研发 | Windows 一键安装程序 (MSI/Inno Setup) | 数据绝对不出内网，单套买断费 + 每年维保升级。 |
| **2. 软硬件一体机** | 个人创作者、中小微工作室 | 预装系统的低成本 Mini PC (RTX 3050/大内存核显) | 开箱即用，免去用户配环境、下载模型的门槛。 |
| **3. 团队 API 网关与座席授权** | 团队开发协同 | 局域网接入点（兼容 OpenAI 规范） | 供团队的 Cursor / Continue 使用，按座席或 Token 计量授权。 |
| **4. 垂直 Agent 工具技能包** | 特定行业从业者 | MCP 插件市场 / 专有技能包 | 提供定制数据源解析（如 CAD/图纸/私有格式解析工具）。 |
