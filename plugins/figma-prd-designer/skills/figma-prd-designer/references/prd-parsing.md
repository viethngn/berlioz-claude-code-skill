# PRD Parsing — Confluence Extraction

How to fetch a PRD from Confluence and derive a structured, ordered screen list
that drives the rest of the design workflow.

---

## 1. Fetch the Confluence Page

```
mcp__MCP_DOCKER__confluence_get_page({ page_id: "{PAGE_ID}" })
```

Collect from the response:
- `title` — the page title (useful for naming the Figma top-level page)
- `body` — full page content (HTML or Confluence storage format)
- `space.key` / `space.name` — context for naming

If the page body references JIRA ticket IDs matching `[A-Z]+-\d+`, consider fetching
key tickets with `mcp__MCP_DOCKER__jira_get_issue` — ticket descriptions often contain
field lists, acceptance criteria, and UI detail that refine component inference.

---

## 2. Extract Feature Context

Scan the page body for these elements:

| Element | What to extract |
|---------|----------------|
| **Feature title** | H1 / H2 heading or the page title itself |
| **Problem statement** | Intro paragraph, "Background", "Problem", "Context" sections |
| **User stories** | "As a / I want / So that" blocks, tables of user requirements |
| **UX flows** | "User flow", "Workflow", "Journey" sections — numbered step lists |
| **Screen references** | Explicit mentions of screen names, pages, modals, drawers, tabs |
| **UI component hints** | Field/element tables, wireframe descriptions, form field lists |
| **Out-of-scope notes** | "Not doing" / "Out of scope" sections — exclude these screens |

---

## 3. Derive the Screen List

Build an ordered list of screens from what you found. Each screen entry:

```json
{
  "name": "Campaign Dashboard",
  "status": "CREATE",
  "purpose": "Overview of all active campaigns with key metrics at a glance",
  "sections": ["Header nav", "Stats row", "Campaign table", "Quick actions sidebar"],
  "componentsNeeded": ["Button", "DataTable", "StatCard", "Badge", "Input (search)"],
  "userStoriesRef": [
    "As an advertiser, I want to see all my campaigns in one view..."
  ]
}
```

**`status` values:**
- `CREATE` — a new screen to be designed from scratch
- `UPDATE` — an existing screen in the Figma file that needs modification
  (flag these so Phase 2 can locate them)

**How to identify screens:**

| Signal in PRD | Interpretation |
|--------------|----------------|
| Explicit screen/page name | Direct mapping — one screen |
| Flow with distinct states (list → detail → edit) | Each state is a screen |
| Modal, drawer, or dialog described | Each is a screen (even if layered) |
| Step-by-step numbered flow | Each step with distinct UI = separate screen |
| Tab groups | May be one screen with tab variants, or multiple |

**Ordering:** Present screens in the order a user encounters them in the primary happy path
(e.g., list → detail → create → confirm). Modals/drawers appear after the screen that
triggers them.

---

## 4. Infer Components from User Story Patterns

Use these heuristics when the PRD doesn't name UI elements explicitly:

| User story pattern | Components likely needed |
|-------------------|--------------------------|
| "filter / search for..." | Input, Dropdown/Select, Button (Apply/Reset) |
| "see a list of..." | DataTable or Card grid + Pagination |
| "view summary / metrics" | StatCard, Chart, Badge, ProgressBar |
| "create / edit a..." | Form (Input, Select, Datepicker, Textarea, Toggle) + Button |
| "confirm / approve / delete" | Modal + Button (Primary + Secondary or Destructive) |
| "navigate between sections" | Tabs, Sidebar Nav, or Breadcrumb |
| "upload a file" | FileUpload component |
| "see status / progress" | Badge, StatusIndicator, ProgressBar |
| "receive feedback / notification" | Toast, Alert Banner, InlineError |
| "see empty state" | EmptyState component |
| "loading / async data" | Skeleton, Spinner |

---

## 5. Edge Cases

**Vague PRD with no screen references:**
Infer screens from user story flows. Present the derived list and ask for confirmation —
the user knows their product better than the document does. A simple:
"Based on the user stories, I'm inferring these screens: [list]. Does this sound right?"

**PRD describes updates to existing screens:**
Mark those as `"status": "UPDATE"` in the screen list. In Phase 2, use `get_metadata`
to locate those frames in the Figma file by name. Plan targeted modifications rather than
rebuilding from scratch — less work and less risk to existing designs.

**PRD includes wireframe images:**
You can't read the images directly, but the surrounding text (section headings, captions,
element names in alt text) usually describes what they show. Note any explicit element
names from image captions and include them in `componentsNeeded`.

**PRD is very large (10+ screens):**
Ask the user which screens to prioritize for this session. Design system complexity grows
non-linearly — it's better to do fewer screens well than rush through many.

**PRD references a design or prototype link:**
If a Figma link is provided in the PRD body, note the `fileKey` and `nodeId`. Use
`get_design_context` or `get_screenshot` on those nodes during Phase 2 to understand
the intended visual direction before auditing the main design file.
