# The base image the fixture's tasks share: a repository at /app with a
# function to implement. Fixed dates make the base commit's hash fixed too.
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends git python3 ca-certificates \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV GIT_AUTHOR_DATE="2026-01-01T00:00:00Z" GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"
RUN git init -q -b main . \
  && printf 'def add(a, b):\n    raise NotImplementedError\n\n\ndef negate(a):\n    return -a\n' > calc.py \
  && git add -A \
  && git -c user.name=fixture -c user.email=fixture@example.invalid commit -qm base
ENV GIT_AUTHOR_DATE= GIT_COMMITTER_DATE=
CMD ["/bin/bash"]
