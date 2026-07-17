用agent管理obsidian的开源项目有哪些 https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f 这类思想的，按照效果，成熟流行度排序

## 结论

截至 **2026 年 7 月 17 日**，真正符合 Karpathy 这份 gist 思路的项目并不等同于“Obsidian 里接一个 AI 聊天框”。

这里的核心模式是：

`不可变原始资料 raw/ → Agent 增量编译、合并和交叉引用 → 持久 Markdown Wiki → index/log/schema → query/lint/持续维护`

重点是让模型**维护一个会长期复利的外部知识制品**，而不只是每次提问时临时做 RAG。([Gist][1])

下面按我给出的综合权重排序：

- 实际知识库效果：45%
- 工程成熟度与可维护性：35%
- 社区流行度：20%

这是基于项目架构、功能、测试、发布频率和社区数据的综合判断，不是统一数据集上的实测 Benchmark。

---

# 严格同类项目综合排名

## 1. `Ar9av/obsidian-wiki`

**综合最推荐，最适合作为长期主仓库。**

它基本是 Karpathy 模式最均衡的工程化实现：有明确的 Agent 工作流、跨 Agent 配置、增量更新、来源管理、研究、维护、归档和重建机制；同时没有把整个系统做得过于绑定某一种 Agent。

当前约 **2.9k Stars、291 Forks、220 次提交**，并已经迭代了约 23 个版本；仓库中直接包含 Claude、Codex、Cursor、Kiro、Pi、Windsurf 等适配目录以及测试。([GitHub][2])

**优势**

- 对 Karpathy 原始结构还原度高。
- Agent 兼容面广，适合 Codex、Claude Code 混用。
- 有 CLI 和比较完整的生命周期管理。
- 适合已有 Obsidian Vault，而不只是创建一个 Demo Vault。
- 结构相对通用，方便你继续用 Git、Skill 和 Agent 扩展。

**不足**

- 自动化程度没有第二名那么激进。
- 仍然需要你设计自己的审阅、分支和定时维护策略。

**适合你：** 很高。你熟悉 Codex、Agent 和 Git，这个项目最适合作为基础框架，而不是直接接受别人高度定制的第二大脑体系。

---

## 2. `eugeniughelbur/obsidian-second-brain`

**开箱后功能和自动化效果最强。**

它将 Karpathy Wiki 扩展成了一套非常完整的 AI-first Second Brain：当前提供约 44 个命令，包含笔记重写、矛盾协调、综合文章、语义搜索、网页研究、代码仓库文档化以及定时 Agent 自动维护。

当前约 **3.3k Stars、401 Forks、267 次提交**，已发布多个正式版本，并适配 Claude Code、Codex、Gemini、OpenCode、Hermes 和 Pi。([GitHub][3])

**优势**

- 自动化最完整。
- 不只是资料编译，还覆盖日常捕获、研究、组织、复盘和定时维护。
- 多 Agent 支持好。
- 适合直接体验“Vault 会自己演化”的效果。

**不足**

- 命令和约定很多，系统表面积较大。
- 更像完整产品方法论，侵入性强。
- 自动重写和定时任务越多，越需要严格的 Git diff、审批和回滚机制。

**选择建议：** 想快速看到最明显效果选它；想自己控制架构和工作流，第一名更稳。

---

## 3. `breferrari/obsidian-mind`

**最成熟的“编码 Agent 持久工作记忆”方案。**

它不是纯通用百科编译器，而是将 Obsidian Vault 变成 Claude Code、Codex CLI 和 Gemini CLI 的持久记忆层。它的核心竞争力是会话生命周期 Hooks：启动时加载上下文，操作后校验，压缩前备份，会话结束时审计并写回知识。

当前约 **3.4k Stars、419 Forks、117 次提交**，社区规模在严格同类项目中最高的一档。([GitHub][4])

**优势**

- Agent 生命周期设计成熟。
- 对项目、决策、人员、工作记录和工程上下文支持很好。
- Git 跟踪和本地检索集成自然。
- 很适合“多个编码项目共享长期记忆”。

**不足**

- 更偏工程工作记忆和个人绩效管理。
- 不如前两名适合把书籍、论文和网页持续编译成通用概念 Wiki。
- Vault 结构和作者的工作管理理念较强。

**适合场景：** 用 Obsidian 管理长期研发项目、技术决策、Agent 操作记录和跨会话上下文。

---

## 4. `atomicstrata/llm-wiki-compiler`

**研究资料、论文和事实可追溯性最强。**

这是最接近“知识编译器”概念的实现之一。它会把原始资料编译成不同类型的 Wiki 页面，并强调段落或 claim 级来源引用、来源行号、两阶段生成以及混合检索和图扩展。

当前约 **1.8k Stars、168 Forks、136 次提交**，发布节奏活跃。([GitHub][5])

**优势**

- 来源追踪和引用设计优于大部分 Obsidian Agent 项目。
- 适合论文、技术标准、研究报告等高可信度场景。
- 更关注“这项结论来自哪里”，而不只是生成好看的概念笔记。
- 数据结构相对严格，适合后续做自动评估。

**不足**

- Obsidian 只是 Markdown 展示端，不是系统的主要交互界面。
- 对日常笔记、任务管理和 Agent 会话记忆支持较弱。
- 使用体验更像编译流水线。

**适合场景：** 科研笔记、技术调研仓库、论文知识库。若你准备让 Agent 管理长期科研资料，它可能比前三名更合适。

---

## 5. `kytmanov/synto`

**当前最值得关注的新一代本地知识编译器。**

Synto 是 `obsidian-llm-wiki-local` 的正式后继项目。它保留本地 LLM、增量编译、草稿审批和 Obsidian Markdown，同时新增：

- 稳定 concept/entity ID
- rename、merge、split、homonym 消歧
- PDF 和多种资料类型导入
- Agent-ready pack 导出
- MCP 原文读取工具
- 离线质量评估
- 编译 lineage
- A/B 模型比较

当前约 **201 Stars、113 次提交、13 个 Release**，最新版为 2026 年 7 月 15 日发布的 v0.6.3。([GitHub][6])

**优势**

- 在知识实体演化和可维护性上设计得最深入。
- 本地 Ollama 优先，也支持 OpenAI-compatible 接口。
- 有质量评估和 CI 接入能力。
- Wiki 可以进一步导出为不同 Agent 可消费的知识包。
- 对 PDF、论文、规范、API 文档等类型做了差异化分析。

**不足**

- 项目较新，社区验证仍不足。
- 架构还在快速变化。
- 当前版本号尚未进入 1.0。

**判断：** 单看产品能力，它未来可能进入前三；当前因社区规模和长期稳定性排第五。

---

## 6. `kytmanov/obsidian-llm-wiki-local`

**当前最成熟的纯本地实现，但已经进入维护模式。**

它完整实现了：

- Ollama 本地运行
- 原始笔记不可变
- 增量概念编译
- 草稿审阅和拒绝反馈
- 手工修改保护
- `query`、`synthesize`
- `lint`、`maintain`
- 自动 Git Commit 和 Undo
- 多语言生成

当前约 **772 Stars、122 Forks、62 次提交、19 个 Release**。但仓库明确声明已经进入维护模式，新功能转移至 Synto。([GitHub][7])

**优势**

- 本地化最成熟。
- 流程保守，生成内容先进入草稿。
- 有明确的人类审批和错误反馈闭环。
- 对手工编辑保护较好。
- 安装和命令相对简单。

**不足**

- 新用户现在更应直接考虑 Synto。
- 默认无向量检索的设计适合较小知识库；仓库说明传统 index 路由适合约百份来源规模。
- 后续主要只有 Bug 修复。

**选择建议：** 只在你看重当前稳定行为、不希望跟随新架构变化时选择。

---

## 7. `gowtham0992/link`

**跨 Agent 长期记忆最严谨的新方案。**

Link 把 Karpathy Wiki 和 Agent Memory 结合起来：

- `raw/` 保存资料
- `wiki/` 保存来源支持的知识页
- 显式“记住这个”进入可审批记忆
- CLI、MCP、Skill 和本地查看器共享同一份 Markdown
- 支持 Codex、Claude、Cursor、Kiro、Copilot 等
- 提供 `review_after` 和 `expires_at`，防止 Agent 长期信任已经过期的记忆

当前约 **104 Stars、15 Forks、653 次提交、6 个 Release**，最新版为 2026 年 7 月 10 日的 v1.6.0。([GitHub][8])

**优势**

- 跨 Agent 共享记忆的设计非常清楚。
- 所有记忆都可审阅、归档、遗忘和追溯。
- 有公开 Benchmark 和回归测试。
- 运行时严格本地化。
- 对时效性记忆处理得比其他项目好。

**不足**

- 它更接近 Agent Memory Layer，不是完全自动的 LLM Wiki Compiler。
- 记忆写入路径刻意避免使用 LLM，因此自动综合能力弱于前面的编译器。
- Obsidian 是兼容查看和编辑端，不是产品核心 UI。
- 社区规模仍小。

**适合场景：** 多个 Agent 共享你的个人偏好、项目决策和长期上下文，而不是主要用于编译大量读书资料。

---

## 8. `LiyuanW21/obsidian-wiki-system`

**中文友好、最轻量的 Skill/Vault 模板。**

这是直接受 Karpathy gist 启发的 Codex/OpenCode Skill，提供中英文 Vault 模板，将内容组织为：

`raw/inbox → sources → concepts/questions/projects/syntheses → index/log`

它支持让 Codex 或 OpenCode直接初始化 Vault，也可以给其他支持 Skill 目录的 Agent 使用。([GitHub][9])

但目前只有约 **8 Stars、7 次提交**，本质上仍是结构模板和操作说明，还不是完整的知识编译引擎。([GitHub][9])

**优势**

- 中文模板完善。
- 容易阅读和改造。
- 对 Codex/OpenCode 安装方式明确。
- 几乎没有额外运行时。

**不足**

- 缺少成熟编译器、状态数据库、来源追踪和自动评估。
- 自动化效果主要取决于调用它的 Agent。
- 项目极新，尚未形成社区。

**适合场景：** 参考它的目录和 Skill 写法，然后整合进自己的仓库，而不是直接把它当作完整产品。

---

# 不属于严格同类，但很值得组合使用

## `kepano/obsidian-skills`

这是目前最流行的 Obsidian Agent 基础能力包，约 **42.3k Stars**。它教 Agent 正确操作 Obsidian Markdown、Bases、JSON Canvas 和 Obsidian CLI，但**不负责知识库的长期编译策略**。([GitHub][10])

定位是：

> Obsidian 文件格式与工具能力层，而不是知识管理工作流层。

建议安装，但不能替代上述 Wiki 系统。

## `tobi/qmd`

约 **28k Stars、582 次提交**的本地 Markdown 搜索引擎，提供 BM25、向量检索、重排序、CLI 和 MCP。Karpathy 的 gist 也明确把它列为可选的本地搜索层。([GitHub][11])

定位是：

> 大型 Vault 的检索层，而不是写入、合并和维护层。

## `RAIT-09/obsidian-agent-client`

约 **2.3k Stars**，通过 ACP 把 Claude Code、Codex、Gemini 等 Agent 直接接进 Obsidian，提供会话、终端、笔记引用等交互能力。([GitHub][12])

定位是：

> Agent UI/通信桥，不是知识编译系统。

---

# 对你的推荐

考虑到你熟悉 Codex、Claude Code、Agent、Git，不建议采用“安装一个大而全插件，然后允许它直接修改整个 Vault”的方式。

最合理的第一版组合是：

```text
Obsidian Vault Git 仓库
├── Ar9av/obsidian-wiki       # 核心知识生命周期
├── kepano/obsidian-skills    # Obsidian 格式与 CLI 能力
├── qmd                       # 大规模本地搜索
├── AGENTS.md / CLAUDE.md     # 仓库约束
└── Agent 写入分支 + diff/review + merge
```

选择逻辑：

- **综合长期使用：** `Ar9av/obsidian-wiki`
- **追求最高自动化效果：** `obsidian-second-brain`
- **研发项目与跨会话工作记忆：** `obsidian-mind`
- **论文、研究、技术资料：** `llm-wiki-compiler`
- **本地 Ollama、知识包和 MCP：** `Synto`
- **多个 Agent 共享可信记忆：** `Link`

基于你的能力，我会选择 **`Ar9av/obsidian-wiki + kepano/obsidian-skills + qmd`**，然后吸收 `Synto` 的实体 ID、来源 lineage、离线 eval 和草稿审批机制，而不是直接依赖某个高度意见化的 Second Brain 模板。

需要我设成每月自动检查这些仓库的 Release、维护活跃度和新增竞品，并更新这份排名吗？

[1]: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f "llm-wiki · GitHub"
[2]: https://github.com/Ar9av/obsidian-wiki "GitHub - Ar9av/obsidian-wiki: Framework for AI agents to build and maintain a digital brain through Obsidian wiki using Karpathy's LLM Wiki pattern · GitHub"
[3]: https://github.com/eugeniughelbur/obsidian-second-brain "GitHub - eugeniughelbur/obsidian-second-brain: Cross-CLI skill for Obsidian: turn your vault into a living AI-first second brain across Claude Code, Codex, Gemini, OpenCode, Hermes, and Pi. 44 commands - self-rewriting notes, local+hybrid semantic search, key-less web research, /obsidian-architect codebase docs, and scheduled agents that maintain the vault while you sleep. · GitHub"
[4]: https://github.com/breferrari/obsidian-mind "GitHub - breferrari/obsidian-mind: An Obsidian vault that gives AI coding agents persistent memory. Claude Code, Codex CLI, Gemini CLI. · GitHub"
[5]: https://github.com/atomicstrata/llm-wiki-compiler "GitHub - atomicstrata/llm-wiki-compiler: The knowledge compiler. Raw sources in, interlinked wiki out. Inspired by Karpathy's LLM Wiki pattern. · GitHub"
[6]: https://github.com/kytmanov/synto "GitHub - kytmanov/synto: More than just Karpathy’s LLM Wiki, 100% local with Ollama. Drop Markdown notes → AI extracts concepts → your Obsidian wiki auto-links and grows. Zero sharing. Your notes stay yours. · GitHub"
[7]: https://github.com/kytmanov/obsidian-llm-wiki-local "GitHub - kytmanov/obsidian-llm-wiki-local: Karpathy’s LLM Wiki, 100% local with Ollama. Drop Markdown notes → AI extracts concepts → your Obsidian wiki auto-links and grows. Zero sharing. Your notes stay yours. · GitHub"
[8]: https://github.com/gowtham0992/link "GitHub - gowtham0992/link: Local personal memory for LLM agents · GitHub"
[9]: https://github.com/LiyuanW21/obsidian-wiki-system "GitHub - LiyuanW21/obsidian-wiki-system: A portable Obsidian wiki system for Codex, OpenCode, and other skill-capable agents. It helps users turn raw notes, sources, questions, and projects into an iterative, linked personal knowledge base inspired by Karpathy’s llm-wiki. · GitHub"
[10]: https://github.com/kepano/obsidian-skills "GitHub - kepano/obsidian-skills: Agent skills for Obsidian. Teach your agent to use Obsidian CLI and open formats including Markdown, Bases, JSON Canvas. · GitHub"
[11]: https://github.com/tobi/qmd "GitHub - tobi/qmd: mini cli search engine for your docs, knowledge bases, meeting notes, whatever. Tracking current sota approaches while being all local · GitHub"
[12]: https://github.com/RAIT-09/obsidian-agent-client "GitHub - RAIT-09/obsidian-agent-client: Bring AI agents into Obsidian via Agent Client Protocol (ACP), such as Claude Code, Codex and Gemini CLI. · GitHub"
