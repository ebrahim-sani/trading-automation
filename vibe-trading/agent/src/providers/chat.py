"""ChatLLM: Native Session-based Gemini implementation."""

from __future__ import annotations
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import google.generativeai as genai

@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Dict[str, Any]
    thought_signature: Optional[str] = None

@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

def sanitize_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items() if k not in ("default", "additionalProperties")}
    if isinstance(schema, list):
        return [sanitize_schema(i) for i in schema]
    return schema

class ChatLLM:
    """Session-managed Gemini Client that delegates signature handling to the Google SDK."""
    
    _sessions: Dict[str, genai.ChatSession] = {}

    def __init__(self, model_name: Optional[str] = None) -> None:
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not found")
        
        genai.configure(api_key=api_key)
        self.model_name = model_name or os.getenv("LANGCHAIN_MODEL_NAME", "gemini-flash-latest")

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, timeout: Optional[int] = None) -> LLMResponse:
        """Execute chat using a stateful session to preserve cryptographic signatures."""
        
        # Determine the session key (use the user message hash or a passed ID)
        # For simplicity in the AgentLoop, we'll use a one-shot rebuild for now 
        # but with THE CORRECT PROTOCOL that Google's SDK expects.
        
        google_tools = []
        if tools:
            decls = []
            for t in tools:
                fcall = t["function"] if "function" in t else t
                decls.append({
                    "name": fcall["name"],
                    "description": fcall.get("description", ""),
                    "parameters": sanitize_schema(fcall.get("parameters", {}))
                })
            google_tools.append({"function_declarations": decls})

        model = genai.GenerativeModel(
            model_name=self.model_name,
            tools=google_tools if google_tools else None
        )

        # REBUILD HISTORY with the EXACT format the SDK wants
        history = []
        if len(messages) > 1:
            for m in messages[:-1]: # Everything except the last user message
                role = "user" if m["role"] in ("user", "system") else "model"
                parts = []
                
                if m.get("content"):
                    parts.append({"text": m["content"]})
                
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fcall = {"name": tc["function"]["name"], "args": tc["function"].get("args", tc["function"].get("arguments", {}))}
                        if isinstance(fcall["args"], str): fcall["args"] = json.loads(fcall["args"])
                        if "thought_signature" in tc and tc["thought_signature"]:
                            fcall["thought_signature"] = tc["thought_signature"]
                        parts.append({"function_call": fcall})

                if m.get("role") == "tool":
                    role = "user" # Tool results MUST come from 'user' in the SDK history
                    
                    # Safe JSON parsing for tool results (handles '[cleared]' and other strings)
                    tool_content = m.get("content", "")
                    try:
                        if isinstance(tool_content, str):
                            tool_result = json.loads(tool_content)
                        else:
                            tool_result = tool_content
                    except (json.JSONDecodeError, TypeError):
                        tool_result = {"result": tool_content}

                    parts.append({"function_response": {
                        "name": m.get("name"), 
                        "response": tool_result
                    }})
                
                history.append({"role": role, "parts": parts})

        # Start a fresh session with the reconstructed (and corrected) history
        chat = model.start_chat(history=history)
        
        # Send the final message
        last_msg = messages[-1]["content"]
        response = chat.send_message(last_msg, generation_config={"temperature": 0.0})
        
        content_text = None
        tool_calls = []
        
        if response.candidates:
            candidate = response.candidates[0]
            for part in candidate.content.parts:
                if part.text:
                    content_text = part.text
                if part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCallRequest(
                        id=f"call_{fc.name}",
                        name=fc.name,
                        arguments=dict(fc.args),
                        thought_signature=getattr(fc, "thought_signature", None)
                    ))

        return LLMResponse(content=content_text, tool_calls=tool_calls)

    def stream_chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, **kwargs) -> LLMResponse:
        return self.chat(messages, tools=tools)
