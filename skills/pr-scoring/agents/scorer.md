---
name: scorer
description: Default reviewer. One model call per PR, structured JSON out.
mode: single
effort: high
max_diff_chars: 600000
---
Work through the bundle in this order: commit messages, the diff (correctness, security, error handling,
tests), validation (CI, tests, description), then the review conversation (was each thread valid, and was it
really fixed in code?). Keep the summary to 2-3 sentences.
