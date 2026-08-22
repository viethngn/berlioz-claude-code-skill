# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Nano Banana Pro is a Claude Code plugin for generating **detailed, high-quality documentation images** using Google's Gemini 3 Pro model. Unlike generic image generators, this tool specializes in creating visual assets that explain technical concepts, illustrate systems, and enhance documentation clarity.

**Image Types Supported**:
- **Architecture Diagrams**: System designs, microservices, network topologies, cloud architectures
- **Process Flows**: CI/CD pipelines, user journeys, workflows, decision trees, algorithm flows
- **Infographics**: Feature comparisons, performance metrics, timeline visualizations, data comparisons
- **Technical Illustrations**: API sequences, data flows, integration patterns, authentication mechanisms
- **Concept Visualizations**: Explaining abstract concepts, comparing approaches, illustrating patterns
- **Annotated Visuals**: Labeled screenshots, interface mockups with explanations, component breakdowns

**Critical Requirement**: This tool requires LONG, DETAILED, DESCRIPTIVE prompts (3-5+ sentences). Short, keyword-based prompts produce poor results. Prompts should describe the image so thoroughly that someone could recreate it from the description alone.

Triggered by requests like "create a detailed diagram", "generate an infographic", "visualize this architecture", "illustrate this process", or "create documentation image".

## When to Use This Plugin

Use Nano Banana Pro when:
- Writing technical documentation that needs visual explanations
- Creating README files that would benefit from architecture diagrams
- Building knowledge base articles with process flows
- Documenting APIs with sequence diagrams
- Explaining complex systems with detailed illustrations
- Comparing multiple approaches/technologies with infographics
- Visualizing data flows, state machines, or system interactions
- Creating onboarding materials with concept visualizations

**Don't use for**:
- Simple flowcharts/diagrams better suited for Mermaid.js (use `/generate-diagram` instead)
- Quick sketches or wireframes
- Photos or realistic imagery for non-documentation purposes
- Generic artwork without technical/documentation context

## Prompt Engineering for Documentation Images

The quality of output is directly proportional to prompt detail. Follow these guidelines:

### Prompt Length
- **Minimum**: 3-5 sentences
- **Ideal**: 6-10 sentences for complex diagrams
- **Include**: What, how, why, where, style, and technical details

### Essential Elements
1. **Type**: "Create a [horizontal/vertical] [architecture diagram/infographic/flow diagram]..."
2. **Content**: List all components, labels, data points that must appear
3. **Layout**: Describe spatial arrangement and organization
4. **Relationships**: Explain connections, flows, hierarchies between elements
5. **Visual Style**: Colors (with hex codes if specific), typography, aesthetic
6. **Text**: Exact labels, titles, annotations, legends that must be present
7. **Context**: What this explains, audience level, documentation type
8. **Technical**: Aspect ratio, resolution, where it will be embedded

### Example Prompts

See the SKILL.md file for comprehensive examples of well-crafted prompts for:
- System architecture diagrams
- Process timeline infographics
- Concept comparison illustrations
- Performance data visualizations

## Running the Image Generation Script

```bash
uv run skills/generate/scripts/image.py \
  --prompt "Your image description" \
  --output "/path/to/output.png"
```

Options:
- `--prompt` (required): Image description
- `--output` (required): Output file path (PNG)
- `--aspect` (optional): `16:9` (default), aspect ratio for the image
- `--reference` (optional): Path to reference image for style guidance
- `--size` (optional): `1K` (default), `2K`, `4K`

## Prerequisites

- `GEMINI_API_KEY` environment variable must be set with a Google AI API key
- Python 3.10+ with `uv` package manager
- Dependencies (`google-genai`, `pillow`, `httpx`) are managed via inline script metadata

## Architecture

```
nano-banana-pro/
└── skills/
    └── generate/
        ├── SKILL.md          # Skill definition and usage docs
        └── scripts/
            └── image.py      # Main image generation script
```

The plugin follows Claude Code's skill structure where `SKILL.md` defines the skill metadata (name, description, triggers) and provides usage instructions. The Python script uses Google's GenAI SDK with inline PEP 723 dependencies for zero-config execution via `uv run`.

## Example Usage

### Generate Architecture Diagram
```bash
uv run skills/generate/scripts/image.py \
  --prompt "Create a detailed cloud architecture diagram showing a three-tier web application deployed on AWS. Front tier: CloudFront CDN with Route53 DNS, both shown with AWS icons. Application tier: Application Load Balancer distributing to an Auto Scaling group of EC2 instances (show 4 EC2 icons in a group), running in a VPC across two availability zones (use different colored zones, light blue and light green). Data tier: RDS PostgreSQL with Multi-AZ setup (show primary and standby), ElastiCache Redis cluster (3 nodes), and S3 bucket for static assets. Draw arrows showing request flow from users through CDN to ALB to EC2 to database. Include a NAT Gateway for outbound internet access. Add security group icons around EC2 and RDS layers. Use official AWS colors but slightly muted for professionalism. Label each component clearly. Add annotations for 'Auto-scales 2-10 instances' on EC2 tier and 'Automated backups enabled' on RDS. Clean, professional style suitable for system design documentation." \
  --output "./docs/diagrams/aws-architecture.png" \
  --size 1K \
  --aspect 16:9
```

### Generate Comparison Infographic
```bash
uv run skills/generate/scripts/image.py \
  --prompt "Create a side-by-side comparison infographic contrasting SQL vs NoSQL databases for our technical documentation. Left side: SQL (Relational) with a table icon in blue. Show characteristics as a vertical list: Fixed schema (with table structure illustration), ACID transactions (checkmark), Vertical scaling (upward arrow), Complex queries with JOINs (SQL snippet icon), Best for: Financial systems, Examples: PostgreSQL, MySQL. Right side: NoSQL (Document) with a document/JSON icon in orange. Show characteristics: Flexible schema (with JSON structure illustration), Eventual consistency (circular arrows), Horizontal scaling (multiple server icons), Simple queries (key-value icon), Best for: Real-time apps, Examples: MongoDB, DynamoDB. Use color-coded headers (blue vs orange). In the middle, add a gray vertical divider with decision criteria: 'Use SQL if: Complex relationships, ACID required, Structured data' and 'Use NoSQL if: Rapid iteration, Massive scale, Flexible data'. Modern, clean design with icons and good typography. Title at top: 'Database Selection Guide'. Suitable for architecture decision documentation, 16:9 aspect ratio." \
  --output "./docs/infographics/database-comparison.png" \
  --size 1K
```

### Generate Process Flow
```bash
uv run skills/generate/scripts/image.py \
  --prompt "Create a vertical flowchart showing the OAuth 2.0 authorization code flow with PKCE for mobile apps. Start with 'User opens mobile app' at top. Next: 'App generates code_verifier and code_challenge'. Then: 'Redirect to authorization server with code_challenge'. User sees: 'Login screen' (show smartphone icon). After login: 'Authorization server validates credentials'. Decision diamond: 'User approves?' with No path going to 'Access denied (error screen)' and Yes path continuing to 'Generate authorization code'. Next: 'Redirect back to app with code'. App then: 'Exchange code + code_verifier for tokens'. Server: 'Validate code_verifier matches code_challenge'. Final step: 'Return access_token and refresh_token'. End with 'User authenticated in app'. Use rounded rectangles for process steps, diamonds for decisions, parallelograms for user interactions. Color code: blue for app steps, green for server steps, orange for user interactions. Add small icons (lock for security steps, key for tokens). Include annotations explaining PKCE adds security for mobile. Professional style for API security documentation." \
  --output "./docs/diagrams/oauth-pkce-flow.png" \
  --size 1K \
  --aspect 9:16
```
