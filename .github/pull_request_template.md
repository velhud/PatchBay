## Summary

-

## Safety Checklist

- [ ] No secrets, local auth files, or private machine identifiers added.
- [ ] Repository scope remains explicit and local-control defaults are preserved.
- [ ] Public/tunnel behavior requires auth.
- [ ] Mutating behavior is clearly marked and tested.
- [ ] Prompt/config/log output remains redacted by default.
- [ ] CodexPro attribution remains accurate when derived behavior is touched.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
```

For connector or ChatGPT-facing changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```
