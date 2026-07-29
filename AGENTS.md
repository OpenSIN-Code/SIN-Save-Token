# Repository Agent Rules

## RTK command policy

Every shell command in this repository must be prefixed with `rtk`.

- Use `rtk git ...`, never raw `git ...`.
- Use `rtk pytest ...`, never raw `pytest ...`.
- Use `rtk curl ...`, never raw `curl ...`.
- Prefix every command in a chain separately, for example: `rtk git add . && rtk git commit -m "message"`.
- Use absolute repository paths instead of `cd`.

`CLAUDE.md` is the detailed RTK command reference.
