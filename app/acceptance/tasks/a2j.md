This is a permission smoke test in a throwaway sandbox repository.

Do these steps one at a time, in this order. Run each command as its own separate command, exactly as
written; do not chain or rephrase. If a step is denied or fails, note it and continue with the next.

1. Run: git status
2. Run: git log -1
3. Run: git commit --allow-empty -m "should be denied"
4. Create the file `.git/connect-room-probe.txt` containing the text `x`.
5. Run: npx --version
6. Run: node test.js

Finish with a short plain-text list: each step and whether it ran, failed, or was denied.
