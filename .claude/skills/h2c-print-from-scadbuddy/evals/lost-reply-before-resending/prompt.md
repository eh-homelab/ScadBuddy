---
tags: [skill-creator]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

ugh. I called print_output for library file 1000000050 on the H2C (the LAN registry printer) and the MCP transport dropped before the reply came back. get_print_progress for that file says null. The printer status in Bambuddy still shows the previous job's name. Should I just call print_output again? What would you check first, concretely, and what happens if I resend while it turns out the first one did start?
