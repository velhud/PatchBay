## Summary

-

## Safety Checklist

- [ ] No secrets, local auth files, or private machine identifiers added.
- [ ] Repository scope remains explicit and local-first.
- [ ] Mutating behavior is clearly marked and tested.
- [ ] Prompt/config/log output remains redacted by default.

## Verification

```bash
python -m compileall .
python -m pytest tests -q
```
