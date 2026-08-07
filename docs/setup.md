# Setup and Configuration Guide

## Prerequisites

- Git 2.30+
- A GitHub account with access to this repository

## Local Setup

```bash
# Clone the repository
git clone https://github.com/dinghaiyan-ux/test-dhy.git
cd test-dhy

# Create a new branch for your work
git checkout -b feature/your-feature-name

# Make changes, then commit
git add .
git commit -m "Describe your changes"

# Push and open a pull request
git push origin feature/your-feature-name
```

## Configuration

No additional configuration is required for basic usage. CI checks will run automatically when a pull request is opened.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Push rejected | Ensure your branch is up to date with `main` |
| CI checks failing | Review the build logs in the PR's Checks tab |

---

*Last updated: March 27, 2026*
