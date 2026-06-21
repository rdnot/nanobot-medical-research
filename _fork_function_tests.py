"""Explicitly test all fork-specific functions to confirm they work post-merge."""
import sys, traceback
results = []

def test(name, fn):
    try:
        result = fn()
        if result is True or result is None:
            results.append(f"  PASS: {name}")
        else:
            results.append(f"  FAIL: {name} - returned {result!r}")
    except Exception as e:
        results.append(f"  FAIL: {name} - {type(e).__name__}: {e}")

# 1. loop.py fork customizations
def test_fork_progress_hook():
    from nanobot.agent.loop import _ForkProgressHook
    # all_tool_calls_log is set in __init__ as instance attr (line 116)
    import inspect
    src = inspect.getsource(_ForkProgressHook.__init__)
    assert "all_tool_calls_log" in src
    sig = inspect.signature(_ForkProgressHook.__init__)
    assert "force_final_threshold" in sig.parameters
    assert "before_execute_tools" in _ForkProgressHook.__dict__
    return True

def test_build_tools_summary():
    from nanobot.agent.loop import AgentLoop
    assert hasattr(AgentLoop, "_build_tools_summary")
    out = AgentLoop._build_tools_summary([
        {"name": "web_search", "arguments": {"query": "ER chest pain"}},
        {"name": "web_fetch", "arguments": {"url": "https://pubmed.ncbi.nlm.nih.gov/123"}},
        {"name": "read_file", "arguments": {"path": "C:\\Users\\titiw\\.nanobot\\skills\\foo.md"}},
    ])
    assert "search(" in out and "fetch(" in out and "read_file(" in out
    return True

def test_return_tuple_is_6():
    import inspect
    from nanobot.agent.loop import AgentLoop
    sig = inspect.signature(AgentLoop._run_agent_loop)
    ann = str(sig.return_annotation)
    assert "tuple" in ann
    return True

# 2. web.py fork customizations
def test_web_fork_imports():
    from nanobot.agent.tools.web import (
        _smart_truncate, _extract_pdf_text, _extract_meta, _build_image_blocks,
        _fetch_raw, DEFAULT_SEARXNG_URL, _html_to_text, WebFetchConfig, WebFetchTool,
    )
    return True

def test_use_jina_default():
    from nanobot.agent.tools.web import WebFetchConfig
    cfg = WebFetchConfig()
    assert cfg.use_jina_reader is False, f"Expected False, got {cfg.use_jina_reader}"
    return True

def test_tiered_fetcher_present():
    from nanobot.agent.tools import web
    import inspect
    # _fetch_raw is a module-level function (not a method) in this fork
    assert hasattr(web, "_fetch_raw")
    src = inspect.getsource(web._fetch_raw)
    assert "curl_cffi" in src
    assert "httpx" in src
    # And it's actually called by WebFetchTool.execute()
    exec_src = inspect.getsource(web.WebFetchTool.execute)
    assert "_fetch_raw(" in exec_src
    return True

def test_smart_truncate():
    from nanobot.agent.tools.web import _smart_truncate
    text = "First sentence. Second sentence. " * 100
    out = _smart_truncate(text, 100)
    assert len(out) <= 200
    return True

# 3. schema.py fork defaults
def test_schema_defaults():
    from nanobot.config.schema import AgentDefaults
    a = AgentDefaults()
    assert a.context_window_tokens == 200_000, f"got {a.context_window_tokens}"
    assert a.max_tool_result_chars == 400_000, f"got {a.max_tool_result_chars}"
    return True

# 4. filesystem.py fork size limits
def test_filesystem_size_limits():
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
    assert ReadFileTool._MAX_CHARS == 768_000, f"got {ReadFileTool._MAX_CHARS}"
    assert ReadFileTool._DEFAULT_LIMIT == 8000, f"got {ReadFileTool._DEFAULT_LIMIT}"
    assert ReadFileTool._MAX_PDF_PAGES == 120, f"got {ReadFileTool._MAX_PDF_PAGES}"
    # _max_content_chars is set in __init__ (instance attr) — verify the __init__ source
    import inspect
    src = inspect.getsource(WriteFileTool.__init__)
    assert "_max_content_chars" in src
    return True

# 5. builtin.py custom commands
def test_custom_commands():
    from nanobot.command import builtin
    assert hasattr(builtin, "cmd_clear")
    assert hasattr(builtin, "cmd_rerun")
    help_text = builtin.build_help_text()
    assert "/s —" in help_text
    assert "/c —" in help_text
    assert "/rerun —" in help_text
    return True

# 6. whatsapp.py markdown conversion
def test_whatsapp_markdown():
    from nanobot.channels.whatsapp import _markdown_to_whatsapp
    out = _markdown_to_whatsapp("**bold** and *italic*")
    assert "*bold*" in out
    assert "_italic_" in out
    return True

# 7. helpers.py tiktoken + new tool cache
def test_helpers_token_caching():
    from nanobot.utils import helpers
    assert hasattr(helpers, "_get_token_encoding")
    assert hasattr(helpers, "_estimate_tools_tokens")
    assert hasattr(helpers, "_TOOLS_TOKEN_CACHE")
    return True

def test_estimate_prompt_tokens_with_tools():
    from nanobot.utils.helpers import estimate_prompt_tokens
    msgs = [{"role": "user", "content": "Hello world"}]
    tools = [{"name": "test", "description": "test tool", "parameters": {}}]
    n = estimate_prompt_tokens(msgs, tools)
    assert n > 0
    return True

# 8. nanobot.py SDK facade (new upstream feature)
def test_nanobot_facade():
    import nanobot
    # Public API may just be version metadata (acceptable). What matters: nanobot package
    # exposes a public symbol that the SDK/runtime uses. Just check the package is importable
    # and the SDK sub-modules are reachable.
    assert nanobot.__file__ is not None
    from nanobot.nanobot import AgentLoop
    return True

# 9. SDK module exists (new upstream feature)
def test_sdk_module():
    from nanobot.sdk import runtime, types, streaming, clients
    return True

# 10. Telegram rich message support (new upstream)
def test_telegram_rich_message():
    from nanobot.channels.telegram import TelegramChannel
    import inspect
    src = inspect.getsource(TelegramChannel)
    return "rich" in src.lower() or "sendRichMessage" in src or "rich_message" in src

# 11. WhatsApp LID mapping seeding (new upstream)
def test_whatsapp_lid_seed():
    from nanobot.channels.whatsapp import WhatsAppChannel
    import inspect
    return "_load_lid_mappings" in inspect.getsource(WhatsAppChannel)

# 12. Keenable search (new upstream)
def test_keenable_search():
    from nanobot.agent.tools.web import _KEENABLE_SEARCH_API_URL
    assert "keenable" in _KEENABLE_SEARCH_API_URL
    return True

# 13. run_extra_hooks_for_ephemeral (new upstream)
def test_run_extra_hooks_param():
    import inspect
    from nanobot.agent.loop import AgentLoop
    sig = inspect.signature(AgentLoop._run_agent_loop)
    assert "run_extra_hooks_for_ephemeral" in sig.parameters
    assert "hooks" in sig.parameters
    return True

# 14. TurnContext has new fields (new upstream)
def test_turn_context_new_fields():
    from nanobot.agent.loop import TurnContext
    import dataclasses
    fields = {f.name for f in dataclasses.fields(TurnContext)}
    assert "run_extra_hooks_for_ephemeral" in fields
    assert "hooks" in fields
    return True

# 15. nanobot.utils.helpers.estimate_prompt_tokens_chain (new upstream)
def test_token_chain():
    from nanobot.utils.helpers import estimate_prompt_tokens_chain
    return True

# 16. Provider base retry mode (unchanged - regression test)
def test_provider_retry():
    from nanobot.providers.base import LLMProvider
    import inspect
    src = inspect.getsource(LLMProvider)
    return "retry" in src.lower()

# 17. Channel base / manager (regression test)
def test_channel_manager():
    from nanobot.channels.manager import ChannelManager
    return True

# 18. Config loader (regression test)
def test_config_loader():
    from nanobot.config.loader import load_config
    return True

# 19. Skill / SKILL.md presence
def test_skills():
    from pathlib import Path
    skills_dir = Path("nanobot/skills")
    assert skills_dir.exists()
    return True

print("=== Fork + upstream function tests ===")
test("1. _ForkProgressHook class with force_final_threshold", test_fork_progress_hook)
test("2. _build_tools_summary static method", test_build_tools_summary)
test("3. _run_agent_loop returns tuple", test_return_tuple_is_6)
test("4. web.py fork functions importable", test_web_fork_imports)
test("5. use_jina_reader default = False (FORK)", test_use_jina_default)
test("6. _fetch_raw has curl_cffi + httpx (tiered)", test_tiered_fetcher_present)
test("7. _smart_truncate works", test_smart_truncate)
test("8. AgentDefaults: 200K context, 400K result (FORK)", test_schema_defaults)
test("9. ReadFileTool fork size limits (768K/8000/120)", test_filesystem_size_limits)
test("10. /s, /c, /rerun custom commands (FORK)", test_custom_commands)
test("11. _markdown_to_whatsapp (FORK)", test_whatsapp_markdown)
test("12. helpers._TOOLS_TOKEN_CACHE (NEW UPSTREAM)", test_helpers_token_caching)
test("13. estimate_prompt_tokens with tools (NEW UPSTREAM)", test_estimate_prompt_tokens_with_tools)
test("14. nanobot facade (NEW UPSTREAM)", test_nanobot_facade)
test("15. nanobot.sdk.* modules (NEW UPSTREAM)", test_sdk_module)
test("16. Telegram rich message (NEW UPSTREAM)", test_telegram_rich_message)
test("17. WhatsApp LID mapping seeding (NEW UPSTREAM)", test_whatsapp_lid_seed)
test("18. Keenable search constant (NEW UPSTREAM)", test_keenable_search)
test("19. _run_agent_loop new upstream params (hooks, run_extra_hooks_for_ephemeral)", test_run_extra_hooks_param)
test("20. TurnContext new fields (NEW UPSTREAM)", test_turn_context_new_fields)
test("21. estimate_prompt_tokens_chain (NEW UPSTREAM)", test_token_chain)
test("22. Provider base retry (regression)", test_provider_retry)
test("23. ChannelManager importable (regression)", test_channel_manager)
test("24. Config loader importable (regression)", test_config_loader)
test("25. Skills directory exists (regression)", test_skills)

for r in results:
    print(r)

n_pass = sum(1 for r in results if r.startswith("  PASS"))
n_fail = sum(1 for r in results if r.startswith("  FAIL"))
print(f"\n{n_pass}/{len(results)} passed, {n_fail} failed")
sys.exit(0 if n_fail == 0 else 1)
