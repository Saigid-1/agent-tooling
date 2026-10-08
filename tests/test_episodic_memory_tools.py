"""Model memory tools are session-bound and preserve the source/interpretation boundary."""
import pytest
from jsonschema import ValidationError
import asyncio
from datetime import timedelta
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore, EpisodeUnavailable
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools, tool_failure


def _setup(tmp_path):
    store = episode_store(tmp_path)
    event = {"event_id": "choice", "role": "user", "text": "Choose pool B; retain pool A for recovery."}
    episode = store.capture("session-1", source_ref="visible:1", events=[event])["episode_id"]
    return store, episode, event


def test_same_desk_discovery_recovery_and_proposal(tmp_path):
    store, episode, event = _setup(tmp_path)
    tools = EpisodicMemoryTools(store, "session-2")
    status = tools.call("memory.status", {})
    assert status["episodes"]["entries"][0]["episode_id"] == episode
    assert status["capsules"]["entries"] == []
    assert event["text"] not in str(status)
    source_directory = tools.call("memory.episode_directory", {"episode_id": episode, "limit": 1})
    assert source_directory["entries"] == [{"event_id": "choice", "role": "user", "characters": len(event["text"])}]
    assert source_directory["next_offset"] is None
    assert event["text"] not in str(source_directory)
    page = tools.call("memory.read_event", {"episode_id": episode, "event_id": "choice", "length": 10})
    assert page["text"] == event["text"][:10]
    assert page["next_start"] == 10
    quote = "Choose pool B"
    item = {"kind": "decision", "text": quote, "citations": [{"episode_id": episode,
            "event_id": "choice", "start": 0, "end": len(quote), "quote": quote}]}
    capsule = tools.call("memory.propose", {"episode_ids": [episode], "items": [item],
                                            "unresolved_questions": []})
    assert capsule["handoff"]["items"][0]["semantic_validation"] == "not-assessed"
    found = tools.call("memory.list", {"kind": "capsules"})
    assert found["entries"][0]["capsule_id"] == capsule["capsule_id"]
    assert "not accepted or active" in found["authority"]
    handoff = tools.call("memory.handoff", {"capsule_id": capsule["capsule_id"]})
    assert handoff["items"][0]["citations"][0]["quote"] == quote
    directory = tools.call("memory.evidence_directory", {"capsule_id": capsule["capsule_id"]})
    assert directory["entries"][0]["event_id"] == "choice"


def test_session_cannot_be_chosen_by_model_and_cross_desk_hidden(tmp_path):
    store, episode, _ = _setup(tmp_path)
    wrong = EpisodicMemoryTools(store, "session-3")
    assert wrong.call("memory.status", {})["episodes"]["entries"] == []
    with pytest.raises(EpisodeUnavailable):
        wrong.call("memory.read_event", {"episode_id": episode, "event_id": "choice"})
    with pytest.raises(EpisodeUnavailable):
        wrong.call("memory.episode_directory", {"episode_id": episode})
    with pytest.raises(ValidationError):
        wrong.call("memory.status", {"session": "session-1"})
    with pytest.raises(ValidationError):
        wrong.call("memory.propose", {"session": "session-1", "episode_ids": [episode],
                                       "items": [], "unresolved_questions": []})
    with pytest.raises(RuntimeError):
        EpisodicMemoryTools(store, "unadmitted")


def test_invalid_proposal_does_not_create_capsule(tmp_path):
    store, episode, _ = _setup(tmp_path)
    tools = EpisodicMemoryTools(store, "session-2")
    item = {"kind": "decision", "text": "invented", "citations": [{"episode_id": episode,
            "event_id": "choice", "start": 0, "end": 5, "quote": "wrong"}]}
    with pytest.raises(ValueError):
        tools.call("memory.propose", {"episode_ids": [episode], "items": [item],
                                      "unresolved_questions": []})
    assert tools.call("memory.list", {"kind": "capsules"})["total"] == 0
    with pytest.raises(ValidationError):
        tools.call("memory.read_event", {"episode_id": episode, "event_id": "choice", "length": 8001})
    with pytest.raises(ValueError):
        tools.call("memory.capture", {"source_ref": "model", "events": []})
    assert tool_failure("memory.read_event", EpisodeUnavailable("private"))["category"] == "memory_unavailable"
    assert tool_failure("memory.capture", ValueError("private"))["category"] == "unknown_operation"
    assert "private" not in str(tool_failure("memory.read_event", EpisodeUnavailable("private")))


def test_native_mcp_roundtrip_uses_bound_session(tmp_path):
    store, episode, _ = _setup(tmp_path)
    root = Path(__file__).resolve().parents[1]
    # Exercise the portable operator configuration through actual MCP framing.
    program = """
import asyncio, sys
from kp_agent_tooling._impl.service.desk_memory_runtime import from_local_config
from kp_agent_tooling.memory_cli import serve
asyncio.run(serve(from_local_config(sys.argv[1])))
"""

    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=["-c", program, str(tmp_path / "session-2.json")],
            env={"PYTHONPATH": str(root / "packages/tooling/src") + ":" + str(root)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"memory.status", "memory.episode_directory", "memory.read_event", "memory.propose"} <= names
                reply = await session.call_tool("memory.status", {})
                assert not reply.isError
                assert json.loads(reply.content[0].text) == reply.structuredContent
                assert reply.structuredContent["episodes"]["entries"][0]["episode_id"] == episode
                proposal = await session.call_tool("memory.propose", {"episode_ids":[episode],
                    "items":[{"kind":"decision","text":"Choose pool B","citations":[{
                        "episode_id":episode,"event_id":"choice","start":0,"end":13,"quote":"Choose pool B"}]}],
                    "unresolved_questions":[]})
                capsule_id = proposal.structuredContent["capsule_id"]
                resumed = await session.call_tool("memory.resume", {"capsule_id":capsule_id,"budget_bytes":3000})
                assert not resumed.isError and resumed.structuredContent["complete_handoff"]
                page = await session.call_tool("memory.handoff_page", {"capsule_id":capsule_id})
                assert page.structuredContent["entries"][0]["value"]["text"] == "Choose pool B"
                coverage = await session.call_tool("memory.evidence_directory", {"capsule_id":capsule_id})
                start,end = coverage.structuredContent["entries"][0]["uncited_ranges"][0]
                recovered = await session.call_tool("memory.read_event",
                    {"episode_id":episode,"event_id":"choice","start":start,"length":end-start})
                assert recovered.structuredContent["text"] == "; retain pool A for recovery."
                directory = await session.call_tool("memory.episode_directory", {"episode_id": episode})
                assert directory.structuredContent["entries"][0]["event_id"] == "choice"
                denied = await session.call_tool("memory.status", {"session": "session-1"})
                assert denied.isError and denied.structuredContent["category"] == "invalid_arguments"
                assert "guidance" in denied.structuredContent
                absent = await session.call_tool("memory.read_event", {"episode_id": episode, "event_id": "absent"})
                assert absent.isError and absent.structuredContent["category"] == "memory_unavailable"

    asyncio.run(check())


def test_cli_configuration_failure_is_structured(tmp_path):
    config = tmp_path / "invalid.json"
    config.write_text("{}")
    from kp_agent_tooling import memory_cli
    script = Path(memory_cli.__file__)
    result = subprocess.run([sys.executable, str(script), "--config", str(config), "doctor"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["category"] == "memory_unavailable"
    assert "Traceback" not in result.stderr
