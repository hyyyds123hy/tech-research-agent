from datetime import datetime
from typing import Literal

from langchain_community.tools import OpenWeatherMapQueryRun
from langchain_core.tools import tool
from ddgs import DDGS
from langchain_community.utilities import OpenWeatherMapAPIWrapper
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnableSerializable
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode

from agents.safeguard import Safeguard, SafeguardOutput, SafetyAssessment
from agents.tools import calculator
from core import get_model, settings


class AgentState(MessagesState, total=False):
    """`total=False` is PEP589 specs.

    documentation: https://typing.readthedocs.io/en/latest/spec/typeddict.html#totality
    """

    safety: SafeguardOutput
    remaining_steps: RemainingSteps
    draft_report: str


@tool("WebSearch")
def web_search(query: str) -> str:
    """Search the web for recent information. Input should be a search query."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        if not results:
            return "WebSearch 没有找到相关结果。"

        output = []
        for i, r in enumerate(results, start=1):
            title = r.get("title", "")
            url = r.get("href") or r.get("url", "")
            body = r.get("body", "")
            output.append(f"{i}. {title}\n链接：{url}\n摘要：{body}")

        return "\n\n".join(output)

    except Exception as e:
        return (
            f"WebSearch 工具调用失败：{type(e).__name__}: {e}\n"
            "请基于已有知识回答，并明确说明本次未能成功联网检索。"
        )
tools = [web_search, calculator]

# Add weather tool if API key is set
# Register for an API key at https://openweathermap.org/api/
if settings.OPENWEATHERMAP_API_KEY:
    wrapper = OpenWeatherMapAPIWrapper(
        openweathermap_api_key=settings.OPENWEATHERMAP_API_KEY.get_secret_value()
    )
    tools.append(OpenWeatherMapQueryRun(name="Weather", api_wrapper=wrapper))

current_date = datetime.now().strftime("%B %d, %Y")
instructions = f"""
    You are a helpful research assistant with the ability to search the web and use other tools.
    Today's date is {current_date}.

    NOTE: THE USER CAN'T SEE THE TOOL RESPONSE.

    A few things to remember:
    - Please include markdown-formatted links to any citations used in your response. Only include one
    or two citations per response unless more are needed. ONLY USE LINKS RETURNED BY THE TOOLS.
    - Use calculator tool with numexpr to answer math questions. The user does not understand numexpr,
      so for the final response, use human readable format - e.g. "300 * 200", not "(300 \\times 200)".
    """
reviewer_instructions = """
你是一个技术报告 Reviewer，负责检查和优化技术调研报告。

你的任务：
1. 检查报告结构是否完整。
2. 检查内容是否具体，避免空泛表达。
3. 检查技术实现思路是否有工程化细节。
4. 检查是否包含优势、局限、风险和落地难点。
5. 检查语言是否清晰、专业、适合中文技术报告。
6. 如果报告质量不够，请直接优化成最终版本。

最终输出要求：
- 只输出最终优化后的报告。
- 不要输出“这是初稿”。
- 不要输出你的审查过程。
- 可以在报告末尾增加一个简短的“Reviewer 检查结果”。
- 使用中文。
"""


def wrap_model(model: BaseChatModel) -> RunnableSerializable[AgentState, AIMessage]:
    bound_model = model.bind_tools(tools)
    preprocessor = RunnableLambda(
        lambda state: [SystemMessage(content=instructions)] + state["messages"],
        name="StateModifier",
    )
    return preprocessor | bound_model  # type: ignore[return-value]


def format_safety_message(safety: SafeguardOutput) -> AIMessage:
    content = (
        f"This conversation was flagged for unsafe content: {', '.join(safety.unsafe_categories)}"
    )
    return AIMessage(content=content)


async def acall_model(state: AgentState, config: RunnableConfig) -> AgentState:
    m = get_model(config["configurable"].get("model", settings.DEFAULT_MODEL))
    model_runnable = wrap_model(m)
    response = await model_runnable.ainvoke(state, config)

    if state["remaining_steps"] < 2 and response.tool_calls:
        return {
            "draft_report": "抱歉，本次任务需要更多步骤才能完成，无法生成完整技术调研报告。",
            "messages": [],
        }

    if response.tool_calls:
        return {"messages": [response]}

    return {
        "draft_report": response.content,
        "messages": [],
    }


async def review_report(state: AgentState, config: RunnableConfig) -> AgentState:
    draft_report = state.get("draft_report", "")

    if not draft_report:
        return {
            "messages": [
                AIMessage(content="抱歉，本次没有生成可供 Reviewer 检查的报告内容。")
            ]
        }

    m = get_model(config["configurable"].get("model", settings.DEFAULT_MODEL))

    review_prompt = f"""
{reviewer_instructions}

以下是待检查和优化的技术调研报告初稿：

{draft_report}
"""

    response = await m.ainvoke([SystemMessage(content=review_prompt)], config)

    final_content = response.content

    if "Reviewer 检查结果" not in final_content:
        final_content = (
            final_content
            + "\n\n---\n\n"
            + "### Reviewer 检查结果\n\n"
            + "已完成结构完整性、技术深度、风险分析和表达质量检查。"
        )

    return {"messages": [AIMessage(content=final_content)]}

async def safeguard_input(state: AgentState, config: RunnableConfig) -> AgentState:
    safeguard = Safeguard()
    safety_output = await safeguard.ainvoke(state["messages"])
    return {"safety": safety_output, "messages": []}


async def block_unsafe_content(state: AgentState, config: RunnableConfig) -> AgentState:
    safety: SafeguardOutput = state["safety"]
    return {"messages": [format_safety_message(safety)]}


# Define the graph
agent = StateGraph(AgentState)
agent.add_node("model", acall_model)
agent.add_node("reviewer", review_report)
agent.add_node("tools", ToolNode(tools))
agent.add_node("guard_input", safeguard_input)
agent.add_node("block_unsafe_content", block_unsafe_content)
agent.set_entry_point("guard_input")


# Check for unsafe input and block further processing if found
def check_safety(state: AgentState) -> Literal["unsafe", "safe"]:
    safety: SafeguardOutput = state["safety"]
    match safety.safety_assessment:
        case SafetyAssessment.UNSAFE:
            return "unsafe"
        case _:
            return "safe"


agent.add_conditional_edges(
    "guard_input", check_safety, {"unsafe": "block_unsafe_content", "safe": "model"}
)

# Always END after blocking unsafe content
agent.add_edge("block_unsafe_content", END)

# Always run "model" after "tools"
agent.add_edge("tools", "model")


# After "model", if there are tool calls, run "tools". Otherwise END.
def pending_tool_calls(state: AgentState) -> Literal["tools", "reviewer"]:
    last_message = state["messages"][-1]

    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"

    return "reviewer"


agent.add_conditional_edges(
    "model",
    pending_tool_calls,
    {"tools": "tools", "reviewer": "reviewer"},
)

agent.add_edge("reviewer", END)

tech_research_agent = agent.compile()
