# Design System Audit

A complete read-only Figma inventory. Run all steps before any writes.
The richer this catalog, the fewer new components you'll need to create.

---

## Step 1: Discover Libraries

```
get_libraries({ fileKey: "FILEKEY" })
```

Returns:
- `libraries_added_to_file` — already available in this file
- `libraries_available_to_add` — org/community libraries you could add
- `libraries_available_to_add_next_offset` — paginate if non-null (20 per page)

Note the `libraryKey` for each library. Use it to scope `search_design_system` calls
when results from a broad search are noisy.

---

## Step 2: Search the Design System

Run these `search_design_system` queries. Parallel calls are fine here (this is read-only).
Use `includeLibraryKeys` to scope to specific libraries when you know which one to target.

**Component searches** (`includeComponents: true`):

Run in parallel batches — don't wait for one before starting the next:

```
Batch 1 (layout + containers): button, input, card, modal, table, drawer, sidebar, header, footer
Batch 2 (navigation + selection): nav, tab, dropdown, select, checkbox, radio, toggle, breadcrumb
Batch 3 (data + feedback):       badge, tag, avatar, chart, stat, metric, alert, toast, empty, skeleton
Batch 4 (misc):                  icon, datepicker, accordion, stepper, progress, upload, pagination
```

**Variable searches** (`includeVariables: true`):

```
color, background, surface, foreground, text, border, shadow,
space, spacing, radius, gap, padding,
primary, secondary, neutral, gray, brand
```

Try multiple synonyms — libraries vary widely in naming ("grey" vs "gray",
"spacing" vs "space", "color/bg" vs "background").

**Style searches** (`includeStyles: true`):

```
heading, body, caption, label, display, code,
shadow, elevation, blur
```

---

## Step 3: Inspect File Structure

Load `figma-use` skill, then run:

```js
const pages = figma.root.children.map(p => ({
  id: p.id,
  name: p.name,
  childCount: p.children.length
}));
return { fileName: figma.root.name, pages };
```

Identify which pages contain:
- Existing product screens (the primary audit source)
- Component library pages (local components)
- Foundations / tokens pages

---

## Step 4: Inventory Components From Existing Screens

This is the most valuable step. Components found in real screens are authoritative —
they tell you exactly what's already in active use.

**Step 4a** — get page IDs (one `use_figma` call):
```js
return figma.root.children.map(p => ({ id: p.id, name: p.name }));
```

**Step 4b** — for each page that contains screens, emit parallel `use_figma` calls
(one per page, all in the same assistant turn):
```js
figma.skipInvisibleInstanceChildren = true;
const page = await figma.getNodeByIdAsync("PAGE_ID");
await figma.setCurrentPageAsync(page);

const uniqueSets = new Map();
page.findAllWithCriteria({ types: ["INSTANCE"] }).forEach(inst => {
  const mc = inst.mainComponent;
  if (!mc) return;
  const cs = mc.parent?.type === "COMPONENT_SET" ? mc.parent : null;
  const key = cs ? cs.key : mc.key;
  const name = cs ? cs.name : mc.name;
  if (key && !uniqueSets.has(key)) {
    uniqueSets.set(key, {
      name,
      key,
      isSet: !!cs,
      remote: mc.remote,
      sampleVariant: mc.name
    });
  }
});
return [...uniqueSets.values()];
```

Merge results across pages into `componentCatalog` in the state file.

---

## Step 5: Discover Variables In Use

Run this against one of the primary screen pages:

```js
figma.skipInvisibleInstanceChildren = true;
const page = await figma.getNodeByIdAsync("PAGE_ID");
await figma.setCurrentPageAsync(page);

const uniqueIds = new Set(
  page.findAll(() => true).flatMap(n =>
    Object.values(n.boundVariables ?? {})
      .flatMap(b => Array.isArray(b) ? b : [b])
      .map(b => b?.id)
      .filter(Boolean)
  )
);
const vars = await Promise.all(
  [...uniqueIds].map(id => figma.variables.getVariableByIdAsync(id))
);
return vars
  .filter(Boolean)
  .map(v => ({ name: v.name, id: v.id, key: v.key, type: v.resolvedType, remote: v.remote }));
```

Cross-reference with `search_design_system` results. For remote variables
(`remote: true`), import by key with `figma.variables.importVariableByKeyAsync(key)`.
Build `tokenMap` in the state file from the results.

**Also check for local variable collections** (separate from remote library vars):
```js
const collections = await figma.variables.getLocalVariableCollectionsAsync();
return collections.map(c => ({
  name: c.name, id: c.id,
  varCount: c.variableIds.length,
  modes: c.modes.map(m => m.name)
}));
```

Note: `getLocalVariableCollectionsAsync()` returns only *local* variables. An empty
result does not mean no variables exist — always also run `search_design_system` with
`includeVariables: true` to check library variables.

---

## Step 6: Discover Text and Effect Styles

```js
figma.skipInvisibleInstanceChildren = true;
const page = await figma.getNodeByIdAsync("PAGE_ID");
await figma.setCurrentPageAsync(page);
const styles = { text: new Map(), effect: new Map() };

for (const node of page.findAll(() => true)) {
  if ('textStyleId' in node && node.textStyleId) {
    const s = figma.getStyleById(node.textStyleId);
    if (s) styles.text.set(s.id, { name: s.name, id: s.id, key: s.key });
  }
  if ('effectStyleId' in node && node.effectStyleId) {
    const s = figma.getStyleById(node.effectStyleId);
    if (s) styles.effect.set(s.id, { name: s.name, id: s.id, key: s.key });
  }
}
return {
  textStyles: [...styles.text.values()],
  effectStyles: [...styles.effect.values()]
};
```

---

## Step 7: Capture Component Properties

For components that will likely be used in Phase 5 screen building, fetch their
component properties now (needed for `setProperties()` text overrides later).

For each cataloged `COMPONENT_SET` key:
```js
const set = await figma.importComponentSetByKeyAsync("COMPONENT_KEY");
const temp = set.defaultVariant.createInstance();
const props = Object.entries(temp.componentProperties).map(([k, v]) => ({
  key: k,
  type: v.type,
  defaultValue: v.value
}));
temp.remove();
return props;
```

Add `properties` to each entry in `componentCatalog`.

---

## Step 8: Extract Design Language Patterns

From observing existing screens, note these patterns to carry into new screens:

| Pattern | How to observe |
|---------|---------------|
| **Grid / spacing** | Common frame widths, padding values (usually 8/16/24/32px multiples) |
| **Typography** | Which text styles appear most — body, heading, caption sizes |
| **Corner radius** | Most common radius on cards, buttons, inputs |
| **Color usage** | Primary brand color, background, text, accent fills in active use |
| **Naming convention** | How pages, frames, and layers are named — match this exactly |
| **Icon style** | Stroke vs filled, size (16/20/24px) |

Summarize in a one-liner for the Phase 2 checkpoint:
> "8px grid · Inter typeface · 8px radius on interactive elements · primary brand blue `#0066CC`"

---

## Output: Component Catalog Format

Merge all findings into `componentCatalog` in the state file:

```json
{
  "Button": {
    "key": "abc123",
    "type": "COMPONENT_SET",
    "library": "DS Core",
    "source": "screen-inspection",
    "properties": {
      "Variant": { "type": "VARIANT" },
      "Size": { "type": "VARIANT" },
      "Label#2:0": { "type": "TEXT", "defaultValue": "Button" },
      "Has Icon#4:64": { "type": "BOOLEAN", "defaultValue": false }
    }
  },
  "StatCard": {
    "key": "xyz789",
    "type": "COMPONENT_SET",
    "library": "local",
    "source": "created-phase4",
    "properties": {
      "Value#1:0": { "type": "TEXT", "defaultValue": "0" },
      "Label#2:0": { "type": "TEXT", "defaultValue": "Metric" },
      "Trend": { "type": "BOOLEAN", "defaultValue": false }
    }
  }
}
```

The `source` field tracks provenance:
- `"screen-inspection"` — found by inspecting existing screens
- `"search-design-system"` — found via `search_design_system`
- `"created-phase4"` — created or modified during Phase 4
