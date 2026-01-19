---
name: standard-workflow
description: ClaudeCode 主AI 全流程工作流（按需调度 Codex 支持）
tips: /standard-workflow <task_description>
---

# 🚨 5条铁律（违反=立即终止）

1. **任何操作前必须 sequential-thinking**（包括 Codex 执行AI）
2. **上下文必须由 Codex 收集**（主AI 禁止自行收集）
3. **路径必须 `<project>/.claude/`**（禁止 `~/.claude/` 或 `C:\Users\...`）
4. **默认自动执行，不询问**（仅5类例外：删核心配置/数据库破坏/Git push/连续3次同错/用户要求）
5. **工具链顺序不可乱**：sequential-thinking → shrimp-task-manager → Codex

---

# ⚡ 4步执行流程

## ```
1. sequential-thinking           → 理解目标/风险/验证
2. Codex 收集上下文              → .claude/context-*.json
3. shrimp-task-manager 规划     → 任务拆解
4. 主AI 编码 + Codex 审查        → 小步实现 + 质量验证
## ```

---

# 📁 路径规范（高频错误）

✅ **正确**：`<project>/.claude/context-initial.json`
❌ **禁止**：`~/.claude/` 或 `C:\Users\WenYu\.claude/`

---

# 📎 Codex 调度模板

**首次调用**：
## ```
mcp__codex__codex(
  model="gpt-5.2-codex",
  sandbox="danger-full-access",
  approval-policy="on-failure",
  prompt="
[TASK_MARKER: YYYYMMDD-HHMMSS-XXXX]

目标：[1-2句话]
输出：[交付物列表]
约束：[限制条件]

请在响应末尾附加：[CONVERSATION_ID]: <conversationId>
"
)
## ```

**继续会话**：
## ```
mcp__codex__codex-reply(conversationId="<ID>", prompt="[指令]")
## ```

---

详细流程参考 @CLAUDE.md