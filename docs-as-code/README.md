# docs-as-code

Documentation that lives in a git repo as Markdown, is reviewed like code, and is
published by CI. Each project here is a self-contained tutorial with a runnable example.

| Project | What it covers |
|---|---|
| [`gitlab-wiki-mirror/`](gitlab-wiki-mirror/) | Repo `docs/` mirrored into the project's GitLab wiki by CI (pages, attachments, sidebar from the MkDocs nav, guard against hand edits), an importer for moving an existing wiki into the repo, and the full pipeline. The no-Pages option. |
| [`mkdocs-material/`](mkdocs-material/) | Material for MkDocs from zero to a published site: theme, fonts, emoji, code blocks, tabs, admonitions, Mermaid diagrams, footer, and deployment to GitHub Pages or GitLab Pages. |
