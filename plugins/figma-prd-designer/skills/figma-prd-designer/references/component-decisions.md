# Component Decisions — Reuse / Modify / Create

The three-level decision tree for every component gap. Work down the levels in order —
escalate only when the lower option genuinely cannot serve the need. Fewer new components
means a cleaner, more coherent design system.

---

## Level 1: REUSE

Use an existing component from the catalog exactly as it is.

**When to choose REUSE:**
- An exact match exists in the catalog (same visual pattern, same variant axes)
- A close match exists where text content or a boolean/variant property override is sufficient
- More than ~60% of the component's needs are met by an existing component

Variant differences alone don't require a new component — you set `Variant=Secondary`
or `Size=Small` via `setProperties()` on the instance. Only reach for MODIFY when the
variant you need literally doesn't exist in the component set.

**How to implement:**

In Phase 5, within the `figma-generate-design` Step 4 section build:
```js
const [wrapper, buttonSet] = await Promise.all([
  figma.getNodeByIdAsync("WRAPPER_ID"),
  figma.importComponentSetByKeyAsync("BUTTON_KEY")  // from componentCatalog
]);
const primaryBtn = buttonSet.children.find(c => c.name.includes("Variant=Primary"))
  || buttonSet.defaultVariant;
const instance = primaryBtn.createInstance();
instance.setProperties({ "Label#2:0": "Save Changes" });
wrapper.appendChild(instance);
```

**Example:**
- Need: A "Cancel" button
- Catalog has: Button with `Variant` (Primary/Secondary/Destructive) and `Size` (Sm/Md/Lg)
- Decision: REUSE. Set `Variant=Secondary, Size=Medium`. Override label to "Cancel".

---

## Level 2: MODIFY

Extend an existing component by adding a new variant value or component property.

**When to choose MODIFY:**
- The existing component's structure and visual pattern are right
- It just lacks one specific option (e.g., Card exists in Default size only — need Compact)
- Adding the new option won't change how existing instances render

Adding a **new variant** to a component set is safe — existing instances keep their current
variant selection and are completely unaffected. Adding a **new component property** with
a sensible default is equally safe. What breaks existing instances: renaming variant axes,
removing properties, or changing the default variant's structure.

**Safety check before modifying:**
```js
// Count how many existing instances use this component, and on which pages
figma.skipInvisibleInstanceChildren = true;
const key = "COMPONENT_KEY";
const allInstances = figma.root.findAllWithCriteria({ types: ["INSTANCE"] })
  .filter(inst => {
    const mc = inst.mainComponent;
    const cs = mc?.parent?.type === "COMPONENT_SET" ? mc.parent : mc;
    return cs?.key === key;
  });
const pages = [...new Set(allInstances.map(inst => {
  let node = inst.parent;
  while (node && node.type !== "PAGE") node = node.parent;
  return node?.name ?? "unknown";
}))];
return { instanceCount: allInstances.length, pages };
```

A high instance count isn't a reason to avoid modification — it's a reason to be careful
about *what* you change. Adding a new variant is always safe regardless of instance count.

**How to implement (load figma-generate-library):**

1. Locate the component set by key: `importComponentSetByKeyAsync(key)`
2. Duplicate a nearby variant as a starting point
3. Apply the changes (resize, adjust padding tokens, update color bindings)
4. Ensure the new variant has all required variable bindings — no hardcoded values
5. Give the new variant a name matching the existing naming convention
   (e.g., if existing variants are `Size=Default`, add `Size=Compact`, not `compact-variant`)
6. Validate with `get_screenshot`

**Example:**
- Existing: Card with only `Size=Default`
- Need: A compact card for dense list views
- Decision: MODIFY. Add `Size=Compact` variant. Reduce padding from `spacing/md` to
  `spacing/sm` using existing tokens. All existing `Size=Default` instances unaffected.

**When NOT to modify:**
- The component lives in a remote library you don't own (can't edit it — CREATE a wrapper instead)
- The change is so structural it's essentially a new component (e.g., completely different layout)
- The new option would require changing the variant axis structure (rename + rebuild = risky)

**Wrapper pattern** (when you can't edit a remote component):
Create a local wrapper component that nests the remote component as an instance. Expose
the properties you need on the wrapper. Future updates to the remote component propagate
through the nested instance automatically.

---

## Level 3: CREATE NEW

Build a brand-new component from scratch.

**When to choose CREATE:**
- No existing component has a similar structure or visual pattern
- MODIFY would require changes too risky for existing instances
- The component is needed on 2+ screens in this PRD, or clearly useful beyond this feature

**One exception:** If the component is genuinely one-off — used on a single screen,
no plausible future reuse — build it as a local frame, not a component. Don't add clutter
to the design system for things that will never be reused.

---

### Design-for-the-Design-System Rule

A new component must be designed as a design system primitive, not a screen-specific layout.
If you find yourself naming it after a screen (`DashboardStatCard`) or hardcoding values
that belong in variables (`fills: [{color: {r: 0.4, g: 0.4, b: 1}}]`), stop and rethink.

| Bad (screen-specific) | Good (generic) |
|----------------------|----------------|
| DashboardStatCard    | StatCard       |
| CampaignFilterBar    | FilterBar      |
| ReportTableRow       | TableRow       |
| AnalyticsChartCard   | ChartCard      |
| OnboardingStepItem   | StepItem       |

---

### Property Design

Before writing a single line of Plugin API code, design the component's full property
surface. Think about what realistic future users of this component will need:

**Good property design — StatCard:**
```
"Value" (TEXT)           — the primary metric value, e.g. "1,284"
"Label" (TEXT)           — what the metric represents, e.g. "Active Campaigns"
"Sublabel" (TEXT)        — secondary context, e.g. "vs last 30 days"
"Trend" (BOOLEAN)        — show/hide the trend indicator
"Trend Direction" (VARIANT: Up / Down / Flat)
"Icon" (INSTANCE_SWAP)   — optional leading icon
"Size" (VARIANT: Default / Compact)
```

The goal: a future designer should be able to use this component in a reporting screen,
a summary card, a KPI widget — without touching Figma's component editor.

**Common property types:**
- `TEXT` — any text content that varies per instance (labels, values, descriptions)
- `BOOLEAN` — show/hide an optional element (icon slot, trend indicator, badge)
- `INSTANCE_SWAP` — a slot that accepts any icon or sub-component
- `VARIANT` — mutually exclusive states (Size, Variant, State, Direction)

---

### Variable Binding Rule

Every visual property must bind to a token from `tokenMap`, not hardcode a value.
Before creating the component, verify the needed tokens exist. If they don't:
- For color/spacing/radius tokens that are clearly missing from the design system:
  ask the user whether to add them or use the nearest existing token
- Never create a component with hardcoded fills, spacing, or radii

```js
// Wrong — hardcoded
section.fills = [{ type: 'SOLID', color: { r: 0.98, g: 0.98, b: 0.98 } }];

// Right — bound to token
const bgVar = await figma.variables.importVariableByKeyAsync("color/surface/secondary-key");
const bgPaint = figma.variables.setBoundVariableForPaint(
  { type: 'SOLID', color: { r: 0, g: 0, b: 0 } }, 'color', bgVar
);
section.fills = [bgPaint];
```

---

### State Coverage

At minimum, build Default and Disabled states for interactive components.
Add more states when they're realistic for this component type:

| Component type | Recommended states |
|---------------|-------------------|
| Button        | Default, Hover, Active, Disabled, Loading |
| Input         | Default, Focused, Error, Disabled, ReadOnly |
| Card          | Default, Hover, Selected |
| Badge / Tag   | (usually stateless — variants for color/type instead) |
| StatCard      | Default, Loading (skeleton-style) |

You don't need to build every state on the first pass. Default + Disabled covers 80% of
real usage. Add other states if the PRD explicitly describes them.

---

## Anti-Patterns

**Creating a component for every small variation.**
If only text content changes, that's `setProperties()` on an existing instance — not a
new component. Components should vary in *structure*, not just *content*.

**Naming components after screens or features.**
`DashboardHeader` becomes confusing the moment the component appears elsewhere. Use the
element type: `PageHeader`, `SectionHeader`.

**Building variants you're not sure about.**
If you don't know whether `Size=XLarge` will ever be used, don't create it. Variant
matrix size grows exponentially and makes components harder to maintain.

**Skipping the token check.**
Every fill, stroke, spacing, and radius must bind to a variable. No exceptions for
new components — this is what makes them reusable across themes and modes.

**Creating before checking.**
Always run the full decision tree. It's easy to jump to CREATE because it feels
productive. But REUSE takes seconds; CREATE takes 20+ `use_figma` calls.
