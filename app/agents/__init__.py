"""Agent registry package — one AI service, many agents (chatbot, document, …)."""
from ..agents.registry import AgentSpec, get_agent, list_agents

__all__ = ["AgentSpec", "get_agent", "list_agents"]
