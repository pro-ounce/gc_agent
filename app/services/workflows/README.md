# Authoring workflows

A workflow is **data, not code**. Drop a `*.json` file in this directory (or author one on the
admin screen) and the loader (`workflow.py` → `from_dict` / `load_dir`) translates it into the
typed node graph and registers it in `REGISTRY`. The chat engine picks it up automatically —
its `trigger` regex opens it, and it runs node-by-node with a shared `data` bag passed between
nodes. No Python, no bespoke handler.

## Shape

```json
{
  "id": "grant_access",
  "title": "Grant application access",
  "trigger": "\\bgrant .*access\\b",
  "start": "ask_user",
  "nodes": [ { "id": "...", "type": "...", ... } ]
}
```

`id` unique · `trigger` a regex that opens the workflow · `start` the first node id · `nodes`
the graph. The loader validates that `start` and every edge point at a real node.

## Node types

| type      | purpose                          | key fields |
|-----------|----------------------------------|-----------|
| `ask`     | prompt the user for a value      | `field`, `prompt`, `next`; options via `options_from` (a data key), `label_key`, `value_key`, or `static` (fixed list); `multi`, `optional` |
| `fetch`   | call a read tool → stash data    | `tool`, `args` (values may be `"$field"` = pull from data), `as_key`, `next` |
| `filter`  | shape a list                     | `source`, `as_key`, `keep` ({field: value-or-`$ref`}), `exclude_source` + `on` (drop rows whose `on` appears in that list), `next`. Sets `<as_key>_empty` = `Y`/`N` |
| `branch`  | route on a collected value       | `field`, `cases` ({value: node id}), `default` |
| `confirm` | summary + gate the write         | `summary` ([label, data-field] rows), `next`. User can `confirm`, `cancel`, or `change <step>` |
| `mutate`  | call a write tool                | `tool`, `arg_map` ({tool arg: data field}), `next`. Registry resolvers turn names→ids |
| `say`     | terminal message, ends the run   | `text` |

## Data passing

- A `fetch`/`filter` stores its result under `as_key`; later nodes read it (`options_from`, `source`, or a `"$field"` arg).
- An `ask` that resolves an option also stores `<field>_label` for clean summaries.
- Everything a user answers lands in the shared `data` bag keyed by the node's `field`.

`grant_access.json` in this directory is the worked example (every node type, a two-input
filter, a branch, confirm-gated mutate). Run `python -m app.services.workflow` for an offline
walk-through with a stub backend.
