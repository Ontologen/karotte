"""Run tasks from Harbor-format datasets, such as DeepSWE, on karotte.

``karotte harbor import`` reads an environment's ``harbor.toml`` and writes its
lock and data; :func:`karotte.harbor.task.register_harbor_tasks` turns the lock
into tasks; :mod:`karotte.harbor.verifier` grades them.
"""

LOCK_FILE = "harbor.lock.json"
VALIDATION_FILE = "harbor.validation.json"
