# PRD Parsing — Multi-Source Extraction

How to extract screens and component requirements from any combination of inputs.
All modes produce the same output: an ordered `screens[]` list saved to the state file.

Jump to the section matching `prdSource.type`:
- [Input Mode: Confluence URL](#input-mode-confluence-url)
- [Input Mode: Wireframe Images](#input-mode-wireframe-images)
- [Input Mode: Wireframe PDF](#input-mode-wireframe-pdf)
- [Input Mode: Mixed Sources](#input-mode-mixed-sources)

Then use the shared steps:
- [Deriving the Screen List](#deriving-the-screen-list) — applicable to all modes
- [Inferring Components Visually](#inferring-components-visually) — for image/PDF modes
- [Inferring Components from Text](#inferring-components-from-text) — for Confluence mode
- [Edge Cases](#edge-cases)

---

## Input Mode: Confluence URL

### 1. Fetch the Page

```
mcp__MCP_DOCKER__confluence_get_page({ page_id: "{PAGE_ID}" })
```

Collect: `title` (for naming), `body` (full content), `space.key`.

If the body references JIRA ticket IDs (`[A-Z]+-\d+`), consider fetching key tickets
with `mcp__MCP_DOCKER__jira_get_issue` — ticket descriptions often contain field lists,
acceptance criteria, and UI detail that refine component inference.

### 2. Extract Feature Context

Scan the body for these elements:

| Element | What to extract |
|---------|----------------|
| **Feature title** | H1/H2 heading or page title |
| **Problem statement** | "Background", "Problem", "Context" sections |
| **User stories** | "As a / I want / So that" blocks, user story tables |
| **UX flows** | "User flow", "Workflow", "Journey" — numbered step lists |
| **Screen references** | Explicit screen names, modals, drawers, tabs |
| **UI component hints** | Field/element tables, form field lists, wireframe descriptions |
| **Out-of-scope notes** | "Not doing" / "Out of scope" — exclude these screens |

Use [Inferring Components from Text](#inferring-components-from-text) to derive
`componentsNeeded` from user story patterns.

---

## Input Mode: Wireframe Images

Claude reads `.png`, `.jpg`, `.jpeg`, `.webp`, and `.gif` files natively via the
`Read` tool — no conversion needed.

### 1. Read Each Image

For each image path provided:
```
Read({ path: "~/wireframes/dashboard.png" })
```

Analyze the image visually. For each wireframe, identify:

| What to look for | Maps to |
|-----------------|---------|
| Title text or header label in the wireframe | `name` for the screen |
| Filename (e.g., `03-campaign-detail.png`) | `name` fallback if no title visible |
| Major horizontal/vertical zones | `sections[]` entries |
| UI elements visible (see mapping table below) | `componentsNeeded[]` entries |
| Annotation text, labels, placeholder text | Refinement hints |

### 2. One Screen Per Image (Default)

Treat each image as one screen entry. If a single image clearly shows multiple screens
(e.g., a flow diagram or side-by-side comparison), split it — produce one entry per
distinct screen shown.

### 3. Order by Filename or Flow

If filenames have a numeric prefix (`01-`, `02-`, `03-`), use that order. Otherwise,
infer the logical user flow order (list → detail → create → confirm) and sort accordingly.

Use [Inferring Components Visually](#inferring-components-visually) to populate
`componentsNeeded` from what you see.

---

## Input Mode: Wireframe PDF

### 1. Try the Read Tool First

```
Read({ path: "~/Documents/feature-wireframes.pdf" })
```

The `Read` tool converts PDF content to text automatically.

**If the extracted text is meaningful** (annotated wireframes with section labels, field
lists, component names): proceed — treat this like Confluence content and use
[Inferring Components from Text](#inferring-components-from-text).

**If the extracted text is sparse or empty** (wireframes are embedded images with little
or no selectable text): load the `pdf` skill and use it to extract each page as an image,
then process each page image using the
[Wireframe Images](#input-mode-wireframe-images) approach above.

### 2. Page = Screen (Default)

Each PDF page is one candidate screen. Confirm with the user if the page count is large
(10+) — ask which pages to prioritize.

### 3. Naming Screens from PDF Pages

Use the most prominent heading or title text visible on each page. If none is visible,
use `Page {N}` as a temporary name and flag it for the user to rename at the Phase 1
checkpoint.

---

## Input Mode: Mixed Sources

When the user provides multiple source types (e.g., Confluence URL + wireframe images),
run each applicable mode above in parallel and then merge the results.

**Typical merge strategy:**

| Source | Role |
|--------|------|
| Confluence text | Primary source for `purpose`, user stories, `userStoriesRef`, out-of-scope rules |
| Wireframe images / PDF | Primary source for `sections[]` (visual layout), `componentsNeeded[]` (what's visible), screen ordering |

When the two sources agree on screens, merge into one entry. When they disagree
(e.g., Confluence lists a "Settings" screen but no wireframe exists for it), note the
discrepancy in the Phase 1 checkpoint and ask the user how to handle it.

---

## Deriving the Screen List

All modes produce entries in this format:

```json
{
  "name": "Campaign Dashboard",
  "status": "CREATE",
  "purpose": "Overview of all active campaigns with key metrics at a glance",
  "sections": ["Header nav", "Stats row", "Campaign table", "Quick actions sidebar"],
  "componentsNeeded": ["Button", "DataTable", "StatCard", "Badge", "Input (search)"],
  "sourceRef": "wireframe: dashboard.png / confluence: page 12345678"
}
```

**`status` values:**
- `CREATE` — new screen to be designed from scratch
- `UPDATE` — existing screen in the Figma file needing modification
  (Phase 2 will locate these by name in the file)

**How to identify screens from text (Confluence):**

| Signal | Interpretation |
|--------|---------------|
| Explicit screen/page name | Direct mapping — one screen |
| Flow with distinct states (list → detail → edit) | Each state is a screen |
| Modal, drawer, or dialog described | Each is its own screen |
| Step-by-step numbered flow | Each step with distinct UI = separate screen |
| Tab groups | One screen with tab variants, or multiple — use judgment |

**Ordering:** Primary happy path first (list → detail → create → confirm). Modals and
drawers appear after the screen that triggers them.

---

## Inferring Components Visually

Use this table when processing wireframe images or PDF pages. Match what you see to
component types — then verify against the `componentCatalog` during Phase 2.

| Visible element in wireframe | Component type to infer |
|-----------------------------|------------------------|
| Rectangle with label + border | Input or Textarea |
| Pill / rounded rectangle with text | Button or Badge |
| Large bordered box with caret / arrow | Select / Dropdown |
| Grid of rows with column headers | DataTable |
| Cards in a grid or list layout | Card component |
| Left-side vertical nav with links | Sidebar Nav |
| Top horizontal item row with underline | Tabs |
| Circular or square image placeholder | Avatar |
| Small colored dot or pill with text | Badge / StatusIndicator |
| Dashed rectangle with "+" or "Upload" | FileUpload |
| Thin filled bar (horizontal) | ProgressBar |
| Bell or X icon in corner | Notification / Toast trigger |
| Horizontal rule with section title | Divider / Section Header |
| Skeleton / gray placeholder blocks | Skeleton / Loading state |
| Empty box with centered icon + text | EmptyState |
| Chevron / arrow on a row item | List item with disclosure |
| Star or heart icon | Rating or Favorite toggle |
| Toggle switch shape | Toggle |
| Checkbox square or radio circle | Checkbox / Radio |
| Date or calendar picker box | Datepicker |
| Stepper (1 → 2 → 3 circles) | StepIndicator / Stepper |
| Breadcrumb path text | Breadcrumb |

When a UI element is ambiguous (e.g., a rectangle could be an Input or a Card), note
both possibilities with a `?` suffix in `componentsNeeded` and resolve during Phase 2
once the actual design system catalog is known.

---

## Inferring Components from Text

Use these heuristics when deriving components from Confluence user story language:

| User story pattern | Components likely needed |
|-------------------|--------------------------|
| "filter / search for..." | Input, Dropdown/Select, Button (Apply/Reset) |
| "see a list of..." | DataTable or Card grid + Pagination |
| "view summary / metrics" | StatCard, Chart, Badge, ProgressBar |
| "create / edit a..." | Form (Input, Select, Datepicker, Textarea, Toggle) + Button |
| "confirm / approve / delete" | Modal + Button (Primary + Secondary or Destructive) |
| "navigate between sections" | Tabs, Sidebar Nav, or Breadcrumb |
| "upload a file" | FileUpload |
| "see status / progress" | Badge, StatusIndicator, ProgressBar |
| "receive feedback / notification" | Toast, Alert Banner, InlineError |
| "see empty state" | EmptyState |
| "loading / async data" | Skeleton, Spinner |

---

## Edge Cases

**Vague PRD with no screen references:**
Infer screens from user story flows or wireframe structure. Present the derived list
and ask the user to confirm — they know the product better than the document does.

**PRD describes updates to existing screens:**
Mark those as `"status": "UPDATE"`. In Phase 2, use `get_metadata` to locate those
frames in the Figma file by name. Plan targeted modifications — less work, less risk.

**Large PDF or many images (10+ screens):**
Ask which screens to prioritize for this session. Design system complexity grows
non-linearly — it's better to do fewer screens well than rush through many.

**PRD references an existing Figma prototype or design link:**
Note the `fileKey` and `nodeId` from the URL. Use `get_design_context` or
`get_screenshot` on those nodes during Phase 2 to understand the intended visual
direction before auditing the main target file.

**Wireframe is low-fidelity or hand-drawn:**
Low-fidelity wireframes (boxes, arrows, rough sketches) still convey layout sections
and element positions. Focus on spatial zones (header/main/sidebar/footer) and gross
element shapes rather than exact visual details. The design system audit in Phase 2
determines the actual visual treatment.
