---
name: figma-prd-designer
description: >-
  Reads a PRD from Confluence and builds Figma screen designs that fully leverage
  the target file's existing design system — components, variables, and styles.
  Runs a structured five-phase workflow: parse the PRD, audit the design system,
  classify every component need (reuse / modify / create), resolve gaps, then
  build screens section by section. The reuse-first strategy keeps designs
  consistent with the existing library and ensures new components are generic
  and durable. Invoke this skill whenever the user wants to design screens from a
  PRD or Confluence document, translate product requirements into Figma mockups,
  build Figma screens that follow an existing component library, or generate
  designs from user stories. Trigger on phrases like: "design this PRD in Figma",
  "create Figma screens from the Confluence page", "turn these requirements into
  designs", "build the UI for this feature in Figma", "take the PRD and make the
  mockups", "I need wireframes or mockups for this feature", "create a design file
  for this PRD", "push these requirements to Figma", "design the screens from this
  spec", "make a Figma file from this Confluence link", "design from these
  wireframes", "I have wireframe images and want to build in Figma", "here's a
  PDF of the wireframes, create the Figma designs", "use these mockup images as
  the design spec", "build Figma screens from these screenshots".
disable-model-invocation: false
---

# Figma PRD Designer

Turn a Confluence PRD into Figma screen designs that faithfully use the existing
design system. The key discipline: before creating a single node, fully audit what
already exists — then reuse aggressively, modify sparingly, and create new
components only when nothing close exists. New components must be generic enough
to serve future screens, not just the current feature.

## Prerequisites

- **Figma MCP** — `plugin-figma-figma` server must be connected
- **Confluence MCP** — required only when a Confluence URL is provided
  (`mcp__MCP_DOCKER__confluence_get_page`, same Docker MCP as `release-note-writer`)
- **figma-use skill** — load before every `use_figma` call (critical API rules)
- **figma-generate-design skill** — load before Phase 5 screen building
- **figma-generate-library skill** — load before Phase 4 component creation or modification

## Required Inputs

Ask for these upfront if not provided. At least one PRD source is required.

| PRD Source — pick one or combine | Format |
|----------------------------------|--------|
| Confluence URL | `https://.../confluence/.../pages/{PAGE_ID}/Feature-Name` |
| Wireframe images | Local file paths — `~/wireframes/login.png`, `~/wireframes/dashboard.png` |
| Wireframe PDF | Local file path — `~/Documents/feature-wireframes.pdf` |

| Always required | Format |
|-----------------|--------|
| Figma target file URL | `https://figma.com/design/{fileKey}/FileName?node-id=...` |

If the user doesn't have a Figma file yet, load `figma-create-new-file` skill and
call `create_new_file` first. Use the returned `fileKey` for all subsequent calls.

## State File

Create `/tmp/fprd-{runId}.json` at Phase 0. Re-read it at the start of each phase —
long workflows lose conversation context and the file is the source of truth.
Use a timestamp run ID: `fprd-YYYYMMDD-HHMMSS`.

```json
{
  "runId": "fprd-20260614-120000",
  "figmaFileKey": "FILEKEY",
  "prdSource": {
    "type": "mixed",
    "confluencePageId": "12345678",
    "imagePaths": ["~/wireframes/dashboard.png", "~/wireframes/detail.png"],
    "pdfPath": null
  },
  "phase": "phase1",
  "screens": [
    {
      "name": "Campaign Dashboard",
      "purpose": "Overview of active campaigns with key metrics",
      "sections": ["Header", "Stats Row", "Campaign Table", "Quick Actions"],
      "componentsNeeded": ["Button", "StatCard", "DataTable", "Badge"]
    }
  ],
  "componentCatalog": {
    "Button": { "key": "abc123", "type": "COMPONENT_SET", "library": "DS Core" },
    "Input":  { "key": "def456", "type": "COMPONENT_SET", "library": "DS Core" }
  },
  "tokenMap": {
    "color/bg/primary": { "key": "var-111", "type": "COLOR" },
    "spacing/md":       { "key": "var-222", "type": "FLOAT" }
  },
  "decisions": {
    "reuse":  [{ "name": "Button", "catalogKey": "abc123", "screens": ["Dashboard"] }],
    "modify": [{ "name": "Card", "change": "Add compact variant", "screens": ["Dashboard"] }],
    "create": [{ "name": "StatCard", "rationale": "Needed on 3 screens", "properties": ["value","label","trend","icon"] }]
  },
  "completedPhases": ["phase0", "phase1"]
}
```

---

## Phase 0 — Setup

1. Extract Figma `fileKey` from the URL (`figma.com/design/{fileKey}/...`)
2. Detect the PRD source mode from what the user provided:
   - **CONFLUENCE** — Confluence URL given → `prdSource.type = "confluence"`, extract page ID
   - **IMAGES** — image file paths given (.png/.jpg/.jpeg/.webp/.gif) → `prdSource.type = "images"`, list paths
   - **PDF** — PDF path given → `prdSource.type = "pdf"`, note path
   - **MIXED** — multiple source types given → `prdSource.type = "mixed"`, capture all
3. Initialize the state file with `figmaFileKey`, `prdSource`, and `phase: "phase0"`
4. Confirm inputs with the user before proceeding

---

## Phase 1 — PRD Parsing

Load [references/prd-parsing.md](references/prd-parsing.md) and follow the section
matching `prdSource.type` from the state file:

| `prdSource.type` | Section to follow |
|-----------------|-------------------|
| `"confluence"` | Input Mode: Confluence URL |
| `"images"` | Input Mode: Wireframe Images |
| `"pdf"` | Input Mode: Wireframe PDF |
| `"mixed"` | Input Mode: Mixed Sources |

All modes produce the same output: an ordered `screens[]` list saved to the state file,
where each entry has `name`, `purpose`, `sections[]`, and `componentsNeeded[]`.

**User checkpoint:**

> "I found [N] screens in this PRD:
> 1. **[Screen]** — [purpose]. Sections: [list]. Components likely needed: [list].
> 2. ...
>
> Does this match what you're expecting? Anything to add, remove, or rename?"

Don't proceed to Phase 2 until the screen list is confirmed.

---

## Phase 2 — Design System Audit

Read-only Figma inspection. Goal: know everything that exists before deciding what to build.
The effort here directly reduces the number of components you'll need to create — invest
in it properly.

Load [references/design-audit.md](references/design-audit.md) for audit scripts and queries.

**Steps:**
1. `get_libraries` → discover all linked libraries
2. `search_design_system` (multiple query terms) → catalog components, variables, styles
3. `use_figma` read-only scripts → inspect existing screens to extract design language patterns
4. Build `componentCatalog` and `tokenMap` in state file

**User checkpoint:**

> "Design system audit complete:
> - **[N] components** cataloged: Button, Input, Card, Avatar, Badge, ...
> - **[N] variables** (colors, spacing, radii)
> - **[N] text styles**, **[N] effect styles**
> - Design language: [e.g., 8px grid · Inter typeface · 8px corner radius]
>
> **Gaps for this PRD** (not in the library):
> - StatCard — needed on Dashboard, Analytics screens
> - FilterBar — needed on Campaign List screen
>
> Ready to plan the component strategy?"

---

## Phase 3 — Component Strategy

Apply the decision tree to every gap identified in Phase 2. See
[references/component-decisions.md](references/component-decisions.md) for the full tree
with examples and safety checks.

**Quick rule: Reuse > Modify > Create.** Each level up costs more — more time, more design
system complexity, more future maintenance. Only escalate when the lower option genuinely
cannot serve the need.

**Classify every gap:**
- **REUSE** — existing component covers the need (exact match or close with variant override)
- **MODIFY** — existing component is structurally right but missing a variant or property;
  adding it won't break current instances
- **CREATE** — no close match; component needed on 2+ screens or clearly general-purpose

**User checkpoint — get explicit approval before any writes:**

> "Here's my component plan:
>
> **REUSE** (no changes needed):
> - Button (Primary/Large) from DS Core
> - Input (Default) from DS Core
>
> **MODIFY** (extend existing):
> - Card → add 'Compact' size variant (currently only Default exists)
>
> **CREATE NEW** (new general-purpose components):
> - StatCard — metric display tile. Props: value (text), label (text), trend (boolean),
>   icon (instance-swap). Reusable for any dashboard or reporting screen.
>
> Does this plan look right? I won't touch Figma until you approve."

---

## Phase 4 — Component Resolution

Execute the approved plan. Load `figma-generate-library` for all writes.

**Order:** MODIFY first (extend existing), then CREATE (build new).

**MODIFY:** Locate the component by key, add the new variant/property following existing
naming conventions. Validate with `get_screenshot`. Get user confirmation per component.

**CREATE:** Follow the `figma-generate-library` workflow — tokens must exist before
components are built, all visual properties bind to variables, full variant matrix,
component properties for TEXT / BOOLEAN / INSTANCE_SWAP. Name everything generically
(`StatCard` not `DashboardStatCard`). After creation, add the new key to `componentCatalog`
in the state file.

**User checkpoint per component:** Show screenshot. Confirm before moving to the next.

---

## Phase 5 — Screen Building

Load `figma-generate-design` and follow its required workflow for each screen.

**Adaptations for this skill:**
- Use `componentCatalog` from Phases 2–4 as the primary source for Step 2 component keys —
  avoid redundant `search_design_system` calls for already-cataloged items
- Build screens in the order confirmed in Phase 1
- Screen naming: match the convention of existing screens in the file.
  If there are none, use `[FeatureName] / [ScreenName]`
- After each screen: `get_screenshot` → user review → fix issues → next screen

Don't start the next screen until the current one is approved.

---

## Component Decision Quick Reference

Full tree with safety checks and examples in [references/component-decisions.md](references/component-decisions.md).

```
Does an exact match (or close variant) exist in the catalog?
  YES → REUSE. Import and instance it. Override text/boolean properties as needed.
  NO  → Does a partial match exist (same structure, missing variant or property)?
    YES → Can the variant/property be added without breaking existing instances?
      YES → MODIFY.
      NO  → Is the component needed on 2+ screens or clearly reusable?
        YES → CREATE NEW (generic naming, expose all likely properties, bind all tokens).
        NO  → REUSE closest match with property overrides. Don't create for one screen.
    NO  → CREATE NEW.
```

---

## Reference Docs

| Doc | Load when |
|-----|-----------|
| [references/prd-parsing.md](references/prd-parsing.md) | Phase 1 — extracting screens from Confluence |
| [references/design-audit.md](references/design-audit.md) | Phase 2 — auditing the Figma design system |
| [references/component-decisions.md](references/component-decisions.md) | Phase 3 — reuse / modify / create decisions |
| `figma-use` skill | Before every `use_figma` call |
| `figma-generate-design` skill | Phase 5 screen building |
| `figma-generate-library` skill | Phase 4 component creation and modification |
