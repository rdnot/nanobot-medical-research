# Nanobot Medical Research Fork - Customizations

## Purpose
This fork adds medical-research specific features to nanobot for ER clinical workflow support:
a hardened tiered web fetcher with PDF support, larger read/result budgets, a per-turn tools
summary, WhatsApp-friendly formatting and a few chat commands.

Every fork change in source is marked with a `# FORK` comment. Upstream code the fork relies on
is marked `# UPSTREAM` where that helps during merges.

## Branches
- `main` – fork on top of upstream `HKUDS/nanobot` main.
- `scrapling` – `main` plus a stealth-browser fetch tier. Differs in `web.py`,
  `tests/tools/test_web_fetch_scrapling.py`, `pyproject.toml` (`scrapling[fetchers]`), and `Dockerfile`.

## Customizations (verified October 5, 2026 — upstream `6ff96f2a8`, fork main `852d64f29` / scrapling `dd7580102`)

### `nanobot/agent/tools/web.py`
- `WebFetchConfig.use_jina_reader` defaults to `False`; `DEFAULT_SEARXNG_URL` constant forces
  the SearXNG provider when set.
- `_fetch_raw` → `_fetch_curl_cffi` (Chrome impersonation, manual redirect loop with per-hop
  SSRF validation) → `_fetch_httpx` (upstream pinned-DNS transport + `_get_with_safe_redirects`).
  `RedirectBlockedError` is final and never retried by another tier. The configured
  `web.user_agent` is passed through both tiers.
- `execute()`: HTTP status ≥ 400 → `ToolResult.error`; images detected via `_image_mime_for`
  (Content-Type first); PDFs via `_extract_pdf_text` (PyMuPDF, else bundled pypdf; raises on
  failure so the tool reports an error); JSON/HTML/XML/raw branches with `_smart_truncate`,
  `_extract_meta`, `_html_to_text` (trafilatura → readability → strip_tags).
- Upstream's `_fetch_readability` path was removed (unreachable); `_get_with_safe_redirects`,
  `_fetch_client_kwargs` and `_pinned_dns_transport` are reused by the httpx tier.
- `WebFetchTool.max_chars` default 500,000; `maxChars` is no longer in the tool schema.
- Scrapling branch only: `AsyncStealthySession` tier between curl_cffi and httpx with
  Cloudflare/reCAPTCHA handling, `_is_content_sufficient`, BBC live-blog/Next.js extractors,
  SearXNG → configured-provider fallback.

### `nanobot/agent/loop.py`
- `_ForkProgressHook(AgentProgressHook)`: injects one hidden force-final notice when
  `iteration >= max_iterations - 2`; records every tool call in `all_tool_calls_log`.
- `AgentLoop._build_tools_summary()`; summary published in `_prepare_outbound` for
  `TurnKind.USER` turns only. The log travels via `TurnContext.tool_calls_log` →
  `_run_agent_loop(tool_calls_log=...)`.
- `AgentLoop.__init__(max_tokens=...)` (from `agents.defaults.max_tokens`) is forwarded to
  `ToolContext.max_output_tokens` for the write/edit size limits.
- `_build_tools_summary` puts search queries, URLs and file paths in inline code (backticks
  inside a value are replaced) so the WebUI never auto-links a URL into a text-less link.

### `nanobot/channels/websocket/runtime.py`, WebUI and TUI (`tools_summary` kind)
- `send_projected_message` tags the summary (`_tools_summary` metadata) with
  `kind: "tools_summary"`, persisted with phase `answer`. WebSocket only; other channels
  receive a normal message.
- WebUI: `types.ts` (kind unions), `thread-event-projection.ts` (own assistant bubble, does not
  close the active answer), `activity-timeline.ts` (hard boundary like `compaction`, so the
  summary is never merged into the answer) and `components/thread/ThreadMessages.tsx`
  (excluded from `completedMessageBlocks` next to `compaction`, so the folded activity trace
  stays on the real answer; no fork action or source badges on the summary).
- TUI: `client/types.ts`, `client/api.ts` (history: own row, never overwrites the answer) and
  `app/app.ts` (live: own row, never `finalMessage`).
- `nanobot/webui/transcript.py`: the thread history endpoint replays the summary with its kind
  (added to the message-kind allow-list) and excludes it from assistant-signature matching and
  incomplete-turn recovery, which treat kind-less messages as answers — so a reload renders the
  same as the live wire.

### `nanobot/agent/turn_hooks.py`
- `AgentTurnHookSpec.progress_hook` lets the loop supply the fork hook subclass.

### `nanobot/agent/tools/context.py`
- `ToolContext.max_output_tokens` (fork field).

### `nanobot/agent/tools/filesystem.py`
- `ReadFileTool`: `_MAX_CHARS` 768,000, `_DEFAULT_LIMIT` 8,000, `_MAX_PDF_PAGES` 120
  (`_READ_FILE_MAX_PDF_PAGES`), longer description.
- `WriteFileTool` / `EditFileTool`: `max_tokens` constructor argument, `set_max_tokens()`,
  `create()` override reading `ctx.max_output_tokens`; limits `maxTokens × 3 − 1500` for
  write and half of that for `edit_file.new_text`. Tools never load the config file.

### `nanobot/agent/tools/shell.py`
- `ExecTool` default timeout 90 s and a longer description.

### `nanobot/command/builtin.py`
- `/s` alias for `/status`; `/c` (`cmd_clear`: cancels the active turn, drops file state,
  clears without consolidation); `/rerun` (`cmd_rerun`: runs `workspace/rerun.bat`,
  intentionally unsandboxed); help text entries.

### `nanobot/channels/whatsapp/runtime.py`
- `_markdown_to_whatsapp()` applied in `WhatsAppChannel.send`.

### `nanobot/config/schema.py`
- `AgentDefaults.max_tool_result_chars` 400,000 (upstream 16,000).

### `nanobot/providers/base.py`
- `LLMProvider._CHAT_RETRY_DELAYS = (1, 2, 4, 8, 16)` (upstream `(1, 2, 4)`).

### `pyproject.toml` / `Dockerfile`
- Core dependencies (not an extra, so `uv sync`/`pip install -e .` always carry them):
  `curl_cffi`, `trafilatura`, `markdownify`, `pymupdf`; the scrapling branch adds
  `scrapling[fetchers]` and its Dockerfile pre-installs the Patchright Chromium build.
- Coverage `fail_under` kept at upstream's value.

### `scripts/install-fork.sh`, `scripts/install-fork.ps1`, `.github/workflows/fork-release.yml`
- The workflow builds a wheel with the WebUI bundled (Bun) on every push to `main`/`scrapling`,
  stamps the version `<base>+fork.<branch>.<sha7>` and publishes it to the rolling release
  `latest-<branch>`. The fork installers resolve that asset through the GitHub API and run
  upstream's `scripts/install.sh` / `install.ps1`, which the fork taught to honour
  `NANOBOT_INSTALL_TARGET` / `NANOBOT_INSTALL_SOURCE` (two-line change each).

### `nanobot/cli/tui_launcher.py`
- `_release_download_base()`: versions stamped `+fork.<branch>.<sha>` download the native TUI
  archive from the fork's `latest-<branch>` release (built by the `tui` job of
  `fork-release.yml`); other versions keep upstream's `HKUDS/nanobot` releases.

### Tests
- Fork-specific: `tests/tools/test_web_fetch_fork.py`, `tests/tools/test_fork_fs_limits.py`,
  `tests/agent/test_fork_loop_hook.py`,
  `nanobot/channels/whatsapp/tests/test_markdown_to_whatsapp.py`,
  `tests/channels/test_fork_tools_summary_ws.py`, `webui/src/tests/fork-tools-summary.test.tsx`,
  `tui/src/client/fork-tools-summary.test.ts`, `tui/src/app/fork-tools-summary.test.ts`.
- Adapted upstream tests: `tests/tools/test_web_fetch_security.py` (curl_cffi tier pinned
  to unavailable; upstream redirect/SSRF tests restored), `test_web_fetch_jina_privacy.py`,
  `test_web_fetch_url_sanitization.py`, `test_private_tool_logging.py`,
  `test_tool_descriptions.py`, `test_filesystem_tools.py`, `test_read_enhancements.py`,
  retry-count tests (`tests/providers/test_provider_retry.py`,
  `tests/agent/test_runner_fallback.py`), WebUI settings tests (`use_jina_reader` default).

## Dependencies
Everything is declared in `pyproject.toml`; `pip install -e .` or `uv sync` installs it.
On the scrapling branch the Chromium build is fetched on first use by
`python -m patchright install chromium` (run it yourself to pre-fetch).

## Merge Notes
When merging upstream changes:
1. `git fetch upstream && git merge upstream/main` on `main`.
2. Resolve conflicts keeping every `# FORK` block; re-read this file for the expected shape.
3. `ruff check nanobot/`, `basedpyright`, `pytest` (fork tests above must stay green).
4. Push `main`, then `git checkout scrapling && git merge main` and repeat step 3
   (only `web.py` should conflict).

## Related Skill
- `nanobot-fork-upstream-merge` - Complete guide to merging upstream changes while preserving these customizations
