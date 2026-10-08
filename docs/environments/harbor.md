# Harbor tasks

!!! warning "Experimental"

    The `harbor` template is experimental.
    Its functions, files and guarantees can change in any release.

The `harbor` template runs tasks from benchmarks published in the [Harbor task format](https://harborframework.com/docs/tasks), such as [DeepSWE](https://github.com/datacurve-ai/deep-swe).
Each task keeps its own image, instruction and tests.
Karotte adds its harness, the student user and its checks on top of the task's image, and grades the task with its own tests in a way the student can't interfere with.

## Opting in

```sh
karotte create-env my_benchmark --template harbor
cd my_benchmark
uv sync --extra dev
```

The template builds on the `default` template.
It removes the default's example task, since every task here runs on an image of its own.

## Importing tasks

`harbor.toml` names where the tasks come from: a git repository at a pinned commit, or a directory of the environment's own.

```toml
[[source]]
name = "deepswe"
repo = "https://github.com/datacurve-ai/deep-swe"
commit = "0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea"
path = "tasks"
include = ["*"]
exclude = []
```

```sh
uv run karotte harbor import
```

The import reads every task, pins each task's image to its digest, and writes:

- `src/environment/tasks/harbor/harbor.lock.json`: the tasks it accepted, with their pinned images, and the reason for every task it refused.
  Commit it.
  A later import shows what changed upstream as the lock's diff.
- `root_data/harbor/<task id>/`: each task's instruction and tests.
  They are root-only in the image.

The reference solutions are never copied into the environment or an image.
They stay in the source's clone under `~/.cache/karotte/harbor/`, and only validation mounts them.
On a fresh checkout, `uv run setup_data.py` re-creates the data from the lock without contacting the registries.

Task ids are `<source name>-<task directory>`, for example `deepswe-anko-default-function-arguments`.

## What is supported

Karotte runs a strict subset of the format and refuses every other task with the reason:

| Harbor | In Karotte | Refused when |
|---|---|---|
| `instruction.md` | The step's instructions, unchanged. | – |
| `environment.docker_image` | The task's base image, pinned by digest. | The task builds its image from a Dockerfile or compose file. |
| `cpus`, `memory_mb`, `storage_mb` | The sandbox's size. The student gets exactly `memory_mb`. | `gpus` is not 0. |
| `agent.timeout_sec` | `step_time_limit_seconds`, with `on_step_time_limit = "score"`, set in the run config. A run without them is refused. | – |
| `network_mode` | No network for the agent or the verifier. | Anything but `no-network`. |
| `verifier.environment_mode` | The grading below. | `shared`. |
| `tests/` | DeepSWE's verifier frame, pinned by hash. | Any other `test.sh` frame or `grader.py`, or a `tests/Dockerfile` that does more than copy the tests. |
| `[[verifier.collect]]` | Run as the student after it finishes. | – |
| `steps/`, `mcp_servers`, unknown fields | – | Always. |

## Validating

Before trusting a score, check that each task grades as it must, on the runtime you'll use:

```sh
uv run karotte harbor validate --runtime docker --concurrency 4
```

For each task this runs two scripted runs, with the fake model, through the real grading path:

- the reference solution, applied through the student's own shell, must score 1;
- an empty submission must score 0, with its tests having run rather than crashed.

The results go to `harbor.validation.json` next to the lock, with the image digest and content they were checked against.
A task that failed refuses every run but another validation, so it can't slip into a suite's results.
A task whose runs couldn't be graded (its image didn't build, say) gets no verdict, and neither does one validated against an image or tests the lock no longer pins.
Validation results depend on the runtime, so validate on the one you'll run with.

## Running

Run tasks with a [suite](../running/suites.md).
Give the agent the task's own time limit and let its work be graded when the time is up:

```toml
name = "deepswe"
tasks = ["deepswe-*"]

[defaults]
step_time_limit_seconds = 10800
on_step_time_limit = "score"

[[models]]
label = "opus"
model = "anthropic/claude-opus-5-5"
```

## Images built on a task's base

A task's image is built from the environment's `Containerfile` with the task's image as its base, one image per distinct base.
Supported bases are Debian and Ubuntu, glibc, with util-linux 2.38 or later.
The build stops with the reason on anything else.

Compared with an image built on the default base:

- Root's home, and Karotte's private files, are in `/karotte`, mode 0700.
  `/root` stays the base's home, readable by the student, because the base's toolchains live there.
- The student is uid 1000 and the grader uid 1001.
  The build fails if the base already uses either.
- The task's workdir, such as `/app`, belongs to the student.
  A root-only copy of it as the base had it is kept for grading.
- Anything the base left world-writable, or owned by uid 1000 or 1001, is given back to root.
- The harness never sees variables that make an interpreter load code from elsewhere, such as `PYTHONPATH`, even when the base sets them.
  The student's shell and the tests get the base's environment, from a snapshot taken before Karotte changed anything.
- There are no venvs: the student uses the task's own toolchains.

## How a task is graded

When the student finishes:

1. Every student process is killed, and its files outside the task's workdir deleted.
2. The task's collect hooks run as the student, offline, making a patch of its work.
   Git reads the student's repository config only as the student.
   The patch is copied somewhere root-only, refusing links and files someone else owns.
3. Every file the student owns is deleted.
4. The task's workdir is restored from the root-only copy and given to the grader.
   The tests and the patch are staged root-owned and read-only.
5. The tests run as the grader, in a network namespace of their own where only loopback exists, under memory, process and disk limits and the task's verifier timeout.
   Nothing they exec can gain privileges, whatever setuid binaries the base ships.
6. Only the test reports the task names are copied, root-owned, into a fresh directory.
7. `grader.py grade` runs as the grader on those copies alone: with an interpreter checked to be root's, without site processing (`-I -S`), without the task's environment, and with the workdir closed to it, so nothing the student wrote can be imported there.
8. The reward is read after every grader process is confirmed gone.

| Outcome | Score | `verifier_status` |
|---|---|---|
| The tests pass | The task's reward | `ok` |
| The collect hooks fail | 0 | `collect_failed` |
| The tests run past the verifier timeout | 0 | `verifier_timeout` |
| The tests produced none of their reports | 0 | `tests_not_run` |
| Grading reports no valid reward | 0 | `verifier_error` |
| The student, or code it wrote, filled the disk quota so grading couldn't write | 0 | (`misbehavior` in the metadata) |
| Anything Karotte itself couldn't do: stage the tests, start the grader, cut off its network | The run is an error | – |

Code under test runs in the same process as its test framework, so it can forge that framework's reports while the tests run.
For example, it can start a process that keeps the tests' output open, which keeps them running, and rewrites their report after the framework writes it.
No grader of test results can prevent that, Harbor's included.
What Karotte does prevent:

- code under test writing the reward or tampering with the grading logic, which runs in a phase of its own on clean copies of the reports;
- anything it starts outliving the tests: every grader process is killed before the reports are copied;
- anything from the student's own session reaching the grading, since all of it is deleted first.

DeepSWE's grader records signals of tampering, such as a model-added `TestMain`, in the scoring metadata, where a suite's results can be checked for them.

## Limitations

- Restoring the task's workdir for grading writes it into the disk quota the tests share.
  A task whose repository is large next to its `storage_mb`, or a host short of disk, leaves the tests less room; validation on the same host shows it.
- A task that hasn't been validated still runs; its transcript records `validated: null`.

- The student shares the sandbox's network with the harness, so a task whose own tests bind a port the harness listens on (the tool server's 8080) fails them while the student works.
  Grading is not affected: the tests then run in a network namespace of their own.
- DeepSWE's images are amd64 only.
  On Apple silicon they run under emulation, which is slow, and Firecracker needs an x86_64 host.
- Each task image is the base plus Karotte's layers, which aren't shared between bases.
  Pass `--prune-images` to remove each image after its last run.
