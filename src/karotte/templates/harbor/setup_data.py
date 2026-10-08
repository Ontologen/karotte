# Re-creates the tasks' data from the committed lock, keeping its image pins.
# To import anew from harbor.toml instead, run `uv run karotte harbor import`.

# /// script
# requires-python = "==3.12.*"
# dependencies = []
# ///

import subprocess


def main():
    subprocess.run(
        ["uv", "run", "karotte", "harbor", "import", "--from-lock"], check=True
    )


if __name__ == "__main__":
    main()
