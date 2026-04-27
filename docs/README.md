# CNF Documentation

> CNF v0.1.0

---

## Documents

| Document | Description |
| --- | --- |
| [Architecture](Architecture.md) | System architecture — layered design, component diagrams, technology stack, data flow, security model |
| [HLD — High Level Design](HLD.md) | Business context, subsystem map, key functional flows, non-functional requirements |
| [LLD — Low Level Design](LLD.md) | Module-level design, class signatures, method definitions, state transitions, DB schema |
| [Deployment Plan](DeploymentPlan.md) | Prerequisites, Kubernetes (Helm), Ansible (bare metal), Docker Compose, rollback plan |
| [UMLs](UMLs.md) | Mermaid diagrams — class, sequence, state, ER, component, activity, deployment |
| [Startup Guide](StartupGuide.md) | Step-by-step startup order for all containers/services on controller nodes |

---

## Quick Links

- **First deployment?** → Start with [Deployment Plan](DeploymentPlan.md)
- **Starting services?** → See [Startup Guide](StartupGuide.md)
- **Understanding the design?** → Read [HLD](HLD.md) then [Architecture](Architecture.md)
- **Diving into code?** → See [LLD](LLD.md)
- **Visual diagrams?** → See [UMLs](UMLs.md)
