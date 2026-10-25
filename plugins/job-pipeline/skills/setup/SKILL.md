---
name: setup
description: This skill sets up a private job-pipeline workspace when the user asks to install, configure, or start their job search pipeline with their own CV and preferences.
---

Read [shared workflows](../../references/workflows.md), sections Workspace and Setup.
Resolve plugin resources relative to this installed skill; the plugin root is two directories above this file's directory. Never write candidate data into that root.

Ask the user to choose a new local workspace if no destination was given. Run the plugin's `scripts/create_workspace.py --destination "<new-workspace>"` with Python 3.11+. Do not overwrite an existing directory or target a plugin cache or marketplace checkout. After creation, install dependencies using the workspace instructions, initialize blank state, and collect the user's own facts and master CV.

Use the existing importer, creator comparison, visual page review, and explicit current creator approval. Onboarding never enables submission. Finish by reporting actual readiness and keeping `review_only` with cap 0. Do not make a schedule or access job portals during setup.
