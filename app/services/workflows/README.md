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

## Choosing a data tool (correct data, correctly scoped)

The data a node fetches must be relevant to the request — never a global dump where a per-entity
answer is expected. Two rules:

1. **Prefer a scoped tool; if you use a global one, filter it.** A `filter` node with
   `keep: {field: "$ref"}` narrows a global result to the entity in play (e.g. grant_access's
   `filter_has` keeps only the target user's rows). `getAllUserApplicationRoles_post` returns ALL
   users regardless of its `userName` arg — so it is ALWAYS paired with a `filter_has` on
   `userName` (+ `applicationId`).

2. **Active vs all — pick by intent.** For *"does the user already have this?"* (grantable
   exclusion) use the **all-assignments** view so a role held *inactively* is still excluded and
   isn't re-offered (`getAllUserApplicationRoles_post` + filter, or `getUserAppRoleByUserId_post`).
   For *"is the access live?"* (a verify-after-write read-back, or listing usable roles) use the
   **active** view (`getActive…`). grant_access does exactly this: `fetch_user_roles` uses the
   all-view for grantable, and `do_grant.verify` uses `getActiveUserAppRolesByAppIdAndUserName_post`
   to confirm the new grant is live.

## verify (read-back after a write)

A `mutate` node may carry `verify: {tool, args, match}`. After the write succeeds, the engine calls
`tool` (args may be `"$ref"`s) and confirms a returned row matches every `match` field
(value-or-`$ref`); if none matches — or the verify errors — the run reports the change was submitted
but could NOT be confirmed, never a false success. Scope `match` to the acting entity (grant_access
matches `userName` + `roleName`) so another entity's row can't satisfy it.
