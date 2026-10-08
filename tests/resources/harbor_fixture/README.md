# Harbor fixture

Small Harbor-format tasks for `just test-harbor-template`, on a local base image
(`image/Dockerfile`, built as `karotte-harbor-fixture:1`):

- `honest-add`: an ordinary task. Its reference solution must score 1, and an
  empty submission 0.
- `forge-reward`, `forge-report-late`, `reach-out`: their "reference solutions"
  are attacks on the grading (code under test writing the reward, a process
  outliving the tests to rewrite their report, tests reaching the network). Each
  must score 0. Code under test rewriting its report while the tests still run
  is not among them: no grader of test results can stop that.

Each task's `tests/test.sh` frame and `tests/grader.py` are copied unmodified
from DeepSWE (https://github.com/datacurve-ai/deep-swe, commit
0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea), licensed under the Apache License 2.0
in `LICENSE-deep-swe`; only the block between `RUN TESTS` and `END RUN TESTS`
in `test.sh` is this fixture's own.
