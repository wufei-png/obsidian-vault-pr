# 让 Agent 整理 Obsidian，也得留下改动依据

把文章链接、微信推文、PDF 或登录后的网页交给 Agent，最省事的做法是让它直接改笔记。问题也出在这里：它改了什么？引用的是哪段原文？合并笔记时有没有吞掉冲突？进程中断后，写到一半的 Vault 又该怎么收拾？

`obsidian-vault-pr` 专门管这些事。Agent 继续负责读懂材料和组织知识；确定性的 `vault-pr` CLI 负责校验、落盘、提交和恢复。每次语义写入都要先形成一笔有边界的事务，交代改动缘由和目标文件，并把证据一起装进来。计划、审查记录和 Git 历史都能回头核对。

## 一条链接进去，知识要落到体系里

`vault-pr` 可以接收普通网页、Markdown、PDF、本地文件，也能验证外部 Capture Provider 生成的微信文章或登录页快照。原始材料会成为不可变的 Source Capture，其身份与内容、访问类型绑定，不受文件名、抓取时间和本机路径影响。

采集只是起点。Agent 会通过本地 qmd 检索整个 Vault，找到相关概念和主题笔记，再提出一组受限修改：建立可见的来源笔记，补充已有概念页，在重要主张旁保留出处。遇到互相冲突的说法，它应当并列保留来源，更新当前判断和置信度，而不是挑一个顺眼的覆盖过去。

一次事务默认最多触及 15 条笔记。规模再大，就拆开做，逐笔检查、逐笔提交。

## 可以让它动手，出问题时也有路可退

写入前，`vault-pr` 会检查 Git 状态、Plan 结构、Obsidian 引用、附件和路径策略。创建、改写、重命名、拆分可以在验证后执行；合并、删除、矛盾处理和自治修复等高风险操作，需要独立 Reviewer 审核同一份确定性 Review Packet。Reviewer 不能顺手改稿，也不能替规划者自批自过。

真正落盘时，CLI 只提交这笔事务声明过的路径，并把文件哈希和结果写进 transaction receipt。中途报错会回滚；进程如果恰好断在 apply、commit 或 finalize 之间，恢复命令会先核对日志、备份、文件状态和 Git 状态，再决定补完提交还是撤回未提交结果。它不会用一次粗暴的重置掩盖现场。

内容提交后，qmd 索引才会刷新。索引失败不会反过来抹掉已经提交的笔记，系统会限次重试并留下失败状态。图谱检查和 Vault 自有的 RAG 评测也有独立入口，可以查断链、看检索是否退化，并把通过审核的评测结果记录成可追踪的基线。

## 它管的是边界

观点是否成立、概念该怎么组织，仍由 Agent 判断。`obsidian-vault-pr` 盯住容易被忽略的写入环节：Agent 能改多大范围，依据是什么，落盘前通过了哪些检查。失败后也能说明白发生了什么。

## 已经有 Agent 管理 Vault，为什么还需要它

同类开源项目大多走
[Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：
不可变 `raw/` 资料，由 Agent 编译并维护互联 Markdown Wiki，再配合 index / lint / query。
其中不少项目在**新建 Wiki 仓库**场景下，已经可以基本替代 `obsidian-vault-pr` 的知识维护效果。更值得问的是：在**已有 Vault** 上，谁负责持久改动的安全边界。

下表按架构与职责做定性比较，不是统一基准测试。星标约为 2026-07-17 的社区规模。

| 项目                                                                                         | 主要角色                                                         | 擅长什么                                                                     | 相对 `vault-pr` 的客观判断                                                                                                           |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| [obsidian-wiki](https://github.com/Ar9av/obsidian-wiki)（约 2.9k★）                          | 较均衡的 Karpathy Wiki 框架，适配多种编码 Agent                  | 采集、lint、重建、多 Agent 适配、完整生命周期                                | 新建 Wiki 仓库时是很强的替代品。对已有目录与笔记角色的确定性写入内核支持较弱。                                                       |
| [obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain)（约 3.3k★） | 自动化程度最高的 AI-first 第二大脑                               | 约 44 条命令、定时维护、混合检索、网页研究、笔记自重写                       | 若接受其约定，可以替代 `vault-pr` 的“Vault 会自己演化”效果。系统表面积大；安全更多依赖 Agent 工作流，而非内容绑定事务与恢复协议。    |
| [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian)（约 9.5k★）               | 最流行的 Claude Code Wiki 技能包与方法论                         | 来源吸收、概念页、矛盾标注、LYT/PARA/Zettelkasten 模式、按文件 advisory lock | Claude Code 下新建 Wiki 时几乎可直接替代。Advisory lock 与 Obsidian Git 自动提交，不等于带日志的精确路径 apply / commit / finalize。 |
| [llm-wiki-agent](https://github.com/SamurAIGPT/llm-wiki-agent)（约 3.2k★）                   | 轻量跨 Agent 自维护 Wiki 技能                                    | 运行时负担小；适配 Claude Code、Codex、OpenCode、Gemini CLI                  | 想要技能而非治理 CLI、又从零建 Karpathy Wiki 时，可替代 `vault-pr`。独立审查与恢复机制很少。                                         |
| [obsidian-mind](https://github.com/breferrari/obsidian-mind)（约 3.4k★）                     | 编码 Agent 的持久工作记忆                                        | 会话生命周期 hooks、qmd 检索、项目 / 决策 / 人物结构                         | 问题域不同：跨会话工程记忆，不是任意已有 Vault 的通用来源编译与治理。                                                                |
| [llm-wiki-compiler](https://github.com/atomicstrata/llm-wiki-compiler)（约 1.8k★）           | 偏研究资料的知识编译器                                           | Claim / 段落级出处、行号引用、两阶段生成、混合检索                           | 科研 Wiki、强引用场景下可替代 `vault-pr` 的知识编译路径。Obsidian 多为展示端；不是 Vault 无关的治理层。                              |
| [Synto](https://github.com/kytmanov/synto)（约 0.2k★）                                       | 本地 Ollama 优先的 Wiki 编译器（`obsidian-llm-wiki-local` 后继） | 稳定实体 ID、重命名 / 合并 / 拆分、草稿审批、编译 lineage、离线评估          | 编译器里产品直觉最接近（草稿、谱系、评估）。社区仍小；本质仍是 Wiki 编译器，不是“接管已有 Vault”的写入内核。                         |

相关但不属严格同类、适合组合使用的层：
[Claudian](https://github.com/YishenTu/claudian) /
[obsidian-agent-client](https://github.com/RAIT-09/obsidian-agent-client)
（Obsidian 内的 Agent UI）、
[obsidian-skills](https://github.com/kepano/obsidian-skills)
（Obsidian 格式与 CLI 能力）、
[qmd](https://github.com/tobi/qmd)
（本地 BM25 / 向量检索）。

### 本仓库的立足点

`obsidian-wiki` 这类项目回答的是：「Agent 该怎样长出一套知识 Wiki？」
`obsidian-vault-pr` 回答的是更窄的问题：**在你已经信任的 Vault 上，Agent 的一次写入何时才算完成、有依据、且可恢复？**

独特之处在于：

- **接管，不重塑。** 沿用现有目录、笔记角色和方法论，不强制 PARA、Zettelkasten 或 greenfield Wiki 布局。
- **证据 ≠ 授权。** 固化来源只能说明改动有依据；只有有边界的 Write Authorization Snapshot，才允许 Agent 提出修改。
- **高风险独立审查。** 合并、删除、矛盾处理等操作，必须把独立 Semantic Reviewer 绑定到同一份确定性 Review Packet。
- **确定性写入内核。** 干净工作区门槛、默认最多 15 条笔记、精确路径 apply/commit、transaction receipt，以及可核对的 `apply → commit → finalize` 恢复——不只靠 Agent 约定去 Git。
- **写完再验系统。** 内容提交后检查 exact-HEAD 的 qmd 新鲜度、Note Role 图谱，以及可接受的 RAG 评测基线。

从零建数字大脑、并愿意让 Agent 拥有组织方式时，Wiki 项目往往就够了。Vault 已经旧、共享或敏感，需要证明每次持久改动经过授权、有证据、必要时已审查、精确提交且可恢复时，再用 `vault-pr`。

### 如何结合使用

**默认：选一条主写入路径。** [obsidian-wiki](https://github.com/Ar9av/obsidian-wiki)
这类完整 Wiki 技能包，和 `vault-pr` skill 都会教 Agent 改 Vault，也常常会提交。两边都当一等公民写入时，通常会冲突（直接改文件 vs 带日志的 apply、`wiki-sync` / 裸 Git vs `vault-pr commit`、两套目录约定互相抢）。

- 从零建数字大脑、愿意让 Agent 拥有组织方式 → 单独用 Wiki 项目。
- 维护已有、Git 管理的 Vault，要可审计事务 → 单独用 `vault-pr`；笔记形态写在自己的 `AGENTS.md` / schema 即可。

**可以安全叠在 `vault-pr` 上的：** 格式技能
([obsidian-skills](https://github.com/kepano/obsidian-skills))、本地检索
([qmd](https://github.com/tobi/qmd))、可选 Agent UI
([Claudian](https://github.com/YishenTu/claudian) /
[obsidian-agent-client](https://github.com/RAIT-09/obsidian-agent-client))。
它们不替代写入内核。

```text
Obsidian Vault（Git）
├── 你的 AGENTS.md / 笔记约定   # 笔记该长什么样
├── Obsidian 格式技能           # wikilink、Bases、CLI
├── Agent UI（可选）
├── qmd                         # BM25 / 向量
└── vault-pr                    # 唯一允许改文件并提交的路径
```

**可选的进阶叠用**（Wiki 方法论 + `vault-pr`）只有在你能强制执行时才划算：

1. Wiki 技能可以建议综合与链接，但不得写 Vault 文件、不得跑 Git。
2. 凡持久改动一律走 `vault-pr`：采集 → 计划 → 审查 → apply → commit → recover。
3. 关掉 wiki 自动同步 / 全量 stage；活跃事务期间禁止裸 `git add` / `git commit`。
4. 产品代码提交留在本仓库，知识提交留在 Vault；对同一 Vault 串行写入。

做不到这条边界，就把 Wiki 技能包和 `vault-pr` 当互斥，不要叠。

如果你已经有一个长期使用、由 Git 管理的 Obsidian Vault，又想把链接采集和知识维护交给 Agent，`vault-pr` 会先给这位新同事立好规矩：进门要登记，动手前复核，做完要收尾。

先运行 `vault-pr doctor`。确认 Vault、Git、Defuddle 和 qmd 状态正常，再交出第一条链接。
