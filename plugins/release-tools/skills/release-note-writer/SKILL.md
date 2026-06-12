---
name: release-note-writer
description: |
  Generates Slack-style release notes from a Confluence release manual page.
  Use this skill whenever the user provides a Confluence URL and asks to write,
  draft, or generate a release note, changelog, or announcement. Also trigger
  when the user says "release note", "write release notes", "Confluence release
  manual", "create release announcement", or "generate release notes from Jira tickets".
  Even if the user just pastes a Confluence link and says something like "make a release
  note from this" — invoke this skill.
---

# Release Note Writer

Produce a polished Markdown release note in the Slack release note style — casual, self-aware,
slightly witty — from a Confluence release manual page and its linked JIRA tickets.

## Output

A file named `release-note-{version}.md` saved to the current working directory, containing:
- Slack-style release note (What's new + Bug fixes)
- Appendix table of all internal JIRA tickets

## Workflow

### 1. Extract the Confluence page ID

Parse the numeric segment from the URL path:
```
https://.../confluence/.../pages/{PAGE_ID}/...
```

### 2. Fetch the Confluence page

Call `mcp__MCP_DOCKER__confluence_get_page` with `page_id` set to the extracted ID.
Read the page title (use it to derive the product name and version) and the full page body.

### 3. Extract JIRA ticket IDs

Scan the page body for all JIRA ticket IDs matching the pattern `[A-Z]+-\d+` (e.g. `PROJ-123`).
Deduplicate the list.

### 4. Fetch each JIRA ticket

For each ticket ID, call `mcp__MCP_DOCKER__jira_get_issue` with `issue_key` set to the ticket ID.
Collect from each ticket:
- `summary`
- `issuetype.name` (Bug, Story, Task, Feature, Improvement, Epic, etc.)
- `description` (use summary as fallback if empty)
- `status.name`
- `fixVersions` (use the first entry's name if present)

### 5. Derive product name and version

- **Product name**: from the Confluence page title (e.g. "MyApp 2.4.1 Release Manual" → "MyApp")
- **Version**: from the Confluence page title or from `fixVersions` on the JIRA tickets
- **Release date**: today's date, formatted as `{Day} {Month} {Year}` (e.g. "12 June 2026")

### 6. Categorize tickets

| Issue type | Section |
|------------|---------|
| `Bug` | Bug fixes |
| `Story`, `Feature`, `Task`, `Improvement`, `Epic`, anything else | What's new |

If a ticket doesn't clearly fit either section, put it under **What's new**.

### 7. Write the release note

Follow the exact output format below. If a section has no tickets, omit it entirely (don't show an empty "Bug fixes" heading).

### 8. Save the file

Write to `release-note-{version}.md` in the current working directory. Tell the user the filename.

---

## Output Format

```markdown
# {Product} {Version}
{Release Date}

## What's new
{1-2 sentence description per feature, in the Slack release note voice}

## Bug fixes
{1-2 sentence description per fix, in the Slack release note voice}

---

## Appendix: Internal JIRA Tickets

| Ticket | Summary | Type | Status |
|--------|---------|------|--------|
| [PROJ-123]({jira_url}) | {summary} | {type} | {status} |
```

---

## Voice Examples (few-shot)

These are real Slack release notes. They are the gold standard for tone, rhythm, and humor.
Write as if you are the same author. Study the sentence length, the self-awareness, the dry asides.

---

**Slack 4.28.171 — 24 August 2022**

What's new
On Sept. 1, we'll be deprecating support for some older operating systems and outdated versions of Slack. Please visit our Help Center to get all the details: https://slack.com/help/articles/115002037526-System-requirements-for-using-Slack.

Bug fixes
Trying to capture your screen with a third-party app while also sharing your screen in Slack may have resulted in the non-Slack app crashing. We'd like to say that this was because the idea of "capture" is antithetical to "sharing," but in truth it was just a "bug."

---

**Slack 4.27.154 — 14 June 2022**

What's new
You may have noticed that with this release there's a new, larger number at the end of the version string. Going forward, while you'll still see the numbers laid out in a <MAJOR.MINOR.BUILD> sequence, the "Build" numbers will now correspond to specific builds on our end as opposed to a small sequential number. TL;DR: A few more numbers for you, a bit more specificity for everyone.

We've added the most common Apple and Microsoft file extensions to our approved list so you won't be asked to confirm each time you open a Word doc or Keynote presentation. Are you sure you'd like one less approval? YES/NO

Bug fixes
If you're in a locale that does not use the default system string encoding on Mac, opening certain file types would cause a crash in a native dependency that tries to interpret a string passed to it as the system default string encoding. If that doesn't mean anything to you, well don't worry because we fixed it.

---

**Slack 4.10.0 — 6 October 2020**

What's new
Updates mean that Big Sur is totally supported in a very holistic, west coast, chill way. It's, like, totally gnarly. Or sick! Whichever means "good", basically.

Bug fixes
Sometimes, you could not exit full screen mode with escape on windows, which was wrong, because that's literally what escape means. Now, it works.
We fixed some issues that caused window resizing of Slack to be difficult. We never want to be difficult.
Quickly switching workspaces caused problems. Switching workspaces should only cause opportunities, so we fixed that.
There were a few little bugs that caused crashes, like bugs do. We fixed those, and we'll fix the next ones too.

---

## Tone Guidelines

The patterns above reveal the voice. In summary:

- **Short sentences.** One idea per sentence.
- **Plain language.** No jargon, no acronyms, no passive voice.
- **Light wit.** A dry observation or self-deprecating aside — never a pun barrage.
- **Acknowledge the obvious.** If a bug should never have existed, you can say so.
- **Don't oversell.** "We made a thing better" is more charming than "Revolutionary enhancement".

Transform dry JIRA summaries ("Fix NPE in payment gateway") into sentences a person would actually enjoy reading. Explain *what was happening* and *why it was odd or broken*, not just that it was fixed.

---

## Edge Cases

- **No JIRA tickets found on the page**: Warn the user and ask if they want to proceed with just the Confluence content.
- **Ticket fetch fails**: Skip the ticket, note it in a comment at the bottom of the `.md` file.
- **No description on a ticket**: Use the ticket summary as the content basis.
- **Version unclear**: Use `{unknown-version}` in the filename and leave a `<!-- TODO: add version -->` comment.
- **No "What's new" tickets**: Omit the section. Same for "Bug fixes".
