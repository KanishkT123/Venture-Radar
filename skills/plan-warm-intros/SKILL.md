---
name: plan-warm-intros
description: Build evidence-based warm-introduction routes to venture investors using Venture Radar's local network and cited portfolio relationships. Use when prioritizing introduction paths or identifying missing relationship evidence.
---

# Plan warm introductions

Use the Investor dashboard and GET /api/intros?investor=<URL-encoded firm>. The application computes routes locally. Do not send private relationship data to a model or external service without the user's authorization.

1. Start at the node named You. Inspect saved directed relationships and their evidence. Ask only for missing relationships that would materially change the route; never invent a connection.
2. Prefer fully confirmed routes. Compare up to three hops using the product of relationship strengths and a 20% penalty for every hop after the first. This is a ranking heuristic, not a success probability.
3. Mark every unconfirmed link. A cited company-investor funding relationship is a possible bridge, not proof that the user knows the founder or that anyone will introduce them.
4. For each candidate, state the connector, evidence, next verification step and why the firm fits. Review competition, stage and geography before outreach.
5. With no usable route, say so. Suggest asking an existing advisor or portfolio founder whether they actually know the relevant partner. Shared institutions alone are not a warm path.
6. If the user requests a draft, write a short forwardable introduction grounded in verified facts and the user's actual progress. Do not send it without explicit authorization.

Use the app's Manage your network action to save confirmed relationships with evidence. Leave private data in the local database and out of repository commits. Never treat a public announcement as permission to contact someone.
