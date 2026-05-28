# Timeline Refinement Instructions

You are given three files. Your task is to produce an **enriched timeline** where
every reference (`TH-*`, `TC-*`, `R-*`) is replaced with a concise, informative
summary drawn from `timeline_content.json`.

## Input files

| File | Purpose |
|---|---|
| `session.md` | Full chat exports + git index state — use as reference context |
| `timeline.md` | Chronological timeline with reference IDs — your **template** |
| `timeline_content.json` | Detailed content keyed by reference ID — your **source data** |

## How to use each file

1. **Start from `timeline.md`** — copy its structure verbatim (frontmatter, stats,
   entries). This is your output skeleton.
2. **For every bracketed reference** (e.g. `[TH-E0001-0]`, `[TC-E0003]`,
   `[R-E0005]`), look up the matching entry in `timeline_content.json` → `.content[]`
   by its `.id` field.
3. **Replace the reference** with a summary following the rules below.
4. **Consult `session.md`** only when you need broader context (e.g. to understand
   what a tool call accomplished, or what code was changed).

## Summarization rules by content type

### Thinking blocks (`TH-*`)

- **Target length:** 30–50 words
- **Max characters:** 250
- **Focus on:** key reasoning steps, decisions reached, hypotheses tested
- **Omit:** self-corrections, filler phrases ("Let me think…"), repeated reasoning
- **Format:** plain sentence(s), no bullet points

> Example:
> Before: `- Thinking: 2 blocks, 8.3s [TH-E0002-0, TH-E0002-1]`
> After: `- Thinking: 2 blocks, 8.3s — Traced payment flow difference to
>   missing webhook handler; confirmed call_email_service signature mismatch.`

### Tool calls (`TC-*`)

- **Target length:** 20–40 words
- **Max characters:** 200
- **Focus on:** what was accomplished overall; key files touched; any notable outcome
- **Group:** consecutive similar operations into one phrase (e.g. "read 5 config files")
- **Format:** brief narrative after the count

> Example:
> Before: `- Tools (47): read_file x5, edit_file x12, … [TC-E0001]`
> After: `- Tools (47): read_file x5, edit_file x12, … — Read server/MP flow,
>   then patched main.py payment handler, receipt page, and modal component.`

### Responses (`R-*`)

- **Target length:** 50–100 words
- **Max characters:** 500
- **Focus on:** the main answer, solution applied, files changed and why
- **Omit:** pleasantries, caveats, raw code blocks, formatting boilerplate
- **Format:** one short paragraph

> Example:
> Before: `- Response: 1,245 words [R-E0001]`
> After: `- Response: 1,245 words — Fixed the "Não pago" status by adding
>   a dedicated confirmation trigger for single-payment orders in main.py.
>   Updated the receipt page to show correct status icons and adjusted the
>   modal to handle installment restrictions. Also updated dicas.txt with
>   the new OAuth setup flow.`

## Output requirements

- Preserve the **exact Markdown structure** of `timeline.md` (frontmatter, headings,
  stats table, entry layout, separators).
- Do **not** add new headings or sections.
- Do **not** change user prompts (blockquoted text).
- Do **not** include raw code or large snippets.
- Keep the original indicators (block counts, tool counts, word counts) intact;
  append your summary after a ` — ` (space–em-dash–space).
- If a content entry is trivial or uninformative (e.g. a one-word thinking block),
  you may write "*(trivial)*" instead of a full summary.

## Final check

Before submitting, verify:
1. Every `[TH-*]`, `[TC-*]`, `[R-*]` reference has been replaced with a summary.
2. No summary exceeds the max character limit for its type.
3. The output is valid Markdown with no broken tables or fences.
