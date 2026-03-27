#!/usr/bin/env python3
"""
Claude Code -> Langfuse hook

"""

import json
import os
import sys
import time
import hashlib
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- Langfuse import (fail-open) ---
try:
    from langfuse import Langfuse, propagate_attributes
except Exception:
    sys.exit(0)

# --- Paths ---
STATE_DIR = Path(__file__).resolve().parent / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))

# ----------------- Logging -----------------
def _log(level: str, message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
    except Exception:
        # Never block
        pass

def debug(msg: str) -> None:
    if DEBUG:
        _log("DEBUG", msg)

def info(msg: str) -> None:
    _log("INFO", msg)

def warn(msg: str) -> None:
    _log("WARN", msg)

def error(msg: str) -> None:
    _log("ERROR", msg)

# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl  # Unix only
            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.05)
        except Exception:
            # If locking isn't available, proceed without it.
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(session_id: str, transcript_path: str) -> str:
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    """
    Claude Code hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}

def extract_session_and_transcript(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Path]]:
    """
    Tries a few plausible field names; exact keys can vary across hook types/versions.
    Prefer structured values from stdin over heuristics.
    """
    session_id = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )

    transcript = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or payload.get("transcript", {}).get("path")
    )

    if transcript:
        try:
            transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path

# ----------------- Transcript parsing helpers -----------------
def get_content(msg: Dict[str, Any]) -> Any:
    if not isinstance(msg, dict):
        return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg: Dict[str, Any]) -> Optional[str]:
    # Claude Code transcript lines commonly have type=user/assistant OR message.role
    t = msg.get("type")
    if t in ("user", "assistant"):
        return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return r
    return None

def is_tool_result(msg: Dict[str, Any]) -> bool:
    role = get_role(msg)
    if role != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def iter_tool_results(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def iter_tool_uses(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def extract_tool_result_text(content: Any) -> str:
    """Extract text from tool result content, handling str, list-of-blocks, and other types."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"

def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None

def get_timestamp(msg: Dict[str, Any]) -> Optional[datetime]:
    """Extract timestamp from a transcript message and return as datetime."""
    ts = msg.get("timestamp")
    if not ts:
        return None
    try:
        # Handle ISO format like "2026-03-25T22:14:13.347Z"
        if isinstance(ts, str):
            ts = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(ts)
    except Exception:
        pass
    return None

def get_timestamp_ns(msg: Dict[str, Any]) -> Optional[int]:
    """Extract timestamp as nanoseconds since epoch (for OTEL spans)."""
    dt = get_timestamp(msg)
    if dt is None:
        return None
    return int(dt.timestamp() * 1e9)

def _set_span_times(observation: Any, start_ns: Optional[int], end_ns: Optional[int] = None) -> None:
    """Override start/end time on the underlying OTEL span for correct ordering.

    start_time is set directly on the span attribute.
    end_time is injected by wrapping span.end() so the context manager's __exit__
    passes our timestamp instead of time.time_ns().
    """
    try:
        otel_span = getattr(observation, "_otel_span", None)
        if otel_span is None:
            return
        if start_ns is not None and hasattr(otel_span, "_start_time"):
            otel_span._start_time = start_ns
        if end_ns is not None:
            _orig_end = otel_span.end
            def _end_with_time(end_time: Optional[int] = None) -> None:
                _orig_end(end_time=end_ns)
            otel_span.end = _end_with_time
    except Exception:
        pass

# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> Tuple[List[Dict[str, Any]], SessionState]:
    """
    Reads only new bytes since ss.offset. Keeps ss.buffer for partial last line.
    Returns parsed JSON lines (best-effort) and updated state.
    """
    if not transcript_path.exists():
        return [], ss

    try:
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    # last element may be incomplete
    ss.buffer = lines[-1]
    ss.offset = new_offset

    msgs: List[Dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue

    return msgs, ss

# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]
    tool_uses_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tool_result_timestamps: Dict[str, Optional[int]] = field(default_factory=dict)  # tool_use_id -> timestamp_ns
    tool_result_msgs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # tool_use_id -> raw transcript message (for agent metadata)
    msg_tool_use_ids: Dict[str, List[str]] = field(default_factory=dict)  # message_id -> [tool_use_ids] in order

def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    """
    Groups incremental transcript rows into turns:
    user (non-tool-result) -> assistant messages -> (tool_result rows, possibly interleaved)
    Uses:
    - assistant message dedupe by message.id (latest row wins)
    - tool results dedupe by tool_use_id (latest wins)
    - injected user text messages mid-turn (e.g., skill documents) are appended to the
      preceding tool result rather than starting a new turn
    """
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None

    # assistant messages for current turn:
    assistant_order: List[str] = []             # message ids in order of first appearance (or synthetic)
    assistant_latest: Dict[str, Dict[str, Any]] = {}  # id -> latest msg

    tool_results_by_id: Dict[str, Any] = {}     # tool_use_id -> content
    tool_uses_by_id: Dict[str, Dict[str, Any]] = {}  # tool_use_id -> tool_use block (accumulated from ALL rows)
    tool_result_timestamps: Dict[str, Optional[int]] = {}  # tool_use_id -> timestamp_ns
    tool_result_msgs: Dict[str, Dict[str, Any]] = {}  # tool_use_id -> raw message
    msg_tool_use_ids: Dict[str, List[str]] = {}  # message_id -> [tool_use_ids] in order

    last_tool_result_id: Optional[str] = None   # tracks last tool_result for context injection

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id, tool_uses_by_id, tool_result_timestamps, tool_result_msgs, msg_tool_use_ids, last_tool_result_id, turns
        if current_user is None:
            return
        if not assistant_latest:
            return
        assistants = [assistant_latest[mid] for mid in assistant_order if mid in assistant_latest]
        turns.append(Turn(user_msg=current_user, assistant_msgs=assistants, tool_results_by_id=dict(tool_results_by_id), tool_uses_by_id=dict(tool_uses_by_id), tool_result_timestamps=dict(tool_result_timestamps), tool_result_msgs=dict(tool_result_msgs), msg_tool_use_ids=dict(msg_tool_use_ids)))

    for msg in messages:
        role = get_role(msg)

        # tool_result rows show up as role=user with content blocks of type tool_result
        if is_tool_result(msg):
            result_ts = get_timestamp_ns(msg)
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = tr.get("content")
                    tool_result_timestamps[str(tid)] = result_ts
                    tool_result_msgs[str(tid)] = msg
                    last_tool_result_id = str(tid)
            continue

        if role == "user":
            # Check if this is an injected context message (e.g., skill document)
            # appearing mid-turn right after a tool_result, before the next assistant response.
            if current_user is not None and last_tool_result_id is not None:
                content = get_content(msg)
                text = extract_text(content)
                if text:
                    # Append injected context to the preceding tool result output
                    existing = tool_results_by_id.get(last_tool_result_id, "")
                    if isinstance(existing, str):
                        tool_results_by_id[last_tool_result_id] = existing + "\n\n" + text
                    elif isinstance(existing, list):
                        tool_results_by_id[last_tool_result_id] = json.dumps(existing, ensure_ascii=False) + "\n\n" + text
                    else:
                        tool_results_by_id[last_tool_result_id] = str(existing) + "\n\n" + text
                    continue

            # new user message -> finalize previous turn
            flush_turn()

            # start a new turn
            current_user = msg
            assistant_order = []
            assistant_latest = {}
            tool_results_by_id = {}
            tool_uses_by_id = {}
            tool_result_timestamps = {}
            tool_result_msgs = {}
            msg_tool_use_ids = {}
            last_tool_result_id = None
            continue

        if role == "assistant":
            if current_user is None:
                # ignore assistant rows until we see a user message
                continue

            last_tool_result_id = None  # reset: we're back in assistant territory

            mid = get_message_id(msg) or f"noid:{len(assistant_order)}"
            if mid not in assistant_latest:
                assistant_order.append(mid)
            assistant_latest[mid] = msg

            # Accumulate tool_uses from every row — later streaming updates may drop earlier ones.
            # Also track which tool_use IDs belong to which message (for parallel tool calls).
            for tu in iter_tool_uses(get_content(msg)):
                tid = tu.get("id")
                if tid:
                    tid_str = str(tid)
                    tool_uses_by_id[tid_str] = tu
                    if mid not in msg_tool_use_ids:
                        msg_tool_use_ids[mid] = []
                    if tid_str not in msg_tool_use_ids[mid]:
                        msg_tool_use_ids[mid].append(tid_str)
            continue

        # ignore unknown rows

    # flush last
    flush_turn()
    return turns

# ----------------- Sub-agent helpers -----------------
def _get_agent_id(turn: "Turn", tool_use_id: str) -> Optional[str]:
    """Extract agentId from the tool_result message's toolUseResult field."""
    msg = turn.tool_result_msgs.get(tool_use_id)
    if not msg:
        return None
    tur = msg.get("toolUseResult")
    if isinstance(tur, dict):
        return tur.get("agentId")
    return None

def _find_subagent_transcript(transcript_path: Path, agent_id: str) -> Optional[Path]:
    """Find the sub-agent JSONL transcript file."""
    session_dir = transcript_path.with_suffix("")  # e.g., /path/to/{session-id}
    subagent_file = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
    if subagent_file.exists():
        return subagent_file
    return None

def _read_full_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read all JSON lines from a file."""
    msgs: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return msgs

def _emit_subagent_turns(turn_span: Any, turn: "Turn", tool_use_id: str, tool_obs: Any, transcript_path: Path) -> None:
    """Parse a sub-agent transcript and emit its turns as nested spans under the tool observation."""
    agent_id = _get_agent_id(turn, tool_use_id)
    if not agent_id:
        return

    subagent_path = _find_subagent_transcript(transcript_path, agent_id)
    if not subagent_path:
        return

    msgs = _read_full_jsonl(subagent_path)
    if not msgs:
        return

    sub_turns = build_turns(msgs)
    if not sub_turns:
        return

    # Load meta for the agent type
    meta_path = subagent_path.with_suffix(".meta.json")
    agent_meta: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            agent_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Emit sub-agent observations directly under tool_obs (no wrapper span).
    # If multiple sub-turns exist, prefix names with the turn index.
    for st_idx, st in enumerate(sub_turns, 1):
        sub_user_text_raw = extract_text(get_content(st.user_msg))
        sub_user_text, _ = truncate_text(sub_user_text_raw, max_chars=2000)
        sub_start_ns = get_timestamp_ns(st.user_msg)
        sub_end_ns = get_timestamp_ns(st.assistant_msgs[-1]) if st.assistant_msgs else sub_start_ns

        num_sub_responses = len(st.assistant_msgs)
        for sr_idx, sub_asst in enumerate(st.assistant_msgs):
            sc = get_content(sub_asst)
            s_text_raw = extract_text(sc)
            s_text, _ = truncate_text(s_text_raw)
            s_model = get_model(sub_asst)
            s_tool_uses = _get_tool_uses_for_msg(st, sub_asst)

            # Same sequential chaining as emit_turn:
            # First response starts at user message time (step start);
            # subsequent responses start after previous tool ends.
            if sr_idx == 0:
                s_start_ns = sub_start_ns
            else:
                prev_s_tus = _get_tool_uses_for_msg(st, st.assistant_msgs[sr_idx - 1])
                if prev_s_tus:
                    prev_s_end_times = [
                        st.tool_result_timestamps.get(str(ptu.get("id", "")))
                        for ptu in prev_s_tus
                    ]
                    prev_s_end_times = [t for t in prev_s_end_times if t is not None]
                    s_start_ns = max(prev_s_end_times) if prev_s_end_times else get_timestamp_ns(sub_asst)
                else:
                    s_start_ns = get_timestamp_ns(sub_asst)

            # Claude ends when its first tool starts
            if s_tool_uses:
                s_end_ns = get_timestamp_ns(sub_asst)
            elif sr_idx + 1 < num_sub_responses:
                s_end_ns = get_timestamp_ns(st.assistant_msgs[sr_idx + 1])
            else:
                s_end_ns = sub_end_ns

            s_output: Dict[str, Any] = {"role": "assistant", "content": s_text}
            if s_tool_uses:
                s_output["tool_calls"] = [
                    {"id": tu.get("id"), "name": tu.get("name"), "input": tu.get("input")}
                    for tu in s_tool_uses
                ]

            s_name = (
                f"Claude Response ({sr_idx + 1}/{num_sub_responses})"
                if num_sub_responses > 1
                else "Claude Response"
            )

            # Input: first response gets user text, subsequent get tool results
            if sr_idx == 0:
                s_gen_input: Any = {"role": "user", "content": sub_user_text}
            else:
                prev_s_tool_uses = _get_tool_uses_for_msg(st, st.assistant_msgs[sr_idx - 1])
                if prev_s_tool_uses:
                    s_gen_input = [
                        {
                            "type": "tool_result",
                            "tool_use_id": ptu.get("id"),
                            "tool_name": ptu.get("name"),
                            "content": truncate_text(
                                extract_tool_result_text(st.tool_results_by_id.get(str(ptu.get("id", ""))))
                            , max_chars=2000)[0],
                        }
                        for ptu in prev_s_tool_uses
                    ]
                else:
                    s_gen_input = {"role": "continuation"}

            with tool_obs.start_as_current_observation(
                name=s_name,
                as_type="generation",
                model=s_model,
                input=s_gen_input,
                output=s_output,
                metadata={"tool_count": len(s_tool_uses), "agent_id": agent_id, "agent_type": agent_meta.get("agentType", "")},
            ) as s_gen:
                _set_span_times(s_gen, s_start_ns, s_end_ns)

            for stu in s_tool_uses:
                s_tid = str(stu.get("id", ""))
                s_tc_name = stu.get("name") or "unknown"
                s_tc_input = stu.get("input")
                s_result_raw = st.tool_results_by_id.get(s_tid)
                s_out_str = extract_tool_result_text(s_result_raw)
                s_out_trunc, s_out_meta = truncate_text(s_out_str)
                # Tool starts right after Claude Response ended
                s_tool_start = s_end_ns
                s_tool_end = st.tool_result_timestamps.get(s_tid)

                if isinstance(s_tc_input, str):
                    s_tc_input, _ = truncate_text(s_tc_input)

                with tool_obs.start_as_current_observation(
                    name=f"Tool: {s_tc_name}",
                    as_type="tool",
                    input=s_tc_input,
                    metadata={"tool_name": s_tc_name, "tool_id": s_tid, "output_meta": s_out_meta},
                ) as s_tool_obs:
                    _set_span_times(s_tool_obs, s_tool_start, s_tool_end)
                    s_tool_obs.update(output=s_out_trunc)


# ----------------- Langfuse emit -----------------
def _get_tool_uses_for_msg(turn: Turn, asst_msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Get the full list of accumulated tool_uses for an assistant message.

    Uses msg_tool_use_ids (populated across all streaming updates) to look up
    tool_use blocks from tool_uses_by_id, so parallel tool calls aren't lost
    to streaming dedup.
    """
    mid = get_message_id(asst_msg)
    if mid and mid in turn.msg_tool_use_ids:
        return [turn.tool_uses_by_id[tid] for tid in turn.msg_tool_use_ids[mid] if tid in turn.tool_uses_by_id]
    # Fallback: extract directly from content (works when no streaming dedup issue)
    return iter_tool_uses(get_content(asst_msg))

def emit_turn(langfuse: Langfuse, trace_id: str, session_id: str, turn_num: int, turn: Turn, transcript_path: Path) -> None:
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    # Timestamps: turn starts at user message, ends at last assistant message
    turn_start_ns = get_timestamp_ns(turn.user_msg)
    turn_end_ns = get_timestamp_ns(turn.assistant_msgs[-1]) if turn.assistant_msgs else None

    # Final assistant text for the turn-level output
    last_assistant = turn.assistant_msgs[-1]
    final_text_raw = extract_text(get_content(last_assistant))
    final_text, final_text_meta = truncate_text(final_text_raw)

    # Collect all tool calls across all assistant messages for the turn-level summary
    all_tool_uses: List[Dict[str, Any]] = []
    for asst in turn.assistant_msgs:
        all_tool_uses.extend(_get_tool_uses_for_msg(turn, asst))

    turn_output: Dict[str, Any] = {"role": "assistant", "content": final_text}
    if all_tool_uses:
        turn_output["tool_calls"] = [{"name": tu.get("name"), "id": tu.get("id")} for tu in all_tool_uses]

    trace_context = {"trace_id": trace_id}

    with propagate_attributes(
        session_id=session_id,
        trace_name=session_id,
        tags=["claude-code"],
    ):
        # Each turn is a span under the shared trace
        with langfuse.start_as_current_observation(
            trace_context=trace_context,
            name=f"Turn {turn_num}",
            input={"role": "user", "content": user_text},
            output=turn_output,
            metadata={
                "source": "claude-code",
                "session_id": session_id,
                "turn_number": turn_num,
                "transcript_path": str(transcript_path),
                "user_text": user_text_meta,
                "assistant_text": final_text_meta,
                "total_tool_calls": len(all_tool_uses),
                "total_responses": len(turn.assistant_msgs),
            },
        ) as turn_span:
            _set_span_times(turn_span, turn_start_ns, turn_end_ns)
            num_responses = len(turn.assistant_msgs)

            for resp_idx, asst_msg in enumerate(turn.assistant_msgs):
                content = get_content(asst_msg)
                text_raw = extract_text(content)
                text, text_meta = truncate_text(text_raw)
                model = get_model(asst_msg)
                tool_uses = _get_tool_uses_for_msg(turn, asst_msg)

                # Timestamp for this assistant response.
                # For the first response, start = user message timestamp.
                # For subsequent responses, start = end of the last tool from previous response.
                if resp_idx == 0:
                    asst_start_ns = get_timestamp_ns(asst_msg)
                else:
                    prev_tus = _get_tool_uses_for_msg(turn, turn.assistant_msgs[resp_idx - 1])
                    if prev_tus:
                        # Use max of ALL previous tool end timestamps (handles parallel tools)
                        prev_end_times = [
                            turn.tool_result_timestamps.get(str(ptu.get("id", "")))
                            for ptu in prev_tus
                        ]
                        prev_end_times = [t for t in prev_end_times if t is not None]
                        asst_start_ns = max(prev_end_times) if prev_end_times else get_timestamp_ns(asst_msg)
                    else:
                        asst_start_ns = get_timestamp_ns(asst_msg)

                # Claude Response ends when its first tool starts executing.
                # If no tools, it ends at the next response's start or at turn end.
                if tool_uses:
                    first_tid = str(tool_uses[0].get("id", ""))
                    asst_end_ns = get_timestamp_ns(asst_msg)
                elif resp_idx + 1 < num_responses:
                    asst_end_ns = get_timestamp_ns(turn.assistant_msgs[resp_idx + 1])
                else:
                    asst_end_ns = turn_end_ns

                # Build generation output — include tool_calls so they're visible
                gen_output: Dict[str, Any] = {"role": "assistant", "content": text}
                if tool_uses:
                    gen_output["tool_calls"] = [
                        {"id": tu.get("id"), "name": tu.get("name"), "input": tu.get("input")}
                        for tu in tool_uses
                    ]

                # Generation name — numbered when multiple responses per turn
                gen_name = (
                    f"Claude Response ({resp_idx + 1}/{num_responses})"
                    if num_responses > 1
                    else "Claude Response"
                )

                # Input for the generation
                if resp_idx == 0:
                    gen_input: Any = {"role": "user", "content": user_text}
                else:
                    # Input is tool results from the previous response's tool calls
                    prev_tool_uses = _get_tool_uses_for_msg(turn, turn.assistant_msgs[resp_idx - 1])
                    if prev_tool_uses:
                        gen_input = [
                            {
                                "type": "tool_result",
                                "tool_use_id": ptu.get("id"),
                                "tool_name": ptu.get("name"),
                                "content": truncate_text(
                                    extract_tool_result_text(turn.tool_results_by_id.get(str(ptu.get("id", ""))))
                                , max_chars=2000)[0],
                            }
                            for ptu in prev_tool_uses
                        ]
                    else:
                        gen_input = {"role": "continuation"}

                with turn_span.start_as_current_observation(
                    name=gen_name,
                    as_type="generation",
                    model=model,
                    input=gen_input,
                    output=gen_output,
                    metadata={
                        "tool_count": len(tool_uses),
                        "response_index": resp_idx + 1,
                    },
                ) as gen_obs:
                    _set_span_times(gen_obs, asst_start_ns, asst_end_ns)

                # Tool observations for this response's tool_uses — preserves correct ordering
                for tu in tool_uses:
                    tid = str(tu.get("id", ""))
                    tc_name = tu.get("name") or "unknown"
                    tc_input = tu.get("input")

                    result_raw = turn.tool_results_by_id.get(tid)
                    out_str = extract_tool_result_text(result_raw)
                    out_trunc, out_meta = truncate_text(out_str)

                    # Tool starts when Claude invoked it (= assistant msg timestamp),
                    # ends when the result came back (= tool_result timestamp).
                    tool_start_ns = asst_end_ns  # right after Claude Response ended
                    tool_end_ns = turn.tool_result_timestamps.get(tid)

                    if isinstance(tc_input, str):
                        tc_input, in_meta = truncate_text(tc_input)
                    else:
                        in_meta = None

                    # Extract agent usage from toolUseResult if available
                    agent_usage: Dict[str, Any] = {}
                    agent_meta_extra: Dict[str, Any] = {}
                    if tc_name == "Agent":
                        tr_msg = turn.tool_result_msgs.get(tid)
                        if tr_msg:
                            tur = tr_msg.get("toolUseResult")
                            if isinstance(tur, dict):
                                raw_usage = tur.get("usage", {})
                                agent_usage = {
                                    "input": raw_usage.get("cache_read_input_tokens", 0) + raw_usage.get("cache_creation_input_tokens", 0) + raw_usage.get("input_tokens", 0),
                                    "output": raw_usage.get("output_tokens", 0),
                                    "total": tur.get("totalTokens", 0),
                                }
                                agent_meta_extra = {
                                    "total_tokens": tur.get("totalTokens"),
                                    "total_duration_ms": tur.get("totalDurationMs"),
                                    "total_tool_use_count": tur.get("totalToolUseCount"),
                                    "agent_id": tur.get("agentId"),
                                }

                    with turn_span.start_as_current_observation(
                        name=f"Tool: {tc_name}",
                        as_type="tool",
                        input=tc_input,
                        metadata={
                            "tool_name": tc_name,
                            "tool_id": tid,
                            "input_meta": in_meta,
                            "output_meta": out_meta,
                            **agent_meta_extra,
                        },
                    ) as tool_obs:
                        _set_span_times(tool_obs, tool_start_ns, tool_end_ns)
                        tool_obs.update(output=out_trunc, usage_details=agent_usage if agent_usage else None)

                        # For Agent tools, emit sub-agent steps as nested spans
                        if tc_name == "Agent":
                            try:
                                _emit_subagent_turns(turn_span, turn, tid, tool_obs, transcript_path)
                            except Exception as e:
                                debug(f"Sub-agent emit failed for {tid}: {e}")

# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        return 0

    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("CC_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"

    if not public_key or not secret_key:
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_and_transcript(payload)

    if not session_id or not transcript_path:
        # No structured payload; fail open (do not guess)
        debug("Missing session_id or transcript_path from hook payload; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript path does not exist: {transcript_path}")
        return 0

    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            msgs, ss = read_new_jsonl(transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            # Deterministic trace ID from session_id — all turns land in one trace
            trace_id = Langfuse.create_trace_id(seed=session_id)

            # emit turns
            emitted = 0
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted
                try:
                    emit_turn(langfuse, trace_id, session_id, turn_num, t, transcript_path)
                except Exception as e:
                    error(f"emit_turn failed for turn {turn_num}: {e}\n{traceback.format_exc()}")
                    # continue emitting other turns

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            save_state(state)

        try:
            langfuse.flush()
        except Exception:
            pass

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={session_id})")
        return 0

    except Exception as e:
        error(f"Unexpected failure: {e}\n{traceback.format_exc()}")
        return 0

    finally:
        try:
            langfuse.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())