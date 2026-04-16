# Nanobot Medical Research Fork - Customizations

## Purpose
This fork adds medical-research specific features to nanobot for ER clinical workflow support, including enhanced web extraction, PDF handling, and controlled tool execution.

## Customizations (verified April 2026)

**Note:** Only actual code modifications made to this fork are listed below. Differences due to the fork being behind upstream are NOT included.

### `nanobot/agent/loop.py`
- **Line 62:** `force_final_threshold: int` parameter in `_LoopHook.__init__()` - Forces final answer after N iterations
- **Line 73:** `self._force_final_threshold = force_final_threshold` - Stores threshold for iteration checking
- **Line 129:** Force final prompt injection - Injects "For research query, don't use any more tools..." when threshold reached
- **Line 76:** `self.all_tool_calls_log: list[dict] = []` - Tracks all tool calls for transparency summary
- **Lines 96-102:** Tool call tracking in `before_execute_tools()` - Logs each tool call for summary display

### `nanobot/agent/tools/web.py`
- **Line 63-70:** `_smart_truncate()` - Truncates at paragraph/sentence boundaries instead of mid-sentence
- **Line 73-87:** `_extract_pdf_text()` - PDF text extraction using PyMuPDF (fitz)
- **Line 90-105:** `_extract_meta()` - Extracts author, date, description from HTML meta tags
- **Line 108-119:** `_build_image_blocks()` - Converts images to multimodal content blocks for vision LLMs
- **Line 144-175:** `_fetch_raw()` - Tiered fetcher: curl_cffi (Chrome impersonation) → httpx fallback
- **Line 178+:** `_html_to_text()` with trafilatura - Better article extraction with trafilatura → readability fallback
- **Line 18:** `DEFAULT_SEARXNG_URL = ""` - Hardcoded SearXNG URL override for private search
- **Line 18:** Enhanced User-Agent - Full Chrome User-Agent string for better compatibility

## Dependencies (for fork features)

Install these for full fork functionality:
```bash
pip install PyMuPDF curl_cffi trafilatura
```

## Last Verified
- **Date:** April 15, 2026
- **Upstream version:** e18eab8 (main, post v0.1.5.post1)
- **Merge commit:** f082868
- **Verification status:** ✅ Passed (all fork customizations intact, 23 FORK markers)
- **Test status:** 41 passed, 3 failed (test_retry — expected: fork uses 5 retries vs upstream 3-4)

## Merge Notes

When merging upstream changes:
1. Pull fork to local: `git pull origin main`
2. Fetch upstream: `git fetch upstream`
3. Merge: `git merge upstream/main`
4. Run verification script
5. Fix any conflicts using Conflict Resolution Strategy in skill
6. Push to fork: `git push origin main`

## Related Skill
- `nanobot-fork-upstream-merge` - Complete guide to merging upstream changes while preserving these customizations
