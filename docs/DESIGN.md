# 设计说明

本文说明 `astrbot_plugin_lazy_tools` 的实现方式、分组规则、缓存行为与已知权衡。

## 1. 总体结构

```
main.py                   插件入口：注册 on_llm_request / /lazy 命令 / Web API
lazy_tools/
  config.py               config.json / overrides.json 的读写、归一化、导入导出
  catalog.py              工具分类、分组、说明覆盖、token 估算、关键词索引
  runtime.py              会话状态、注入控制器、元工具实现、请求改写、页面快照
  skills.py               AstrBot Skills 段的解析、压缩与技能简介覆盖
  web_api.py              管理页接口（state/config/overrides/actions/export/import/reload）
pages/dashboard/          管理页（原生 HTML/CSS/JS，通过 Plugin Page Bridge 与后端通信）
```

数据流：

```
AstrBot LLM 请求
  └─ on_llm_request(priority=-50000)
       ├─ 从全量 ToolSet 构建目录（分类 + 覆盖项）
       ├─ 恢复该会话已加载组（会话内 + state.json 持久化）
       ├─ 生成注入 ToolSet = 3 元工具 + 常驻组/工具 + 已加载组/工具
       ├─ 替换 req.func_tool
       ├─ 替换系统提示词里的工具说明（原生 TOOL_CALL_PROMPT → 两段式说明）
       └─ 记录本次统计（注入数 / 全量数 / token 估算 / 加载组）
```

元工具 handler 直接修改**同一个** `ToolSet` 实例，而 `ToolLoopAgentRunner` 每一步都会重新读取
`req.func_tool`，所以“加载后下一步可用”不需要额外 hook。

## 2. 自动分组规则

| 工具来源 | 判定方式 | 组 id |
| --- | --- | --- |
| AstrBot 内置能力 | 工具名匹配固定前缀/能力表 | `files` `shell` `web` `knowledge` `messaging` `tasks` `memory` `sessions` `subagents` `browser` `agent_skills` `computer_use` |
| 插件工具 | `FunctionTool.handler_module_path` → `star_map` 元数据 | 插件 display_name / 目录名（如 `demo_plugin`） |
| MCP 工具 | `MCPTool` 的 server 名 | `mcp_<server>` |
| 来源不明 | 名字语义兜底 | `memory` / `sessions` / `other` |

组内工具按名字排序；组按固定优先级 + id 排序。**同一组集合、同一顺序**渲染，保证前缀稳定。

## 3. 说明覆盖

- 组覆盖：显示名、组说明（模型可见）、隐藏；
- 工具覆盖：说明、归组（可新建自定义组）、隐藏；
- 工具说明覆盖通过 `copy.copy(tool)` 生成注入用的副本，**不修改全局工具对象**，因此权限校验、MCP 调用、handoff 等行为保持一致；
- 覆盖项保存在 `overrides.json`，删除即恢复自动值。

## 3.1 技能段（Skills）压缩

AstrBot 的 `build_skills_prompt()` 会把当前可用技能拼成一段固定格式的 system prompt：

```
## Skills
...（说明）...
### Available skills
- **<name>**: <完整 description>
  File: `<绝对路径>`
### Skill rules
1. ...（七条英文规则）
```

插件在同一个 `on_llm_request` 里，对这段做按需压缩：

- 解析出 `(name, description, path)`，按 `SKILL.md` 的父目录把技能分组；
- 技能根目录只写一次，条目写成 `- name: 一句简介 → name/SKILL.md`；
- 简介默认取第一句并截断到 `skill_desc_max_chars`（默认 50）；
- 七条英文规则换成一段中文精简规则（可关闭，保留原规则）；
- 支持 `overrides.skills.<name>`：`description` 覆盖简介、`hidden` 隐藏该技能；
- 解析失败 / 段太短时原样放行，不会把技能弄丢。

压缩只改注入的 system prompt，不修改 `SKILL.md`，也不改技能目录或 `skills.json`。

实测（5 个技能）：Skills 段 **6,700 → 932 字符（约 −86%）**，并且自定义简介 / 隐藏技能会实时反映到下一轮请求。

## 4. 缓存行为实测

DeepSeek / OpenAI 兼容接口的前缀缓存按序列化的 token 前缀匹配，工具 schema 通常位于消息之前，属于前缀的一部分。
在一个真实实例上做同会话 5 轮对照（`provider_stats` 统计，每轮含工具循环所有请求的合计）：

| 配置 | 第 2 轮起 cached/轮 | 每轮 new | 每轮 prompt 合计 | 命中率 |
| --- | --- | --- | --- | --- |
| full 全量（工具集恒定） | ≈ 23,168 | ≈ 11k | ≈ 34k | 65–82% |
| lazy 无加载（工具集恒定） | ≈ 6,272 | ≈ 11–13k | ≈ 18k | 33–35% |
| lazy 中途加载一个组 | 加载后回到 ≈ 6,656 | 该轮 29.7k | 该轮聚合 98.8k | 该轮 70% |

观察：

1. 两者的 cached 差值 ≈ 全量多出来的 schema 大小，**证明工具块确实在缓存前缀中**：工具集一变，从工具块位置往后（含历史）当次请求要重算，之后恢复稳定。
2. 变化只影响“变更那一次”。插件把已加载组做 sticky，只有真正 load/unload 才会改工具块；自动预加载默认关闭，避免误加载造成额外失效。
3. 缓存命中时 schema 也并非免费：按缓存命中价约为未命中 1/10 估算，全量模式每请求多出的 schema 仍要付约 10% 的价格。lazy 每次加载组相当于一次“前缀全价重算”。多轮混合会话里两者等效成本接近，但 lazy 在**首轮冷启动**（少一半输入 token）、**上下文长度**（18k vs 34k）和**无缓存场景**上占优。
4. AstrBot 上下文本身有动态成分（人格、时间、记忆 RAG、增强插件），命中率会波动，工具不是唯一变量。

想把缓存稳定性拉满：常用组直接 pin、减少会话中途加载、不频繁 unload、覆盖项只在需要时修改。
极端方案是 dispatcher（固定 1–2 个工具 schema + 通用 `call_tool(name, args)`），工具块永不变，但会多一跳、参数靠 JSON、工具选择可靠性下降，因此没有作为默认方案。

## 5. 已知限制与后续方向

限制：

- 没有核心级自愈：模型误调未加载工具时，会拿到 AstrBot 原生错误（含可用工具列表），再由提示词引导搜索/加载。
- 一次 Agent 运行内工具目录是快照；运行中新注册的工具在下一轮进入目录。
- 运行中加载组超过上限时只做硬上限保护（上限 + 8），下一轮构建时再裁到上限。
- 与 `tool_schema_mode=skills_like` 不兼容。
- 技能段压缩依赖 AstrBot 现有的 `## Skills` 格式；AstrBot 大版本改变格式时解析失败会原样放行（不会丢技能），届时更新解析规则即可。

后续可做：

- 运行中刷新目录（可配合一个很小的核心 hook 或元工具）；
- 自动衰减 / 自动卸载：长会话按轮次卸载不用的组；
- 管理页增强：拖动排序、批量编辑、内置组与插件组分区统计；
- MCP 工具按 server 前缀或工具名前缀做更细的默认归组模板。
