---
name: generate
description: Generate detailed, high-quality documentation images using Google's Gemini 3 Pro. Use for diagrams, infographics, technical illustrations, concept visualizations, annotated mockups, process illustrations, or any visual that explains concepts in documentation. Invoke when user asks to "create an image for docs", "generate a detailed diagram", "visualize this concept", "create an infographic", "illustrate this system", or needs visual documentation assets. Requires LONG, DETAILED, DESCRIPTIVE prompts for best results.
---

# Nano Banana Pro - Documentation Image Generation

Generate detailed, professional-quality images for technical documentation, user guides, and knowledge bases. Produces diagrams, infographics, technical illustrations, concept visualizations, and any other visual assets that help explain complex ideas.

**Critical**: This tool requires LONG, DETAILED, DESCRIPTIVE prompts. Short, keyword-based prompts will produce poor results. The more context and specifics you provide, the better the output.

## Prerequisites

Set the `GEMINI_API_KEY` environment variable with your Google AI API key.

## Prompt Requirements: Detail Over Brevity

**IMPORTANT**: Nano Banana Pro works best with LONG, COMPREHENSIVE prompts that provide rich context and specific details. This is not a keyword-based tool.

### What Makes a Good Prompt

**Length**: Aim for 3-5 sentences minimum. More detail = better results.

**Components of a Detailed Prompt**:
1. **What**: Specific type of visual (architecture diagram, comparison infographic, process flow, concept illustration)
2. **Content**: All elements that should appear (components, labels, relationships, data points)
3. **Layout**: How elements are arranged (hierarchical, sequential, grid, radial)
4. **Visual Style**: Professional, clean, modern, technical, with specific color preferences
5. **Text/Labels**: Key labels, titles, annotations that must appear
6. **Context**: What this explains, why it matters, who will read it
7. **Technical Details**: Aspect ratio, resolution needs, where it will be used

### Examples: Good vs Bad Prompts

❌ **BAD** (too short, vague):
> "API architecture diagram"

✅ **GOOD** (detailed, comprehensive):
> "A horizontal system architecture diagram showing our microservices API platform. Left side: mobile and web clients. Center: API Gateway with rate limiting and authentication. Right side: five backend microservices arranged vertically (User Service, Order Service, Payment Service, Notification Service, Analytics Service), each connected to its own PostgreSQL database. Include labeled arrows showing request flow from clients through gateway to services. Use professional blue (#2563eb) for services, gray (#6b7280) for databases, green (#10b981) for the gateway. Add small icons next to each service. Clean, technical style suitable for engineering documentation. Include a legend explaining the arrow types (HTTP REST, gRPC, async events)."

❌ **BAD** (keyword stuffing):
> "comparison infographic, features, three columns, blue"

✅ **GOOD** (narrative, descriptive):
> "Create a detailed comparison infographic in a three-column horizontal layout comparing our three deployment strategies. Left column: Blue-Green Deployment with a green checkmark icon at top, followed by 4 bullet points (Zero downtime, Easy rollback, Double resources needed, Best for critical apps). Middle column: Canary Deployment with an orange progress icon, followed by 4 bullet points (Gradual rollout, Real user testing, Complex monitoring, Best for risk mitigation). Right column: Rolling Deployment with a blue cycle icon, followed by 4 bullet points (No extra resources, Continuous updates, Partial downtime risk, Best for regular updates). Use color-coded headers (green, orange, blue) matching each strategy. Professional sans-serif font, clean spacing between sections. Include a title at top: 'Deployment Strategy Comparison'. Suitable for technical documentation, 16:9 aspect ratio."

❌ **BAD** (too generic):
> "flowchart for user authentication"

✅ **GOOD** (step-by-step detail):
> "A vertical flowchart illustrating the OAuth 2.0 user authentication flow with detailed steps. Start at top with 'User clicks Login' in a rounded rectangle. Arrow down to 'Redirect to Auth0' (parallelogram). Next: 'User enters credentials' (rectangle). Diamond decision: 'Credentials valid?' with two paths - 'No' loops back to credentials entry with red arrow, 'Yes' continues down with green arrow. Continue with: 'Generate JWT token' (rectangle), 'Store in secure cookie' (rectangle), 'Redirect to dashboard' (parallelogram), ending with 'User authenticated' (rounded rectangle with green fill). Use standard flowchart shapes, blue outlines for process steps, orange for input/output, clear labels in each box. Include timestamps notation on JWT generation step ('Expires: 24h'). Add a note callout on the security cookie step explaining 'HttpOnly, Secure flags set'. Professional, technical style for API documentation."

## Available Models

| Model | ID | Best For | Max Resolution |
|-------|-----|----------|----------------|
| **Pro** | `gemini-3-pro-image-preview` | Professional quality, complex scenes | Up to 4K |

## Image Generation Workflow

### Step 1: Generate the Image

Use `scripts/image.py` with `uv run` (the script declares its own dependencies via inline
PEP 723 metadata, so `uv run` gives it a clean, correct environment with no setup step). The
script is located in the skill directory at `skills/generate/scripts/image.py`:

```bash
uv run "${SKILL_DIR}/scripts/image.py" \
  --prompt "Your image description" \
  --output "/path/to/output.png"
```

Where `${SKILL_DIR}` is the directory containing this SKILL.md file.

Options:
- `--prompt` (required): Detailed description of the image to generate
- `--output` (required): Output file path
- `--aspect` (optional): Aspect ratio (default: 16:9)
- `--reference` (optional): Path to a reference image for style, composition, or content guidance
- `--size` (optional): Image resolution - "1K", "2K", "4K" (default: 1K)

### Example Usage
```bash
uv run "${SKILL_DIR}/scripts/image.py" \
  --prompt "Create a detailed system architecture diagram showing a three-tier microservices platform. Front layer: Load balancer distributing traffic to API gateway cluster. Middle layer: Five microservices (Auth, Users, Orders, Payments, Notifications) each in separate containers with health check endpoints. Data layer: PostgreSQL primary with read replicas, Redis cache cluster, and S3 for object storage. Show request flow arrows in blue, database connections in green, cache lookups in orange. Include monitoring stack (Prometheus, Grafana) on the side. Use professional colors, clear labels on all components, and add a legend. Suitable for technical documentation explaining our platform architecture." \
  --output "/path/to/architecture-diagram.png" \
  --size 2K
```

### Using a Reference Image

To generate an image based on an existing reference (useful for maintaining consistent visual style across documentation):

```bash
uv run "${SKILL_DIR}/scripts/image.py" \
  --prompt "Create a system integration diagram showing how our payment service connects to Stripe API, using the same visual style, color scheme, and layout conventions as the reference diagram. Show the payment flow from user checkout through our service to Stripe, including webhook callbacks for payment confirmations. Include error handling paths and retry logic. Use the same icon style and arrow conventions from the reference." \
  --output "/path/to/payment-integration-diagram.png" \
  --reference "/path/to/existing-architecture-diagram.png"
```

The reference image helps Gemini understand the desired style, composition, or visual elements you want, ensuring visual consistency across your documentation.

## Crafting Detailed Prompts for Documentation Images

Documentation images need precision and clarity. Write prompts that would allow someone to recreate the image without seeing it.

### Prompt Template

Use this structure for comprehensive prompts:

```
Create a [TYPE] showing [MAIN SUBJECT].

Layout: [DESCRIBE ARRANGEMENT]

Elements: [LIST ALL COMPONENTS WITH DETAILS]

Visual Style: [COLORS, TYPOGRAPHY, AESTHETIC]

Labels/Text: [SPECIFIC TEXT THAT MUST APPEAR]

Purpose: [HOW THIS WILL BE USED IN DOCS]

Technical: [ASPECT RATIO, RESOLUTION, FORMAT NEEDS]
```

### Real-World Examples

**For System Architecture:**
> "Create a detailed cloud architecture diagram for a serverless e-commerce platform. Top tier: CloudFront CDN distributing to global users. Second tier: API Gateway with Lambda functions (show 6 Lambda icons: ProductCatalog, ShoppingCart, Checkout, OrderProcessing, UserAuth, SearchIndex). Third tier: data layer with DynamoDB tables (Products, Orders, Users) and S3 buckets (ProductImages, Invoices). Add RDS PostgreSQL for analytics on the side. Show SQS queue between Checkout and OrderProcessing Lambdas. Include SNS topic connecting to email and SMS notification services. Draw arrows with labels (REST API calls in blue, async events in orange, data queries in green). Add AWS service icons where appropriate. Use official AWS color scheme but muted/professional. Include a VPC boundary around the Lambda functions. Add annotations for 'Auto-scaling enabled' on Lambda tier and 'Multi-AZ' on RDS. Clean, professional style for technical documentation, suitable for architecture decision records."

**For Process Infographic:**
> "Create a horizontal timeline infographic showing the 6 phases of our CI/CD pipeline from code commit to production deployment. Phase 1: 'Code Commit' (purple) - developer pushes to Git, show GitHub logo, duration: seconds. Phase 2: 'Build' (blue) - compile and bundle, show build icon, duration: 2-3 min. Phase 3: 'Unit Tests' (green) - automated testing, show test tubes icon, duration: 3-5 min. Phase 4: 'Security Scan' (orange) - SAST/DAST analysis, show shield icon, duration: 5-7 min. Phase 5: 'Staging Deploy' (yellow) - deploy to staging environment, show server icon, duration: 1-2 min. Phase 6: 'Production Deploy' (red) - blue-green deployment to prod, show cloud icon with checkmark, duration: 2-3 min. Each phase is a rounded rectangle with the phase name, icon at top, brief description, and duration at bottom. Connect phases with right-pointing arrows. Add a total timeline bar at bottom showing cumulative time (15-20 min total). Include success/failure indicators (green checkmarks) at test and scan phases. Modern, clean design with good spacing, suitable for README documentation. Add title: 'Automated Deployment Pipeline'."

**For Concept Illustration:**
> "Create an illustrated concept diagram explaining the difference between JWT and Session-based authentication using a side-by-side comparison. Left half (JWT): Show a client device, arrows going to server, and a token/ticket icon floating between them. Illustrate the JWT token as a small 'package' with three colored segments (header in blue, payload in green, signature in red). Show that the server validates but doesn't store the token. Add annotations: 'Stateless', 'Token stored client-side', 'Each request includes token'. Right half (Sessions): Show a client device, arrows to server, but also show a database behind the server. Illustrate session as a 'key-lock' metaphor - client holds the session ID 'key', server stores session data in the 'lock'. Add annotations: 'Stateful', 'Session ID stored client-side', 'Session data stored server-side'. Use a clean, slightly illustrated style (not purely technical boxes). Include small code snippets or pseudo-code annotations showing how each works. Color code: blues for JWT side, greens for Session side. Add pros/cons bullets under each approach. Professional but approachable style for developer documentation. Title at top: 'Authentication Strategies Compared'."

**For Data Visualization:**
> "Create an infographic visualizing the performance impact of different React rendering patterns. Use a bar chart layout with 5 patterns arranged left to right: 'Re-render Everything', 'Component Memoization', 'State Colocation', 'Code Splitting', 'Virtual Lists'. Each pattern has a vertical bar showing render time in milliseconds (heights: 450ms, 180ms, 120ms, 80ms, 45ms respectively). Color bars in a gradient from red (slowest) to green (fastest). Above each bar, show the pattern name and actual time. Below each bar, include a small code icon and 2-3 word description ('No optimization', 'React.memo', 'Local state', 'Dynamic import', 'Window virtualization'). Add a horizontal threshold line at 100ms labeled 'Target performance'. Include annotations with arrows pointing to the two fastest options saying 'Recommended for large lists' and 'Best for infinite scroll'. Use a clean, modern data viz style with good typography. Add title: 'React Rendering Performance Comparison (10K items)'. Include a small legend explaining the 100ms threshold comes from RAIL model guidelines. Professional style suitable for technical blog posts or architecture docs."

## Integration with Documentation Workflows

Generated images enhance technical documentation across multiple formats:

### Markdown Documentation
```markdown
## Architecture Overview

![System Architecture](./docs/images/architecture-diagram.png)

Our platform uses a microservices architecture...
```

### Confluence/Wiki Pages
- Upload generated images to page attachments
- Reference in documentation with descriptive alt text
- Update images when architecture changes

### README Files
```markdown
# Deployment Process

![CI/CD Pipeline](./README_assets/cicd-pipeline.png)

Our automated pipeline ensures...
```

### API Documentation
- Sequence diagrams for API flows
- Request/response visualizations
- Authentication process diagrams

### Technical Specifications
- Architecture decision records (ADRs)
- System design documents
- Integration guides
- Runbooks and troubleshooting guides

### Example Workflow

1. **Document outline**: User is writing technical documentation and identifies need for visual explanation
2. **Detailed prompt**: Craft comprehensive prompt describing exactly what the image should show
3. **Generate image**: Invoke nano-banana-pro with the detailed prompt
4. **Review and iterate**: Check if image matches intent, refine prompt if needed
5. **Embed in docs**: Add to documentation with descriptive context

**Pro tip**: Save your prompts alongside the images (in comments or separate prompt files) so you can regenerate or modify images later when documentation evolves.

## Output Location and Organization

Organize documentation images by type and context:

**For project documentation:**
- `./docs/images/` - General documentation images
- `./docs/diagrams/` - Architecture and system diagrams
- `./docs/infographics/` - Comparison and data visualizations
- `./README_assets/` - Images specifically for README

**For knowledge bases:**
- `./wiki/images/` - Confluence/Wiki page assets
- `./specifications/diagrams/` - Technical spec diagrams

**Naming conventions:**
- **Descriptive**: `microservices-architecture-v2.png` not `diagram1.png`
- **Versioned**: Include version numbers when diagrams evolve
- **Dated**: For temporal diagrams: `deployment-process-2026-01.png`
- **Categorized**: Prefix by type: `arch-`, `flow-`, `infographic-`, `concept-`

**Examples:**
- `arch-microservices-platform.png`
- `flow-oauth-authentication.png`
- `infographic-deployment-comparison.png`
- `concept-jwt-vs-sessions.png`
