Write a {{language}} project with multiple files.

TARGET SYSTEM:
OS: {{os}}
{{lang_hint}}
Arch: {{arch}}

TASK:
{{prompt}}
{{tool_section}}
FILES TO CREATE:
{{file_descriptions}}

ENTRYPOINT: {{run_command}} {{entrypoint}}

RULES:
- Mark each file with EXACTLY: # === FILE: filename ===
- Write complete, runnable code for each file
- Prefer built-in/standard library modules over third-party packages
{{extra_rules}}- The entrypoint must work with: {{run_command}} {{entrypoint}}
- Return ONLY code, no explanations
