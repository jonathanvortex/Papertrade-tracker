# CLAUDE.md

- Follow SPEC.md and build in the order of its section 8. Test each step before starting the next.
- This repo only reads public data. Never send transactions, sign anything, or call trading endpoints.
- Never commit `.env`, keys, session keys, or `data/*.sqlite`.
- Parse the 18-decimal summary values with `int`/`Decimal`, never float.
- If live API data contradicts SPEC.md, stop and report it rather than guessing.
