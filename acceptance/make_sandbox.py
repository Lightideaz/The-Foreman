"""Create a fresh one-commit sandbox repository for an acceptance case."""
import subprocess
import sys
from pathlib import Path

FILES = {
    "package.json": '{"name":"sandbox","private":true,"scripts":{"test":"node test.js"}}\n',
    "src/greet.js": "module.exports = { greet: (n) => `أهلاً، ${n}` };\n",
    "test.js": (
        "const assert = require('assert');\n"
        "const { greet } = require('./src/greet');\n"
        "assert.strictEqual(greet('سارة'), 'أهلاً، سارة');\n"
        "console.log('ok');\n"
    ),
    "README.md": "# Sandbox\n",
}


def main(dest: str) -> None:
    root = Path(dest)
    if root.exists():
        sys.exit(f"refusing: {root} already exists")
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git = ["git", "-C", str(root)]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(
        git + ["-c", "user.name=The Foreman Acceptance", "-c", "user.email=acceptance@localhost",
               "commit", "-q", "-m", "sandbox: initial"],
        check=True,
    )
    print(root.resolve())


if __name__ == "__main__":
    main(sys.argv[1])
