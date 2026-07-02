# Atlassian API Reference

Load when debugging Confluence/Jira fetches, or when extending the ingest
skill to handle new source shapes.

## Confluence

### Endpoint

```
GET {confluence_base_url}/rest/api/content/{pageId}?expand=body.storage,version,space,ancestors
```

Response includes:

- `title` — Page title
- `body.storage.value` — Page body in Confluence "storage format" (an XHTML
  dialect with `<ac:*>` macros)
- `body.storage.representation` — Always `"storage"` for this endpoint
- `version.number` — Monotonic version counter
- `version.when` — ISO 8601 timestamp of last edit
- `space.key`, `space.name` — Confluence space
- `ancestors[]` — Breadcrumb (each has `id`, `title`)

### Extracting the page ID from a URL

Supported URL shapes:

1. `.../pages/{pageId}/Page-Title` — modern URLs, take the numeric segment
   after `/pages/`
2. `...?pageId=12345` — legacy URLs, take the query parameter
3. `.../display/SPACE/Page+Title` — display URLs, no page ID → resolve via
   the `/rest/api/content` search API using `spaceKey` + `title`

`fetch_confluence.py` handles cases 1 and 2. Case 3 is out of scope for v1;
paste the modern URL instead.

### Storage format to Markdown

Confluence storage format is XHTML plus macros. `fetch_confluence.py` runs
[markdownify](https://github.com/matthewwithanm/python-markdownify) with
BeautifulSoup preprocessing:

- Strip `<ac:structured-macro ac:name="...">` wrappers, keep inner text
- Replace `<ac:link><ri:page ri:content-title="X"/></ac:link>` with `[X]` so
  Claude can rewrite as a `[[X]]` wiki-link during Phase 3
- Keep `<ac:image><ri:attachment ri:filename="..."/></ac:image>` as an
  `<img src="{base_url}/download/attachments/{pageId}/{filename}">` so
  `extract_images.py` can download and hash it
- Drop `<ac:parameter>` metadata

### Downloading attachments

Attachments are served from:

```
GET {confluence_base_url}/download/attachments/{pageId}/{filename}
```

They require the same PAT auth as the page fetch. `extract_images.py` reuses
the Confluence PAT when the image host matches `atlassian.confluence_base_url`.

### PAT auth

Confluence Server/DC and modern Cloud both accept:

```
Authorization: Bearer <PAT>
```

If the token is empty in `.wikirc.json`, `fetch_confluence.py` exits with:

```
ERROR: atlassian.confluence_pat is empty in .wikirc.json but you asked to fetch a Confluence page.
```

## Jira

### Endpoint

```
GET {jira_base_url}/rest/api/2/issue/{issueKey}?expand=renderedFields
```

Response fields we care about:

- `key` — e.g. `PROJ-123`
- `fields.summary` — Ticket title
- `fields.description` — Body (Markdown in Cloud, wiki-markup in Server/DC —
  `renderedFields.description` gives HTML which we can markdownify)
- `fields.issuetype.name` — Bug / Story / Task / Epic
- `fields.status.name` — Todo / In Progress / Done / etc.
- `fields.priority.name` — Highest / High / Medium / Low / Lowest
- `fields.assignee.displayName` — May be null
- `fields.reporter.displayName`
- `fields.created`, `fields.updated` — ISO 8601
- `fields.fixVersions[].name` — Release versions
- `fields.labels[]`
- `fields.components[].name`
- `fields.comment.comments[]` — May be truncated; use `?fields=comment` for full

### Extracting the issue key from a URL

Supported shapes:

1. `.../browse/PROJ-123` — take the segment after `/browse/`
2. `.../issues/?jql=...` + `selectedIssue=PROJ-123` — take the query param
3. Bare `PROJ-123` — assume it's already the key

Regex: `\b[A-Z][A-Z0-9]+-\d+\b`.

### Markdown output shape

`fetch_jira.py` writes the raw file as:

```markdown
# {KEY} — {summary}

**Type:** {issuetype}
**Status:** {status}
**Priority:** {priority}
**Reporter:** {reporter}
**Assignee:** {assignee}
**Created:** {created}
**Updated:** {updated}
**Fix versions:** {fixVersions}
**Labels:** {labels}
**Components:** {components}

## Description

{markdownified renderedFields.description}

## Comments

### {author} — {created}
{body}
...
```

Attachments are extracted from `fields.attachment[]` (URL + filename) and
downloaded like Confluence attachments.

### PAT auth

Same as Confluence — `Authorization: Bearer <PAT>`. Separate token from
Confluence because organizations sometimes provision them independently.

## Common failure modes

| HTTP status | Meaning | Fix |
|-------------|---------|-----|
| 401 | Bad token, expired token, or wrong auth scheme | Check the PAT, verify Bearer vs Basic |
| 403 | Token valid but no permission for this page/issue | User needs Confluence/Jira access to the target |
| 404 | Page/issue doesn't exist, or ID wrong | Double-check the URL — Confluence page IDs are numeric, Jira keys are `LETTERS-DIGITS` |
| 407 | Corporate proxy auth needed | Configure your proxy via `HTTPS_PROXY` env var |
| 502/503 | Server or intermediate proxy hiccup | Retry — `_deps.py` doesn't retry automatically, but manual re-runs are safe |

## Extending to bulk ingest (JQL / CQL)

Out of scope for v1. Sketch:

- **JQL bulk**: `GET /rest/api/2/search?jql={...}&maxResults=100` returns a
  paginated list. Iterate and call `fetch_jira.py` per key.
- **CQL bulk**: `GET /rest/api/content/search?cql={...}` for Confluence.
- Wrap in a new `ingest_bulk.py` that loops and de-duplicates against the
  git-committed manifest so already-ingested items are skipped.
