# Tech Research Agent

一个基于 LangGraph、FastAPI、Streamlit 和 DeepSeek API 构建的技术调研报告 Agent 项目。

本项目基于 `agent-service-toolkit` 进行二次开发，新增了一个自定义 Agent：`tech-research-agent`。  
该 Agent 可以根据用户输入的技术主题，自动进行信息检索、内容分析，并生成结构化的中文技术调研报告。

---

## 1. 项目简介

Tech Research Agent 是一个面向技术学习、行业调研和方案分析的 AI Agent 应用。

用户输入一个技术主题后，系统会调用大语言模型和搜索工具，按照固定的技术调研报告结构输出内容，包括：

- 背景
- 核心概念
- 当前应用场景
- 技术实现思路
- 优势与局限
- 发展趋势
- 总结建议

示例问题：

```text
请调研一下 LangGraph 在多 Agent 系统中的应用，并生成一份技术调研报告