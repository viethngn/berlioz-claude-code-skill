# Cross-Wiki Link Convention

Load whenever `/log` (or `/ingest`'s cascade phase) needs to link into a
*different* wiki than the one currently being edited.

## The `[[label/slug]]` syntax

This reuses the exact namespace-prefix mechanism already used for
`[[archive/slug]]` inside a single wiki (a `/` before the first path segment
is a namespace switch, not a literal subfolder inside the current wiki's
`wiki/` directory):

```
[[label/slug]]  →  <linked_wikis[label].path>/wiki/<slug>.md
```

`label` must match a `label` field in this wiki's `.wikirc.json`
`linked_wikis` array (see `skills/ingest/scripts/config.py`'s
`Config.linked_wikis()`). Two roles are used by `/log` specifically:

- `role: "who"` — link people mentions against the linked wiki tagged
  `role: "who"` (e.g. a colleague-profiles wiki).
- `role: "what"` — link topic/product/entity mentions against the linked
  wiki tagged `role: "what"` (e.g. a product wiki).
- `role: "generic"` — any other linked wiki that doesn't cleanly split into
  who/what; resolved by `--label`, not `--role`.

Resolution (read-only) is done by `scripts/resolve_link.py`, which shells out
to the target wiki's own `scripts/wiki_search.sh` if present, or falls back
to a plain ripgrep scan of its `wiki/` directory. **This never writes into
the linked wiki** — no page, stub, or edit is ever created there by `/log`.
A mention with no confident match is written as plain text with an
unresolved marker and reported at the end of the run instead of being forced
into a link.

## Explicit non-goals

- **No cross-wiki lint validation.** `/lint`'s `lint.py` only ever scans its
  own `wiki/` directory; teaching it to resolve `[[label/slug]]` against
  another wiki's config and file tree is out of scope here. A `[[label/slug]]`
  link will show up in `/lint`'s broken-link report for the wiki it's written
  in — **this is expected, cosmetic noise**, not a defect. Revisit only if it
  becomes genuinely annoying in practice.
- **No Obsidian cross-vault resolution.** Each wiki repo is a separate
  Obsidian vault; Obsidian has no built-in way to resolve a link into a
  different vault. `[[label/slug]]` will render as an unresolved/red link in
  both the source and (if opened) target vault. This is an accepted
  limitation, not something this feature attempts to fix.
