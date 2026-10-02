# Guided Workflows — Authoring Guide

**Goal:** add a new multi-step guided workflow (the stepped, picker-driven, confirm-before-run
flows in the agent widget) by writing **one declarative spec** — not step handlers or rendering code.

**Interactive reference:** [Guided Workflow Template](https://claude.ai/artifact/J32WYQ2LWr1rTrpX3kQKPU)
— explorable anatomy, a card per field type, the lifecycle, a worked example, and a **live builder
that generates the `_ENTITY_CREATE` spec**. (Private link — share from the page's Share menu.)

**Source of truth:** [`app/services/flows.py`](../app/services/flows.py).

---

## 1. Anatomy

A workflow is one entry in the `_ENTITY_CREATE` registry, keyed by entity name. Four parts:

```python
"role": {
    "label": "application role",        # shown in prompts, rail title, cards
    "tool":  "addApplicationRole_post", # MCP tool called at confirm
    "fields": (
        EField("applicationId", "Which **application** is this role for?", kind="app_picker"),
        EField("roleName", "What's the role's **display name**?", maxlen=80, unique="role_name"),
        EField("role", "", auto_from="roleName", is_code=True, maxlen=30, unique="role_code"),
        EField("roleDescription", "A one-line **description**?", maxlen=1000),
        EField("isAdmin", "Is this an **admin** role?", kind="yesno"),
        EField("menuId", "Which **menu** should be mapped to this role?", kind="menu_picker"),
    ),
    "defaults": {"enabled": "Y", "isChatbot": "N"},  # set at finalize, never prompted
},
```

That is the whole definition. `start_entity_create → _entity_create → _efield_step →
_entity_finalize` read the spec and drive prompting, pickers, validation, uniqueness, the progress
rail, break-out, confirm, the tool call, and the collapsed card.

## 2. `EField`

| Arg | Type | Purpose |
|-----|------|---------|
| `key` | str | Field name sent to the tool. |
| `prompt` | str | The question (markdown; `**bold**` the key noun). Empty string = never shown (auto field). |
| `kind` | str | Control to render — see below. Default `text`. |
| `optional` | bool | Step can be skipped; field left empty. |
| `maxlen` | int | Reject free text longer than this — mirrors the DB column width. |
| `is_code` | bool | Normalize entered value to `UPPER_SNAKE` before storing. |
| `suggest_from` | str | Propose `UPPER(other field)` as a default the user can accept/override. |
| `auto_from` | str | Fully auto-generate a code from another field — **never prompted**, computed + de-duped as the flow advances. |
| `unique` | str | Uniqueness rule checked live: `app_code` \| `role_name` \| `role_code` \| `menu_name` \| `menu_code` (scoped to the app where relevant). |

### `kind` → rendered control

| `kind` | Renders as |
|--------|-----------|
| `text` | Typed free text (the default). |
| `app_picker` | Application chips from the catalogue. |
| `role_picker` | Role chips within the chosen application. |
| `menu_picker` | Menu chips, or "create a new menu first". |
| `yesno` | Two decision buttons (✓ Yes / ✕ No). |

## 3. Lifecycle

```
intent detect → start → field step ×N → confirm → execute (tool) → complete (green card)
                                                              └──→ cancel (rose card, nothing saved)
```

- **Intent** — a create verb + entity noun (`_EC_VERB` / `_EC_ENTITY`) classifies the request to an
  entity key, or asks which (`_detect_entity`).
- **Confirm** — `_entity_finalize` shows every captured value and waits for an explicit *Create*. The
  tool call is the **only write** in the whole flow.
- **Break-out** — a real question mid-flow (`"what is a menu?"`) is answered as free-form by
  `_should_break_out` **without derailing** the flow; it resumes. A plain value keeps feeding the step.
- **Cancel** — folds the raw steps into a single rose "cancelled" card; completion folds into a green
  one with "what next" chips.

## 4. Adding a workflow — checklist

1. Read the backend tool's schema — note every **NOT-NULL** and **unique** column and the exact field keys.
2. Add an entry to `_ENTITY_CREATE`: `label`, `tool`, ordered `fields`, and `defaults` for fixed columns.
3. Pick a `kind` per field; add `maxlen` / `unique` / `is_code` to match the column; use `auto_from`
   for codes derived from a name.
4. Register the entity noun in `_EC_ENTITY` (and any alias) so `_detect_entity` routes the intent.
5. Confirm `_entity_finalize` maps collected values + defaults to the tool's arguments.
6. Dry-run in the widget: step through, check the rail values, **cancel once** (rose card), then
   **complete once** (green card) against a test record.

> Field lists and lengths are verified against the backend entity contracts before shipping — get
> them from the tool's schema, not by guessing.
