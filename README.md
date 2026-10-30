# greptile-sub

A self-hosted GitHub PR reviewer that works with any OpenAI-compatible chat API. It's built to run
on [claude-bridge](https://github.com/alicin/claude-bridge), which serves Claude from your
logged-in Claude Code CLI, but anything that supports function calling and `json_schema`
responses will do.

There's no vector index. The model explores the checked-out PR through three read-only tools, the
way a reviewer would.

```
GitHub ──gh──▶ greptile-sub ──OpenAI API──▶ claude-bridge ──▶ claude CLI
                   │  ▲
         list_files│  │results
         grep      ▼  │
         read_file  PR checkout (temp dir)
```

## How a review runs

1. `gh` fetches the PR, its diff, and `AGENTS.md` / `CLAUDE.md` / `.greptile-sub.md` from the
   **base** branch as review guidelines (a PR can't rewrite its own rules).
2. The PR head is shallow-cloned into a throwaway temp dir.
3. **Review pass:** an agent loop. The model calls `list_files`, `grep` and `read_file`, which
   greptile-sub executes against the checkout and returns as `role: "tool"` messages. When it stops
   calling tools, a final request with `response_format: json_schema` collects the findings.
4. **Verify pass:** the same loop, re-checking each finding against the code and dropping
   speculative, already-handled or wrong-line ones.
5. Findings on changed lines become inline comments; the rest go in the review body. With `--post`
   it's posted as one `COMMENT` review; without it, it's only printed.

**Untrusted input.** PR content is treated as untrusted. The tools only read, and every path is
resolved and checked against the checkout root, so `../`, absolute paths and symlinks planted in
the PR can't reach anything else on disk. `.git` is hidden. The model never talks to GitHub; only
greptile-sub's own code posts, and only what fits the schema.

## Learning and dedup

Everything greptile-sub posts carries a hidden `<!-- greptile-sub -->` marker, since it posts as
your own account. `learn` (also run on every `watch` poll) checks each undecided comment. The
first signal that applies wins:

| Signal | Outcome |
| --- | --- |
| comment deleted, or more 👎 than 👍 | dismissed |
| a new human reply, judged by `GREPTILE_SUB_JUDGE_MODEL` | addressed / acknowledged / dismissed |
| 👍 | acknowledged |
| the commented line changed in a later commit | addressed |
| PR closed or merged with no response | ignored |

The latest 25 **dismissed** findings for a repo go into both the review and verify prompts as
"don't report this kind of issue again". `lessons OWNER/REPO` shows them.

Before re-reviewing a PR, the reviewer is shown the comments greptile-sub already left on it
and told not to repeat them. Findings within 3 lines of a current (not outdated) earlier
comment with a similar title are also dropped mechanically. A re-review that finds nothing new
isn't posted.

## Use

Start claude-bridge (see its README), then:

```bash
uv sync
export GREPTILE_SUB_API_KEY=<claude-bridge's API_KEY>
.venv/bin/greptile-sub review alicin/wamove#3            # dry run: print the review
.venv/bin/greptile-sub review alicin/wamove#3 --post     # post it to the PR
.venv/bin/greptile-sub watch alicin/wamove --post        # poll every 2 min, review new PRs
.venv/bin/greptile-sub learn alicin/wamove               # collect feedback now
.venv/bin/greptile-sub lessons alicin/wamove             # what it has learned
.venv/bin/python -m unittest discover tests              # offline tests, no model or GitHub calls
```

`watch` skips drafts and reviews each PR once; `--on-push` re-reviews new head commits.

| Env var | Default |
| --- | --- |
| `GREPTILE_SUB_BASE_URL` | `http://127.0.0.1:8000/v1` (claude-bridge) |
| `GREPTILE_SUB_API_KEY` | `none` |
| `GREPTILE_SUB_MODEL` | `claude-sonnet-5-5` (review pass) |
| `GREPTILE_SUB_VERIFY_MODEL` | `claude-sonnet-5-5` |
| `GREPTILE_SUB_JUDGE_MODEL` | `claude-haiku-5-5` (classifies replies) |
| `GREPTILE_SUB_LLM_TIMEOUT` | `900` seconds per request |
| `GREPTILE_SUB_DB` | `~/.local/state/greptile-sub/reviews.db` |

Requests: a review is one request per tool step, plus one for the JSON answer, for each pass.
That's typically 5-15 requests, plus one per new reply when learning. Through claude-bridge, each
request is a separate `claude -p` run against your plan.

## License

MIT, see [LICENSE](LICENSE).
