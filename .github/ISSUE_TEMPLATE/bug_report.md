---
name: Bug report
about: Use this template to report something that is broken or behaving unexpectedly.
---

### What happened
Describe what you saw, including any relevant output, error message, or log lines.

### What you expected
One or two sentences on what the correct behavior should be.

### Steps to reproduce
List the steps that lead to the problem, in order.

### Doctor and harness output
Paste the output of `python3 scripts/doctor.py` and `python3 scripts/harness_setup.py --check`. These scripts print key names, not values, but look the output over before pasting and remove any real IP addresses or customer names.

### Commit
Paste the output of `git rev-parse --short HEAD`.

### Coding agent and version
Which agent you are using and its version.

### Security issues
Do not open an issue for a security problem. Follow the process in `SECURITY.md` instead.
