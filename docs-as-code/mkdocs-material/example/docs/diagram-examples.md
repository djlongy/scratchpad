# Diagrams

Mermaid diagrams are written as text and rendered in the browser, so they live in the
same Markdown file as the prose and diff cleanly.

## Flowchart

```mermaid
graph LR
  A[Start] --> B{Failure?};
  B -->|Yes| C[Investigate...];
  C --> D[Debug];
  D --> B;
  B ---->|No| E[Success!];
```

## Sequence diagram

```mermaid
sequenceDiagram
  autonumber
  Server->>Terminal: Send request
  loop Health
      Terminal->>Terminal: Check for health
  end
  Note right of Terminal: System online
  Terminal-->>Server: Everything is OK
```

Other diagram types (state, class, entity-relationship, Gantt) use the same fence.
