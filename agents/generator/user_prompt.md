Write a {{language}} script for this task.

TARGET SYSTEM:
OS: {{os}}
{{lang_hint}}
Arch: {{arch}}

TASK:
{{prompt}}
{{tool_section}}
RULES:
- Return ONLY executable {{language}} code
- No explanations, no markdown, no comments about saving files
- Prefer built-in/standard library modules over third-party packages
{{extra_rules}}- The code must be complete and runnable with: {{run_command}} {{entrypoint}}
