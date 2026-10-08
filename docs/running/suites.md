# Suites

`karotte run` runs one task with one model setup.
A suite runs many: every task it selects, with every model setup it lists, as many times as it asks.
It picks up where it left off, retries only failures that say nothing about the model, and records every attempt.

## A suite file

```toml
name = "baseline"
samples = 3                   # runs per task and model setup
tasks = ["deepswe-*"]         # globs over task ids
exclude = ["deepswe-wazero-*"]

[defaults]                    # run config fields for every run
step_time_limit_seconds = 10800
on_step_time_limit = "score"

[[models]]                    # one entry per model setup
label = "opus"
model = "anthropic/claude-opus-5-5"
reasoning_effort = "max"

[[models]]
label = "gpt"
model = "openai/gpt-5.5"
```

Each model setup is a run config: the defaults with its own fields on top, read the way `karotte run --config` reads one, API keys included.
`label` names it in the results, and `mounts` (a list of `source:target[:ro]`) adds mounts to its runs.
`run_id`, `task_id` and `transcript_file` are set per run.

## Running

```sh
uv run karotte suite run suite.toml --runtime docker --concurrency 4
```

| Option | Meaning |
|---|---|
| `--out` | Where results go. Default: `out/suites/<name>`. |
| `--runtime` | As for `karotte run`. |
| `--concurrency` | Runs at once. |
| `--max-retries` | Retries of a run that failed for infrastructure reasons. Default: 2. |
| `--prune-images` | Remove each task's image after its last run, for tasks that each bring their own. |

Each image is built once, when the first run that needs it starts, and runs are ordered by image.
Running the same command again skips every run that already has a final result, so an interrupted suite resumes.

## Outcomes and retries

Every attempt is classified from its transcript, never from an exit code:

| Outcome | What happened | Retried |
|---|---|---|
| `scored` | The task was graded. | No |
| `model_limit` | The model ran out of turns, time or context, or the provider refused its content. It scores 0. | No |
| `infra_error` | The model provider stayed down through every retry the agent made, or the sandbox never started. | Yes, up to `--max-retries` |
| `build_error` | The task's image couldn't be built, three times over. | No |
| `error` | Anything else, including a run that stopped partway. | No |

Only provider and sandbox failures are retried, because they say nothing about the model.
Retrying anything the model could have caused would resample its failures and inflate its score.

## Results

`<out>/runs/<task>/<label>/s<sample>/a<attempt>/` holds each attempt's transcript and log.
`<out>/results.jsonl` has one line per attempt: the run, the attempt, its outcome and whether it's final, the score and `verifier_status`, the error, the tokens, the duration, the transcript and the image.

`<out>/summary.json`, and `karotte suite summary <out>`, give per model setup:

- how many runs ended in each outcome, and each `verifier_status`;
- the mean score, counting every run without a score as 0;
- the mean score over scored runs alone;
- the mean score per task.

`karotte suite run` exits 0 when every run ended `scored` or `model_limit`, 1 otherwise, and 2 when the suite file is invalid.
